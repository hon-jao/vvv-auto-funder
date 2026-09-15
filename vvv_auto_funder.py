#!/usr/bin/env python3
"""
vvv-auto-funder — Claim VVV staking rewards on Base, swap to USDC via Uniswap V3,
forward the verified delta to a funder wallet, optionally top up x402 credit.

The "provider" is whatever x402-compatible AI API you point TOPUP_* at. Venice is the
default; any provider that (a) serves a 402-based x402 top-up and (b) accepts USDC on
Base works — see CUSTOMIZING below.

Flow:
  1. Load VVV_SIGNER_KEY + FUNDER_KEY from environment / .env (keys never printed)
  2. Gate: pendingRewards(main) x VVV price >= --min-usd, gas >= MIN_GAS_ETH
  3. claim() on Venice StakingV2 (eth_call simulated first)
  4. Swap VVV -> USDC via Uniswap V3 SwapRouter02 (exactInput, path-based)
  5. Balance-diff verification: delta = post - pre USDC; must be 0 < delta <= quoted*1.02
  6. Forward EXACT verified delta to funder wallet (never the total balance)
  7. Optional --topup: if funder x402 credit < TOPUP_THRESHOLD, top up TOPUP_AMOUNT

Safety rails:
  - Dry-run by default; --execute required for any state change
  - Never reads total balance as a transfer amount — only the Step-5 verified delta
  - Never touches staked principal, DIEM, or pre-existing USDC
  - All txs simulated (eth_call) before broadcast; abort on revert

Requires: pip install web3 eth-account
Usage:
  python3 vvv_auto_funder.py                 # dry-run: prints plan, signs nothing
  python3 vvv_auto_funder.py --execute       # real transactions
  python3 vvv_auto_funder.py --execute --topup --min-usd 12

=======================================================================================
CUSTOMIZING — the knobs you're most likely to want to turn
=======================================================================================

WHEN REWARDS CONVERT (schedule):
  The script itself is stateless — "when" is whatever schedule you run it on.
  Two levers:
    1. Cron frequency — how often the script *checks* (cheap: a dry-run-equivalent
       price+balance read costs nothing on-chain; it only transacts above the gate):

         # check twice a day, act only if pending >= $12 (default --min-usd)
         30 13 * * *  cd /path && python3 vvv_auto_funder.py --execute --topup >> funder.log 2>&1
         30 1  * * *  cd /path && python3 vvv_auto_funder.py --execute --topup >> funder.log 2>&1

    2. --min-usd — the USD threshold that must accumulate before any transaction fires.
       Higher = fewer, larger swaps (less gas overhead per dollar, price-averaging);
       lower = more frequent but each conversion costs ~2 txs of gas.
       Pass on the command line (--min-usd 25) or bake into cron.

WHICH AI PROVIDER RECEIVES THE CREDITS:
  Default is Venice (api.venice.ai). To point at a different x402 provider, set in .env:

    TOPUP_BASE_URL=https://provider.example/api/v1   # provider's API base
    TOPUP_PATH=/x402/top-up                          # their 402 top-up endpoint

  The provider must implement the x402 "402 discovery → signed payment → settle"
  pattern on Base USDC (most x402-compatible APIs do; this is the protocol's core
  flow — see docs/x402-explainer.md). Then just run with --topup as usual.

  NOTE on the balance check: x402_balance() below calls the *Venice* balance endpoint.
  If you switch providers, update X402BalanceProvider.get() (see its docstring) to the
  provider's balance endpoint, or no-op it and rely on their dashboard.

SWAP / PRICE TUNING:
    VVV_USDC_FEE   Uniswap fee tier (10000 = 1%). The script resolves the pool from
                   the factory and aborts if none exists at that tier — safe to change.
    SLIPPAGE_BPS   Max slippage vs quote (200 = 2%).
    DELTA_TOLERANCE  (code constant) how far above the quote the measured swap result
                   may sit before the forward is aborted. 1.02 = 2%.

GAS BEHAVIOR:
    MIN_GAS_ETH / GAS_FLOOR / GAS_TOPUP (code constants) — when the funder's ETH dips
    below GAS_FLOOR the main wallet sends it GAS_TOPUP. Edit if your gas costs differ.

EXTENDING — common revisions:
  * Different reward source (e.g. another staking contract): replace STAKING_V2 and
    the pendingRewards/claim selectors in main() — the rest of the pipeline is
    source-agnostic.
  * Swap to a different stablecoin/token: change USDC_TOKEN (keep 6-decimals math in
    sync — USDC is 6dp; DAI/USDS are 18) and the path in _path_bytes().
  * Multi-hop swap (e.g. VVV->WETH->USDC if liquidity is thin): extend _path_bytes()
    to tokenA + feeA + tokenB + feeB + tokenC — Uniswap V3 paths chain fee-tier
    segments, and exactInput/quoteExactInput handle arbitrary hops unchanged.
  * Keep the VVV (skip swap/forward): comment out steps 3-5 in main() and use
    --execute alone to just claim.
=======================================================================================
"""
import argparse
import json
import logging
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone

