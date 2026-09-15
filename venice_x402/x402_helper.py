#!/usr/bin/env python3
"""
venice_x402/x402_helper.py — Venice x402 wallet-credit helper.

Subcommands:
  balance                   GET /x402/balance/{wallet} via SIWE — prints JSON with data.balanceUsd
  transactions [--limit N]  GET /x402/transactions/{wallet}
  siwx                      Print a preview of a fresh X-Sign-In-With-X header (truncated)

Requires: FUNDER_KEY (or X402_SIGNER_VAR override) in env / .env next to this file's repo root.
Payments: USDC on Base (eip155:8453). Top-ups: use topup.mjs (real signing via x402 SDK).

Requires: pip install eth-account
"""
import base64
import json
import os
import secrets
import sys
import time
import urllib.request
import urllib.error

from eth_account import Account
from eth_account.messages import encode_defunct

VENICE_BASE = "https://api.venice.ai/api/v1"
CHAIN_ID = 8453
ENV_KEY = os.environ.get("X402_SIGNER_VAR", "FUNDER_KEY")

# Load repo-root .env if present
_ROOT_ENV = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
if os.path.exists(_ROOT_ENV):
    with open(_ROOT_ENV) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def load_key():
    v = os.environ.get(ENV_KEY)
    if not v:
        raise SystemExit(f"{ENV_KEY} not set (env or .env)")
    return Account.from_key(v)


_wallet_cache = {}


def load_key_wallet():
    if "w" not in _wallet_cache:
        _wallet_cache["w"] = load_key()
    return _wallet_cache["w"]


def make_siwx_header(wallet):
    """Build a fresh X-Sign-In-With-X header (SIWE-style, 4-min TTL)."""
    issued_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    nonce = secrets.token_hex(8)
    message = (
        "api.venice.ai wants you to sign in with your Ethereum account:\n"
        f"{wallet.address}\n\n"
        "Sign in to Venice AI\n\n"
        "URI: https://api.venice.ai\n"
        "Version: 1\n"
        f"Chain ID: {CHAIN_ID}\n"
        f"Nonce: {nonce}\n"
        f"Issued At: {issued_at}\n"
        f"Expiration Time: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() + 240))}"
    )
    signature = wallet.sign_message(encode_defunct(text=message)).signature.hex()
    if not signature.startswith("0x"):
        signature = "0x" + signature
    payload = {
        "address": wallet.address,
        "message": message,
        "signature": signature,
        "timestamp": int(time.time() * 1000),
        "chainId": CHAIN_ID,
    }
    return base64.b64encode(json.dumps(payload).encode()).decode()


def api(method, path):
    wallet = load_key_wallet()
    headers = {"X-Sign-In-With-X": make_siwx_header(wallet)}
    req = urllib.request.Request(f"{VENICE_BASE}{path}", headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.load(e)
        except Exception:
            return e.code, {"raw": e.read().decode()[:500]}


def cmd_balance():
    wallet = load_key_wallet()
    code, data = api("GET", f"/x402/balance/{wallet.address}")
    print(json.dumps(data, indent=2))


def cmd_transactions(limit):
    wallet = load_key_wallet()
    code, data = api("GET", f"/x402/transactions/{wallet.address}?limit={limit}")
    print(json.dumps(data, indent=2))


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return
    cmd = args[0]
    if cmd == "balance":
        cmd_balance()
    elif cmd == "transactions":
        limit = int(args[args.index("--limit") + 1]) if "--limit" in args else 20
        cmd_transactions(limit)
    elif cmd == "siwx":
        w = load_key_wallet()
        print(make_siwx_header(w)[:120] + "...(truncated preview)")
    else:
        print(f"Unknown command: {cmd}")


if __name__ == "__main__":
    main()
