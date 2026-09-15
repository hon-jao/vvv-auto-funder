# vvv-auto-funder

## Plain English Explainer: 

If you stake VVV (the token of the Venice AI platform), you earn rewards continuously —
but they just sit there until someone collects them. This tool is a script that
does that collection for you, automatically, and turns the rewards into something you
can actually spend on Venice's AI services. You could modify this for any AI provider that consumes USDC credits. 

Here's the journey your rewards take:

1. **Collect** — The tool checks how many reward tokens you've earned. If it's a
   worthwhile amount (you choose the minimum — default $12), it collects them.
2. **Convert** — Reward tokens can't be spent directly, so the tool trades them for
   USDC (a US-dollar stablecoin) on Uniswap, a public exchange.
3. **Deliver** — The freshly converted USDC is sent to a second wallet (the "funder")
   that exists purely to pay for Venice AI usage.
4. **Top up** — Optionally, the tool uses that USDC to add credit to your Venice AI
   pay-per-use balance automatically, so your AI bill pays itself.

Two safety ideas worth understanding:

- **It does nothing by default.** Running it normally just *shows you the plan* —
  nothing is collected, traded, or sent. Only when you add a special flag
  (`--execute`) does it actually touch your money.
- **It only ever sends what it just earned.** The tool measures exactly how much USDC
  appeared from the trade and sends only that amount. Your existing balance, your
  staked tokens, and everything else are never touched. If the numbers don't add up,
  it stops and sends nothing.

You'll need: a computer that can run Python, two wallet private keys (one for the
wallet that stakes VVV, one for the funder wallet), and about 10 minutes. The rest of
this document explains the details; if someone technical (or an AI assistant) is
setting it up for you, they can jump straight to the agent instructions below.

---

## For AI agents: setup & installation prompt

> The following is a self-contained task description. Pass it to an AI coding agent
> (Claude Code, Codex, OpenCode, a CLI agent, etc.) to have it install and configure
> vvv-auto-funder on a fresh Linux/macOS machine.

```markdown
# TASK: Install and configure vvv-auto-funder

## Context
vvv-auto-funder is a Python tool that autonomously claims Venice (VVV) staking
rewards on the Base blockchain (chain ID 8453), swaps them to USDC on Uniswap V3,
forwards the USDC to a "funder" wallet, and optionally tops up Venice x402 credits.
Source: https://github.com/hon-jao/vvv-auto-funder (clone it).
Docs for the payment rail: docs/x402-explainer.md in the repo.

## Step 1 — System dependencies
- Python 3.10+ and pip. Verify: python3 --version
- Node.js 18+ and npm — ONLY needed if the optional --topup flag will be used.
  Verify: node --version
- Do NOT use a venv unless the user wants one; system python3 is fine.

## Step 2 — Get the code
git clone https://github.com/hon-jao/vvv-auto-funder.git
cd vvv-auto-funder
pip install web3 eth-account
# Only for --topup support:
cd venice_x402 && npm install && cd ..

## Step 3 — Configuration (.env)
Copy .env.example to .env and chmod 600 .env. The user must supply these values —
NEVER invent, generate, or "test" private keys. Ask the user for each:

- VVV_SIGNER_KEY: private key (0x-prefixed) of the wallet that stakes VVV.
  This wallet will sign claims, swaps, and forwards.
- FUNDER_KEY: private key of the funder wallet (holds USDC, pays for x402 credits).
  Can be the same as VVV_SIGNER_KEY only if the user explicitly wants one wallet.
- FUNDER_ADDR: the funder wallet's public address (0x...).
- EXPECTED_MAIN: the public address derived from VVV_SIGNER_KEY. This is a safety
  check — the script aborts if the loaded signer key doesn't derive to this address.
  To help the user find it, derive the address from the key they provided and
  confirm it matches: python3 -c "from eth_account import Account; print(Account.from_key('KEY').address)"
  (run this WITHOUT printing the key itself, only the derived address).

Optional settings (defaults are sane):
- BASE_RPC (default https://mainnet.base.org)
- VVV_USDC_FEE (default 10000 = the 1% Uniswap fee tier)
- SLIPPAGE_BPS (default 200 = 2%)

## Step 4 — Validate before any real transaction
1. Fund check: the main wallet needs a little ETH on Base for gas (0.005+ ETH is
   plenty; the script needs 0.0005 minimum). The funder needs a little ETH too —
   the script auto-sends it 0.005 from main if it drops below 0.002.
2. Dry-run test — MUST pass with no errors before anything else:
   python3 vvv_auto_funder.py
   Expected: a JSON line ending in "outcome": "below_threshold" or
   "dry_run_complete", zero errors.
3. Only after a clean dry-run, and only with the user's explicit approval:
   python3 vvv_auto_funder.py --execute
   Check the returned tx hashes on basescan.org before declaring success.

## Step 5 — Optional automation (cron)
Only set up after a successful --execute run. Example (daily at 13:30 UTC):
  30 13 * * * cd /path/to/vvv-auto-funder && /usr/bin/python3 vvv_auto_funder.py --execute --topup >> funder.log 2>&1
Ensure the cron environment can read .env (the script loads it from its own directory).

## Hard rules
- Dry-run first, always. --execute only with explicit human approval.
- Never print, log, or transmit any private key.
- Never modify the .env values yourself; they come from the user.
- If any step errors, stop and report the exact error — do not improvise workarounds
  on a system that handles real funds.
- First --execute should be run manually and its txs verified on a block explorer
  before any cron automation is enabled.
```

