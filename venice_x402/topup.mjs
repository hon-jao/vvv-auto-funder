// venice_x402/topup.mjs — settle an x402 top-up with a signed USDC transferWithAuthorization
// Usage: node venice_x402/topup.mjs <amount_usd>
// Reads FUNDER_KEY from env (exported from .env by caller or the main script). Never prints the key.
// PROVIDER SWITCHING: set TOPUP_BASE_URL in .env/env to point at any x402-compatible provider
//   (default: https://api.venice.ai/api/v1). The provider must serve the standard x402 flow:
//   POST <base>/x402/top-up returns 402 + payment requirements; we sign USDC (Base) via the
//   x402 SDK and re-POST with the X-402-Payment header. Adjust the path below if the provider
//   uses a different endpoint name.
// Requires: npm install x402 viem/accounts in this directory (see venice_x402/package.json).

import { createPaymentHeader } from 'x402/client';
import { privateKeyToAccount } from 'viem/accounts';

const BASE = process.env.TOPUP_BASE_URL || 'https://api.venice.ai/api/v1';
const TOPUP_PATH = '/x402/top-up'; // change if the provider exposes a different endpoint
const X402_VERSION = 2;

async function main() {
  const amountUsd = parseFloat(process.argv[2]);
  if (!amountUsd || amountUsd < 5) {
    console.error('Usage: node venice_x402/topup.mjs <amount_usd> (min 5)');
    process.exit(1);
  }
  const pk = process.env.FUNDER_KEY;
  if (!pk) { console.error('FUNDER_KEY not set'); process.exit(1); }
  const pkHex = pk.startsWith('0x') ? pk : `0x${pk}`;
  const account = privateKeyToAccount(pkHex);
  console.error('Signer:', account.address);

  // 1. Discovery
  const discover = await fetch(`${BASE}${TOPUP_PATH}`, { method: 'POST' });
  let req;
  if (discover.status === 402) {
    const d = await discover.json();
    // x402 SDK v1.2 expects network names like "base", not CAIP-2 "eip155:8453"
    req = (d.accepts || []).find(a => a.network === 'eip155:8453' || a.network === 'base');
    if (req) req.network = 'base';
  } else {
    console.error('Unexpected discovery status', discover.status, await discover.text());
    process.exit(1);
  }
  if (!req) { console.error('No Base (eip155:8453) requirement in discovery'); process.exit(1); }
  console.error('Discovery OK — payTo:', req.payTo, 'asset:', req.asset);

  // 2. Sign payment header — override maxAmountRequired with our amount (base units, USDC 6 decimals)
  const amount = String(Math.round(amountUsd * 1e6));
  const requirements = { ...req, maxAmountRequired: amount };
  const header = await createPaymentHeader(account, X402_VERSION, requirements);
  console.error(`Payment header signed for $${amountUsd} (${amount} base units)`);

  // 3. Settle
  const settle = await fetch(`${BASE}${TOPUP_PATH}`, {
    method: 'POST',
    headers: { 'X-402-Payment': header },
  });
  const body = await settle.json();
  console.log(JSON.stringify({ status: settle.status, ...body }, null, 2));
  if (settle.status !== 200) process.exit(1);
}

main().catch(e => { console.error('FAILED:', e.message || e); process.exit(1); });
