# x402 + Venice: how the payment rail works

This doc explains the technology behind the `--topup` step: what x402 is, how Venice
implements it, and how `vvv-auto-funder` uses it.

## The problem x402 solves

Traditional API payment = accounts, API keys, monthly subscriptions, minimum commits.
Agentic and machine-to-machine payments don't fit that mold well: a bot that needs one
inference call shouldn't have to register an account first.

x402 is an open protocol (originated by Coinbase) that revives HTTP status code
**402 Payment Required** as an actual payment mechanism:

```
Client                                Server (api.venice.ai)
  │  POST /x402/top-up                    │
  │ ─────────────────────────────────────▶│
  │  402 Payment Required                 │
  │  { accepts: [{ payTo, asset,          │
  │     maxAmountRequired, network }] }   │
  │ ◀─────────────────────────────────────│
  │  sign USDC payment (EIP-3009)         │
  │  POST again + X-PAYMENT header        │
  │ ─────────────────────────────────────▶│
  │  200 OK + receipt                     │
  │ ◀─────────────────────────────────────│
```

Key properties:

- **No account, no API key** — the payment itself is the authentication. A wallet
  signature *is* the identity.
- **Pay per call** — exact amounts, no subscriptions. A 402 response tells you the
  price; you pay it; you're in.
- **Settlement on Base** — payments are USDC transfers on Base (EIP-3009
  `transferWithAuthorization`), which lets a signer authorize a transfer without paying
  gas for the transfer itself (a relayer/facilitator submits it).

## Venice's x402 implementation

Venice exposes its inference API over the x402 rail with **x402 credits**:

1. **Fund a wallet with USDC on Base.** This wallet is your payment identity — nothing
   else is registered anywhere.
2. **Top up credits** — `POST /x402/top-up`. Venice replies `402` with the payment
   requirements (`payTo` address, USDC asset, amount). Your client signs an EIP-3009
   `transferWithAuthorization` over the USDC and retries with the signature in an
   `X-402-Payment` header. Venice settles it and credits your balance.
3. **Spend credits on inference** — chat/completion calls are paid from the credit
   balance, metered per request.
4. **Check balance** — `GET /x402/balance/{wallet}`, authenticated with a
   **SIWE-style** signed header (`X-Sign-In-With-X`): a standard
   "sign in with Ethereum" message, signed by the wallet, base64-encoded, with a
   ~4-minute expiry. No API key, just a fresh signature per request.

The relevant endpoints (all under `https://api.venice.ai/api/v1`):

| Endpoint | Auth | Purpose |
|---|---|---|
| `POST /x402/top-up` | x402 payment header | Discovery (402) then settle a credit top-up |
| `GET /x402/balance/{wallet}` | SIWE header | Current credit balance |
| `GET /x402/transactions/{wallet}` | SIWE header | Credit transaction history |

## How vvv-auto-funder fits in

The funder wallet holds USDC (and a little ETH for gas) and exists to keep the x402
credit balance topped up:

```
VVV staking rewards ──claim──▶ VVV tokens ──Uniswap V3──▶ USDC ──forward──▶ funder wallet
                                                                              │
                                                    --topup: sign transferWithAuthorization
                                                    and settle via X-402-Payment header
                                                                              ▼
                                                            Venice x402 credit balance
```

- `vvv_auto_funder.py --topup` checks the funder's credit balance via
  `venice_x402/x402_helper.py` (SIWE header). Below $10, it tops up $5 by running
  `venice_x402/topup.mjs`.
- `topup.mjs` does the full 402 dance: discovery → sign `transferWithAuthorization`
  (via the `x402` SDK's `createPaymentHeader`) → settle. The signature authorizes USDC
  to move from the funder wallet; Venice's facilitator submits it on-chain.

## Why this design

- **Self-custodial**: no exchange account, no custodial balance. The funder wallet is
  just an EOA with USDC.
- **Gasless payments**: EIP-3009 means the USDC transfer itself costs the funder no
  gas — only the small ETH reserve covers the *credit top-up* path's on-chain
  settlement (and `vvv_auto_funder.py` auto-tops the funder's ETH if it runs low).
- **Composable**: any agent holding USDC on Base can pay Venice (or any x402-enabled
  API) with two HTTP calls — that's the whole point of the protocol.

## References

- x402 protocol: <https://www.x402.org>
- EIP-3009 (`transferWithAuthorization`): <https://eips.ethereum.org/EIPS/eip-3009>
- SIWE (EIP-4361): <https://eips.ethereum.org/EIPS/eip-4361>
- Uniswap V3 on Base: <https://developers.uniswap.org/docs/protocols/v3/deployments/v3-base-deployments>