try:
    from web3 import Web3
    from eth_account import Account
except ImportError:
    print("Missing web3/eth-account. Run: pip install web3 eth-account")
    sys.exit(1)

# --- Load .env if present (stdlib only, no python-dotenv dependency) ---
ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(ENV_FILE):
    with open(ENV_FILE) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

# --- Constants (verified Sep 2026) ---
CHAIN_ID = 8453  # Base mainnet
RPC_PRIMARY = os.environ.get("BASE_RPC", "https://mainnet.base.org")
RPC_FALLBACKS = ["https://base.llamarpc.com", "https://1rpc.io/base", "https://base-rpc.publicnode.com"]
STAKING_V2 = Web3.to_checksum_address("0x321b7ff75154472B18EDb199033fF4D116F340Ff")
VVV_TOKEN = Web3.to_checksum_address("0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf")   # VVV on Base
USDC_TOKEN = Web3.to_checksum_address("0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913")  # native USDC on Base
SWAP_ROUTER = Web3.to_checksum_address("0x2626664c2603336E57B271c5C0b26F421741e481")  # Uniswap V3 SwapRouter02 (Base)
QUOTER_V2 = Web3.to_checksum_address("0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a")    # Uniswap V3 QuoterV2 (Base)
V3_FACTORY = Web3.to_checksum_address("0x33128a8fC17869897dcE68Ed026d694621f6FDfD")   # Uniswap V3 factory (Base)

VVV_USDC_FEE = int(os.environ.get("VVV_USDC_FEE", "10000"))  # 1% fee tier by default
SLIPPAGE_BPS = int(os.environ.get("SLIPPAGE_BPS", "200"))    # 2% slippage guard on the quote

FUNDER_ADDR = os.environ.get("FUNDER_ADDR", "")
EXPECTED_MAIN = os.environ.get("EXPECTED_MAIN", "")
LOG_PATH = os.environ.get(
    "VVV_LOG_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "vvv-auto-funder.log")
)
TOPUP_SCRIPT = os.environ.get(
    "TOPUP_SCRIPT", os.path.join(os.path.dirname(os.path.abspath(__file__)), "venice_x402", "topup.mjs")
)
X402_HELPER = os.environ.get(
    "X402_HELPER", os.path.join(os.path.dirname(os.path.abspath(__file__)), "venice_x402", "x402_helper.py")
)

# --- x402 provider settings (default: Venice) ---
# Point these at any x402-compatible provider to switch where the credits land.
TOPUP_BASE_URL = os.environ.get("TOPUP_BASE_URL", "https://api.venice.ai/api/v1")
TOPUP_THRESHOLD = float(os.environ.get("TOPUP_THRESHOLD", "10"))  # top up when credit < this
TOPUP_AMOUNT = float(os.environ.get("TOPUP_AMOUNT", "5"))        # USD per top-up

