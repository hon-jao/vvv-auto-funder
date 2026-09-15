# FunderHarvester Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task. Tasks are ordered and self-contained; execute sequentially. Each task ends with a commit.

**Goal:** An on-chain `FunderHarvester` contract on Base that holds staked VVV, lets anyone trigger a permissionless harvest (claim → Uniswap swap → forward USDC to the owner's funder wallet), automated by Gelato/Chainlink keepers, with a static web control panel.

**Architecture:** Solidity contract (Foundry) holds staked VVV and exposes `harvest()`. A keeper bot checks the harvest condition off-chain and calls it. A static page reads state + offers stake/configure/harvest buttons via wallet signatures. Repo layout: new `onchain/` directory inside https://github.com/hon-jao/vvv-auto-funder (contract + foundry) and `web/` (page).

**Tech Stack:** Foundry (forge/cast), Solidity 0.8.20+, viem (JS, CDN), Gelato Automate (Base), GitHub Pages.

**Contract addresses (Base mainnet, 8453 — verified, do not re-derive):**
- StakingV2 `0x321b7ff75154472B18EDb199033fF4D116F340Ff` (`pendingRewards(address)`, `claim()`, cooldown does NOT block claims)
- VVV `0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf`
- USDC `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`
- SwapRouter02 `0x2626664c2603336E57B271c5C0b26F421741e481`
- QuoterV2 `0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a`
- V3 factory `0x33128a8fC17869897dcE68Ed026d694621f6FDfD`, VVV/USDC 1% pool `0xF85fA96193f411AE7bAA892Dd4AbB7dAceDF9191`

**Critical Solidity gotchas (from the Python implementation):**
- Use path-based `quoteExactInput`/`exactInput` — `quoteExactInputSingle` REVERTS on Base for this pool.
- QuoterV2 returns a 4-word struct (amountOut, sqrtPriceX96After, ticksCrossed, gasEstimate); take word 0.
- VVV is 18 decimals; USDC is 6.
- Before staking from the contract, StakingV2 must be approved on the VVV token.

---

### Task 0: Scaffold Foundry project in `onchain/`

**Objective:** Working Foundry build environment with the real Base fork wired up.

**Files:**
- Create: `onchain/foundry.toml`, `onchain/src/FunderHarvester.sol` (empty shell), `onchain/test/Harvester.t.sol` (empty), `onchain/script/Deploy.s.sol` (stub)

**Steps:**
1. `cd onchain && forge init --no-commit` (or hand-create foundry.toml: `solc = "0.8.24"`, `evm_version = "cancun"`, rpc_endpoints = { base = "${BASE_RPC_URL}" })
2. `forge install foundry-rs/forge-std OpenZeppelin/openzeppelin-contracts --no-commit`
3. Verify fork works: `forge test --fork-url $BASE_RPC_URL` (trivial test passes)
4. Commit: `feat(onchain): foundry scaffold`

### Task 1: `pendingRewards()` + `price()` view helpers with fork tests

**Objective:** Contract can read its own pending VVV rewards and quote VVV→USDC.

**Files:**
- Modify: `onchain/src/FunderHarvester.sol`
- Test: `onchain/test/Harvester.t.sol`

**Step 1: Write failing test** (fork test against Base mainnet)
```solidity
function test_quoteAndPending() public {
    deal(VVV, address(harvester), 10e18);
    (bool ok,) = address(harvester).call(abi.encodeWithSignature("stakePendingQuote()"));
    // assert quoter returns > 0 for 10 VVV
}
```
**Step 2:** `forge test --fork-url $BASE_RPC_URL -vvv` → FAIL
**Step 3:** Implement:
- `pendingRewards()` → `StakingV2.pendingRewards(address(this))`
- `quoteHarvest()` → QuoterV2.quoteExactInput(path VVV→fee10000→USDC, pending amount); parse word 0 of the 4-word struct return
**Step 4:** PASS. **Step 5:** commit `feat(onchain): pending+quote views`

### Task 2: `harvest()` — claim, swap, forward, with safety rails

**Objective:** The core permissionless function.

**Files:** `onchain/src/FunderHarvester.sol`, `onchain/test/Harvester.t.sol`

**Requirements (all must be enforced on-chain):**
1. `harvest()` is permissionless (no auth) but gated: reverts if `pendingRewards() < minHarvestUsd * 1e18 / price` — simpler: gate on `quoteHarvest() >= minOut` where `minHarvestUsdcRaw` is owner-set.
2. Claim from StakingV2.
3. `exactInput` VVV→USDC (path encoding identical to Python version), `amountOutMinimum` = quoted * (10000 - slippageBps) / 10000. Revert on under-min (Uniswap enforces).
4. Forward **only what was just received**: measure `USDC.balanceOf(this)` before and after swap; transfer exactly the delta to `funder`. NEVER forward total balance.
5. Reimburse keeper gas if the caller isn't owner (optional v1.1 — skip in v1; Gelato 1Balance or sync-fee handles this separately).
6. Events: `Harvested(uint256 vvvClaimed, uint256 usdcForwarded, address indexed caller)`.

**Test:** fork test — `deal` VVV to the contract, call `harvest()` from a random EOA, assert: USDC balance of funder increased by delta > 0, contract holds no leftover USDC.
**Commit:** `feat(onchain): permissionless harvest with delta-forward guard`

### Task 3: Owner config + emergency rails

**Objective:** Owner controls; contract can never be drained by harvest().

**Files:** same as Task 2.

**Requirements:**
- `Ownable` (OZ). `setFunder(address)`, `setMinHarvestUsdc(uint256)`, `setSlippageBps(uint16)` (max 500 = 5%).
- `withdrawERC20(token, amount, to)` — owner-only, for emergencies. Note in NatSpec this is admin trust, not trustless.
- `harvest()` must NOT be callable in a way that lets a caller redirect funds: forward target is always `funder` (state), never `msg.sender`.
- Fuzz test: random EOAs calling harvest() can never change funder or receive the USDC.
**Commit:** `feat(onchain): owner config + emergency withdraw`

### Task 4: `stakeFromHarvester` flow — getting principal in

**Objective:** Owner deposits VVV; contract stakes it with StakingV2.

**Steps:** check StakingV2 interface for `stake(uint256)`/`stakeWithPermit` — read the deployed bytecode / Venice docs first. Implement `depositAndStake(uint256 vvvAmount)`: pull VVV from owner (transferFrom after user approval), approve StakingV2, stake. Test on fork: deposit 10 VVV → StakingV2 reports it as staked balance.
**Commit:** `feat(onchain): deposit+stake`

### Task 5: Deploy to Base Sepolia, then Base mainnet

**Objective:** Real deployment with verified source.

**Steps:**
1. `forge script script/Deploy.s.sol --rpc-url base --broadcast --verify` (Base Sepolia first: chain 84353, need testnet StakingV2 address — if unavailable, mainnet-fork tests are the gate; deploy mainnet directly after review)
2. Record deployed address in `web/config.js` + this plan.
3. **Before mainnet:** run a second agent review of the contract (requesting-code-review skill) focused on: reentrancy (StakingV2 claim callback?), approval hygiene, delta-forward correctness.
**Commit:** `deploy: base deployment + addresses`

### Task 6: Keeper automation (Gelato)

**Objective:** Hands-off triggering.

**Steps:**
1. Create Gelato Automate task: condition `quoteHarvest(address(harvester)) >= minHarvestUsdc` (off-chain resolver or Gelato's on-chain condition module reading the contract's public view), exec `harvest()`.
2. Alternatively document Chainlink Automation setup as the fallback.
3. Fund the keeper balance (small ETH on Base).
4. Verify: force a condition-true state on a test wallet, observe harvest firing within Gelato's check window.
**Commit:** `ops: gelato task config + docs`

### Task 7: Web control panel (`web/index.html`)

**Objective:** Static page — connect, view, act.

**Steps:**
1. Single HTML + JS file, viem via CDN (`esm.sh/viem`), no build step.
2. Read-only panels: pending rewards (StakingV2.pendingRewards via public RPC), USDC quote, contract config (funder, minHarvest, slippage), recent Harvested events (viem getLogs).
3. Action buttons (wallet-signed): `depositAndStake`, `harvest` (for demo/manual trigger), `setFunder`, `setMinHarvest`.
4. Branding: "Stake once. Your AI bill pays itself."
5. Test on Base mainnet with a small amount; confirm all wallet popups fire correctly.
**Commit:** `feat(web): control panel`

### Task 8: Docs + README integration

**Objective:** The public story.

**Steps:**
1. New README section: on-chain vs CLI modes, risk table (keeper fees, contract risk, admin trust).
2. `docs/funder-harvester.md`: architecture diagram, deploy addresses, keeper setup guide.
3. GitHub Pages enable (`web/` folder).
**Commit:** `docs: onchain architecture + pages`

---

## Out of scope (v2)
- Audited deployment for third-party funds (requires external review)
- Multi-token rewards / multi-provider funders (contract currently single funder)
- Alternative keeper networks (Uniswap-specific hooks, etc.)

## Verification gates (do not skip)
- Fork tests against real Base state for Tasks 1–4
- Agent code review of Solidity before mainnet (Task 5)
- Small-amount mainnet test of full flow before announcing anything