---

## How it works

```
claim() on StakingV2 ──▶ Uniswap V3 swap VVV→USDC ──▶ balance-diff verify ──▶ forward to funder ──▶ optional x402 top-up
```

**Dry-run by default.** `--execute` is required for any on-chain state change.

1. **Gate** — reads `pendingRewards(yourWallet)` from Venice's StakingV2 contract and the VVV/USD
   price (CoinGecko → DexScreener fallback). Below `--min-usd` (default $12): nothing happens.
2. **Funder gas top-up** — if the funder wallet's ETH is below 0.002, sends it 0.005 ETH from the
   main wallet (so the funder can always pay gas for x402 settlements).
3. **Claim** — calls `claim()` on StakingV2. The 7-day unstaking cooldown does **not** block
   reward claims.
4. **Swap** — Uniswap V3 `exactInput` on SwapRouter02 (path-based; `quoteExactInputSingle`
   is unreliable for some pools on Base), VVV/USDC 1% fee tier (configurable).
   Quotes first via QuoterV2 and applies a slippage guard (`SLIPPAGE_BPS`, default 2%).
5. **Verify** — measures the actual USDC balance diff (post − pre). The forward amount is *only*
   this verified delta, capped at quote × 1.02. Pre-existing USDC and staked principal are never
   touched.
6. **Forward** — `transfer()` of the exact delta to `FUNDER_ADDR`.
7. **Top-up (optional)** — `--topup` checks the funder's Venice x402 credit and tops up $5 via
   `venice_x402/topup.mjs` when below $10.

## Setup

```bash
pip install web3 eth-account
cd venice_x402 && npm install && cd ..   # only needed for --topup

cp .env.example .env
chmod 600 .env   # private keys in here
```

`.env`:

```
VVV_SIGNER_KEY=0x...        # wallet that stakes VVV (claims + swaps + forwards)
FUNDER_KEY=0x...            # funder wallet key (x402 top-ups only)
FUNDER_ADDR=0x...           # funder wallet address
EXPECTED_MAIN=0x...         # must equal the address derived from VVV_SIGNER_KEY (safety check)
# optional:
# MIN_USD=12                # conversion threshold (command-line --min-usd overrides this)
# BASE_RPC=https://mainnet.base.org
# VVV_USDC_FEE=10000        # Uniswap fee tier in hundredths of a bip (10000 = 1%)
# SLIPPAGE_BPS=200          # 2% slippage guard
# TOPUP_BASE_URL=https://api.venice.ai/api/v1   # switch to any x402-compatible provider
# TOPUP_THRESHOLD=10        # top up when credit < this
# TOPUP_AMOUNT=5            # USD per top-up
```

`EXPECTED_MAIN` is a dead-man switch: if the loaded signer key doesn't derive to that address the
run aborts before touching anything.

### Customizing