MIN_GAS_ETH = 0.0005
DELTA_TOLERANCE = 1.02  # accept up to 2% above quoted
COOLDOWN_SECONDS = 604800  # StakingV2 cooldown (7d) — does NOT block reward claims

log = logging.getLogger("vvv-funder")


def setup_logging():
    os.makedirs(os.path.dirname(LOG_PATH) or ".", exist_ok=True)
    log.setLevel(logging.INFO)
    fh = logging.FileHandler(LOG_PATH)
    fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    log.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    log.addHandler(sh)


def load_key(name: str) -> Account:
    """Load a private key from env/.env. Never prints it."""
    v = os.environ.get(name)
    if not v:
        raise SystemExit(f"{name} not set (env or .env)")
    return Account.from_key(v)


def get_w3() -> Web3:
    for rpc in [RPC_PRIMARY] + RPC_FALLBACKS:
        try:
            w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 30}))
            if w3.is_connected() and w3.eth.chain_id == CHAIN_ID:
                return w3
        except Exception:
            continue
    raise SystemExit("No working Base RPC")


def eth_call(w3, to, data, frm):
    return w3.eth.call({"to": to, "data": data, "from": frm})


def erc20_balance(w3, token, addr, decimals) -> float:
    data = "0x70a08231" + addr[2:].lower().zfill(64)
    return int(eth_call(w3, token, data, addr).hex(), 16) / 10 ** decimals


def erc20_allowance(w3, token, owner, spender) -> int:
    data = "0xdd62ed3e" + owner[2:].lower().zfill(64) + spender[2:].lower().zfill(64)
    return int(eth_call(w3, token, data, owner).hex(), 16)


def send_tx(w3, acct, to, data, gas):
    """Simulate (eth_call) then sign+broadcast. Waits for receipt. Returns tx hash hex."""
    tx = {
        "from": acct.address,
        "to": to,
        "data": data,
        "nonce": w3.eth.get_transaction_count(acct.address),
        "gas": gas,
        "maxFeePerGas": int(w3.eth.gas_price * 2),
        "maxPriorityFeePerGas": w3.to_wei(0.001, "gwei"),
        "chainId": CHAIN_ID,
    }
    w3.eth.call({"from": acct.address, "to": tx["to"], "data": data})  # simulate; raises on revert
    signed = acct.sign_transaction(tx)
    h = w3.eth.send_raw_transaction(signed.raw_transaction)
    log.info(f"tx broadcast: {h.hex()}")
    for _ in range(40):
        try:
            rc = w3.eth.get_transaction_receipt(h)
            if rc.status != 1:
                raise SystemExit(f"tx REVERTED: {h.hex()}")
            log.info(f"tx confirmed: block={rc.blockNumber} gas={rc.gasUsed}")
            return h.hex()
        except SystemExit:
            raise
        except Exception:
            time.sleep(3)
    raise SystemExit(f"tx receipt timeout: {h.hex()}")


def vvv_price() -> float:
    """VVV/USD price. CoinGecko first, DexScreener fallback.
    To use a different price source, add its URL here — anything returning a float."""
    for url in [
        "https://api.coingecko.com/api/v3/simple/price?ids=venice-token&vs_currencies=usd",
        "https://api.dexscreener.com/latest/dex/tokens/" + VVV_TOKEN,
    ]:
        try:
            d = json.load(urllib.request.urlopen(url, timeout=15))
            if "venice-token" in d:
                return float(d["venice-token"]["usd"])
            for p in d.get("pairs", []):
                if p.get("chainId") == "base":
                    return float(p["priceUsd"])
        except Exception:
            continue
    raise SystemExit("Could not fetch VVV price")


def resolve_pool(w3) -> str:
    """Look up the VVV/USDC pool at the configured fee tier from the Uniswap V3 factory."""
    sel = w3.keccak(text="getPool(address,address,uint24)")[:4].hex()
    data = (
        "0x"
        + sel
        + VVV_TOKEN[2:].lower().zfill(64)
        + USDC_TOKEN[2:].lower().zfill(64)
        + hex(VVV_USDC_FEE)[2:].zfill(64)
    )
    raw = eth_call(w3, V3_FACTORY, data, SWAP_ROUTER).hex()
    if int(raw, 16) == 0:
        raise SystemExit(f"No Uniswap V3 pool for VVV/USDC at fee={VVV_USDC_FEE} — set VVV_USDC_FEE correctly")
    return Web3.to_checksum_address("0x" + raw[-40:])