**When rewards convert** — the script is stateless; conversion timing = how often you run it
(cron schedule) × the `--min-usd` threshold. Higher threshold = fewer, larger conversions
(less gas overhead per dollar); lower = more frequent. `MIN_USD` in `.env` sets the default,
`--min-usd` on the command line overrides it.

### Privacy: on-chain correlation

Everything this tool does is **publicly visible on Base**, forever. Anyone who knows one of
your wallet addresses can see on a block explorer that it claims VVV, swaps on Uniswap, and
forwards to another address — and connect that pattern to this tool. This is inherent to
running any on-chain automation and cannot be undone retroactively.

Practical mitigations:

- **Use a fresh funder wallet** if the pairing of your main staking wallet to a specific
  funder address is the sensitive part. Future activity then breaks the link (past history
  stays linked permanently).
- **A fresh main wallet per deployment** (new staking position, new funder) gives a clean
  on-chain identity per instance.
- **Do not publish tx hashes** from your own runs in issues, PRs, or README examples.
- The reference deployment in `.env.example` uses placeholder addresses only.

This tool's public repo contains no wallet addresses or transaction history; the
linkability comes from on-chain activity itself, not from this codebase.

**Switching AI provider** — the funder wallet holds plain USDC on Base, so any x402-compatible
provider works. Set `TOPUP_BASE_URL` (and optionally `TOPUP_THRESHOLD`/`TOPUP_AMOUNT`) in `.env`;
`venice_x402/topup.mjs` settles against whatever provider you point it at, as long as the
provider serves the standard x402 402-discovery flow on Base USDC. The balance check
(`X402BalanceProvider` in `vvv_auto_funder.py`) targets Venice by default — its docstring
explains how to retarget or bypass it for other providers.

**The top of `vvv_auto_funder.py` has a CUSTOMIZING block** documenting every knob:
conversion timing, provider switching, swap/fee tuning, gas behavior, and common code
revisions (different reward source, different stablecoin, multi-hop swaps, claim-only mode).

## Usage

```bash
python3 vvv_auto_funder.py                    # dry-run — prints the plan, signs nothing
python3 vvv_auto_funder.py --execute          # claim + swap + forward
python3 vvv_auto_funder.py --execute --topup  # also tops up x402 credit when < $10
python3 vvv_auto_funder.py --execute --min-usd 20
```

Output is a single JSON line with tx hashes and balances — easy to parse from cron and alert on.

### Cron example

```
30 13 * * *  cd /path/to/vvv-auto-funder && /usr/bin/python3 vvv_auto_funder.py --execute --topup >> funder.log 2>&1
```

## Safety notes

- Keys live in `.env` (or env vars) and are never printed or logged.
- Every transaction is `eth_call`-simulated before broadcast; any revert aborts the run.
- The delta guard means a partial/failed swap can never cause over-forwarding: if the measured
  USDC delta doesn't match the quote within tolerance, the forward is aborted and the USDC stays
  in the main wallet, safe to retry later.
- **On-chain privacy:** claim/swap/forward patterns from your wallet are publicly visible on Base.

## Verified contract addresses (Base mainnet, chain 8453)

| Contract | Address |
|---|---|
| Venice StakingV2 | `0x321b7ff75154472B18EDb199033fF4D116F340Ff` |
| VVV token | `0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf` |
| USDC (native) | `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913` |
| Uniswap V3 SwapRouter02 | `0x2626664c2603336E57B271c5C0b26F421741e481` |
| Uniswap V3 QuoterV2 | `0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a` |

The VVV/USDC pool is resolved at runtime from the Uniswap V3 factory for the configured fee tier —
if the pool doesn't exist at `VVV_USDC_FEE`, the run aborts rather than guessing.

## Files

- `vvv_auto_funder.py` — main loop (claim → swap → verify → forward → top-up)
- `venice_x402/topup.mjs` — x402 top-up (signs `transferWithAuthorization` via the x402 SDK)
- `venice_x402/x402_helper.py` — x402 balance/transactions via SIWE auth
- [`docs/x402-explainer.md`](docs/x402-explainer.md) — what x402 is, how Venice implements it,
  and how this tool plugs into that payment rail

## License

MIT