def _path_bytes() -> str:
    """Hex path string for VVV->USDC at the configured fee tier (token(20B) + fee(3B) + token(20B)).
    Multi-hop: chain more segments, e.g. VVV + fee1 + WETH + fee2 + USDC."""
    return VVV_TOKEN[2:].lower() + hex(VVV_USDC_FEE)[2:].zfill(6) + USDC_TOKEN[2:].lower()


def quote_swap(w3, amount_vvv_raw: int) -> int:
    """Quote VVV->USDC via QuoterV2.quoteExactInput(bytes path, uint256 amountIn).
    Note: quoteExactInputSingle reverts on Base for some pools; the path variant is reliable."""
    sel = w3.keccak(text="quoteExactInput(bytes,uint256)")[:4].hex()
    path = _path_bytes()
    data = (
        "0x" + sel
        + hex(0x40)[2:].zfill(64)               # bytes offset
        + hex(amount_vvv_raw)[2:].zfill(64)     # amountIn
        + hex(len(path) // 2)[2:].zfill(64)     # path length in bytes
        + path
    )
    raw = eth_call(w3, QUOTER_V2, data, SWAP_ROUTER).hex()
    if raw.startswith("0x"):
        raw = raw[2:]
    # QuoterV2 returns a 4-word struct (amountOut, sqrtPriceX96After, ticksCrossed, gasEstimate);
    # amountOut is the first word.
    return int(raw[:64], 16)


def swap_vvv_for_usdc(w3, signer, amount_vvv_raw: int, pool: str, dry: bool):
    """Swap VVV->USDC via Uniswap V3 exactInput on SwapRouter02.
    Returns None in dry-run, else swap tx hash."""
    if dry:
        log.info(f"[dry] would approve + swap {amount_vvv_raw / 1e18:.6f} VVV on pool {pool}")
        return None

    # 1. ERC20 approve router (idempotent: only when allowance insufficient)
    if erc20_allowance(w3, VVV_TOKEN, signer.address, SWAP_ROUTER) < amount_vvv_raw:
        sel = w3.keccak(text="approve(address,uint256)")[:4].hex()
        data = "0x" + sel + SWAP_ROUTER[2:].lower().zfill(64) + hex(amount_vvv_raw)[2:].zfill(64)
        send_tx(w3, signer, VVV_TOKEN, data, gas=60000)

    # 2. exactInput((bytes path, address recipient, uint256 deadline,
    #               uint256 amountIn, uint256 amountOutMinimum))
    deadline = int(time.time()) + 600
    min_out = int(quote_swap(w3, amount_vvv_raw) * (1 - SLIPPAGE_BPS / 10000))
    path = _path_bytes()
    sel = w3.keccak(text="exactInput((bytes,address,uint256,uint256,uint256))")[:4].hex()
    data = (
        "0x" + sel
        + hex(0x20)[2:].zfill(64)                    # tuple offset
        + hex(0xA0)[2:].zfill(64)                    # bytes path offset within tuple
        + signer.address[2:].lower().zfill(64)       # recipient
        + hex(deadline)[2:].zfill(64)
        + hex(amount_vvv_raw)[2:].zfill(64)
        + hex(min_out)[2:].zfill(64)
        + hex(len(path) // 2)[2:].zfill(64)          # path length in bytes
        + path
        + "0" * ((-(len(path) // 2)) % 32 * 2)       # pad path to 32-byte boundary
    )
    return send_tx(w3, signer, SWAP_ROUTER, data, gas=300000)


class X402BalanceProvider:
    """Reads the funder's x402 credit balance.

    Default implementation targets Venice (GET /x402/balance/{wallet}, SIWE-signed).
    TO SWITCH PROVIDERS: either
      (a) subclass/replace get() with the provider's balance endpoint (most x402
          providers follow the same SIWE pattern — swap the URL and message domain), or
      (b) make get() return -1.0 and delete the --topup balance gate; topup.mjs will
          still settle correctly against any x402 provider via TOPUP_BASE_URL.
    The SIWE message below includes the provider host in the domain line — some
    providers reject a mismatched domain, so update it when changing TOPUP_BASE_URL.
    """

    def __init__(self, helper_path: str):
        self.helper_path = helper_path

    def get(self) -> float:
        try:
            r = subprocess.run(
                [sys.executable, self.helper_path, "balance"],
                capture_output=True, text=True, timeout=60,
            )
            return float(json.loads(r.stdout)["data"]["balanceUsd"])
        except Exception:
            return -1.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true", help="Execute real transactions (default dry-run)")
    ap.add_argument("--topup", action="store_true", help="Also top up x402 credit when below TOPUP_THRESHOLD")
    ap.add_argument("--min-usd", type=float, default=float(os.environ.get("MIN_USD", "12.0")),
                    help="Minimum pending-reward USD value before converting (higher = fewer, larger conversions)")
    args = ap.parse_args()

    setup_logging()
    dry = not args.execute
    errors = []
    result = {"ts": datetime.now(timezone.utc).isoformat(), "dry": dry, "errors": errors}

    if not FUNDER_ADDR or not EXPECTED_MAIN:
        raise SystemExit("FUNDER_ADDR and EXPECTED_MAIN must be set (env or .env)")

    signer = load_key("VVV_SIGNER_KEY")
    if signer.address.lower() != EXPECTED_MAIN.lower():
        raise SystemExit("VVV_SIGNER_KEY derives to unexpected address — aborting")

    w3 = get_w3()
    main_addr = signer.address
    pool = resolve_pool(w3)
    log.info(f"using VVV/USDC pool {pool} (fee={VVV_USDC_FEE})")

    # --- Step 1: gate check ---
    sel = w3.keccak(text="pendingRewards(address)")[:4].hex()
    pend_hex = eth_call(w3, STAKING_V2, "0x" + sel + main_addr[2:].lower().zfill(64), main_addr)
    pending_vvv = int(pend_hex.hex(), 16) / 1e18
    price = vvv_price()
    pending_usd = pending_vvv * price
    gas_eth = w3.eth.get_balance(main_addr) / 1e18
    result.update(
        {"pending_vvv": pending_vvv, "vvv_usd": price, "pending_usd": round(pending_usd, 4), "gas_eth": gas_eth}
    )
    log.info(f"pending={pending_vvv:.6f} VVV (${pending_usd:.2f}) gas={gas_eth:.5f} ETH")

    # --- Funder gas top-up: runs on EVERY invocation, before any gating ---
    funder_eth = w3.eth.get_balance(Web3.to_checksum_address(FUNDER_ADDR)) / 1e18
    result["funder_eth"] = round(funder_eth, 5)
    GAS_FLOOR, GAS_TOPUP = 0.002, 0.005
    if funder_eth < GAS_FLOOR:
        if gas_eth < GAS_TOPUP + MIN_GAS_ETH:
            log.warning(f"main gas too low to top up funder: {gas_eth:.5f} ETH")
            errors.append("gas_topup_skipped_main_low")
        elif dry:
            log.info(f"[dry] would send {GAS_TOPUP} ETH to funder (has {funder_eth:.5f})")
        else:
            gas_tx = send_tx(w3, signer, Web3.to_checksum_address(FUNDER_ADDR), "0x", gas=21000)
            result["gas_topup_tx"] = gas_tx
            log.info(f"funder gas topped up to ~{funder_eth + GAS_TOPUP:.5f} ETH")
    else:
        log.info(f"funder gas OK: {funder_eth:.5f} ETH")

    if pending_usd < args.min_usd:
        log.info("below threshold, nothing to do")
        result["outcome"] = "below_threshold"
        print(json.dumps(result))
        return
    if gas_eth < MIN_GAS_ETH:
        log.error(f"insufficient gas: {gas_eth:.5f} ETH < {MIN_GAS_ETH}")
        errors.append("insufficient_gas")
        result["outcome"] = "aborted"
        print(json.dumps(result))
        sys.exit(1)

    pre_usdc = erc20_balance(w3, USDC_TOKEN, main_addr, 6)
    result["pre_usdc"] = pre_usdc

    # --- Step 2: claim ---
    claim_data = "0x" + w3.keccak(text="claim()")[:4].hex()
    if dry:
        log.info(f"[dry] would claim {pending_vvv:.6f} VVV (cooldown does not block reward claims)")
    else:
        claim_tx = send_tx(w3, signer, STAKING_V2, claim_data, gas=200000)
        result["claim_tx"] = claim_tx

    # --- Step 3: swap (Uniswap V3) ---
    amount_raw = int(pending_vvv * 1e18)
    swap_tx = swap_vvv_for_usdc(w3, signer, amount_raw, pool, dry)

    # --- Step 4: balance-diff verification ---
    if dry:
        delta = 0.0
        log.info("[dry] swap step complete (no-op); delta verification skipped")
    else:
        post_usdc = erc20_balance(w3, USDC_TOKEN, main_addr, 6)
        delta = post_usdc - pre_usdc
        quoted = quote_swap(w3, amount_raw) / 1e6
        result["delta_usdc"] = round(delta, 6)
        log.info(f"USDC delta: {delta:.4f} (quoted {quoted:.4f})")
        expected_max = quoted * DELTA_TOLERANCE
        if not (0 < delta <= expected_max):
            log.error(f"delta verification FAILED: {delta:.4f} not in (0, {expected_max:.4f}] — NOT forwarding")
            errors.append("delta_verification_failed")
            result["outcome"] = "aborted_no_forward"
            print(json.dumps(result))
            sys.exit(1)

    # --- Step 5: forward exact delta ---
    if dry:
        log.info(f"[dry] would forward {pending_usd:.4f} USDC (post-swap delta) to funder")
    else:
        delta_raw = round(delta * 1e6)
        sel = w3.keccak(text="transfer(address,uint256)")[:4].hex()
        data = "0x" + sel + FUNDER_ADDR[2:].lower().zfill(64) + hex(delta_raw)[2:].zfill(64)
        fwd_tx = send_tx(w3, signer, USDC_TOKEN, data, gas=80000)
        result["forward_tx"] = fwd_tx
        funder_bal = erc20_balance(w3, USDC_TOKEN, FUNDER_ADDR, 6)
        result["funder_usdc_after"] = funder_bal
        log.info(f"funder USDC after: {funder_bal:.4f}")

    # --- Step 6: optional x402 top-up ---
    if args.topup:
        provider = X402BalanceProvider(X402_HELPER)
        bal = provider.get()
        log.info(f"x402 credit: ${bal:.2f}")
        if 0 <= bal < TOPUP_THRESHOLD and not dry:
            # topup.mjs reads FUNDER_KEY + TOPUP_BASE_URL from env; pass through from .env if needed
            env = os.environ.copy()
            if not env.get("FUNDER_KEY"):
                env["FUNDER_KEY"] = load_key("FUNDER_KEY").key.hex()
            r = subprocess.run(["node", TOPUP_SCRIPT, str(TOPUP_AMOUNT)],
                               capture_output=True, text=True, env=env, timeout=120)
            log.info(f"topup: {r.stdout[-200:]} {r.stderr[-200:]}")
            result["topup"] = {"amount": TOPUP_AMOUNT, "ok": "success" in (r.stdout + r.stderr).lower()}
        elif dry:
            log.info(f"[dry] would top up x402 (current ${bal:.2f})")

    result["outcome"] = "dry_run_complete" if dry else "success"
    log.info("RUN COMPLETE")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
