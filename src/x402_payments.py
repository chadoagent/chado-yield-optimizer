"""x402 payment middleware configuration for crvUSD Yield Optimizer API.

Uses Coinbase x402 protocol for pay-per-request API monetization.
Payments in USDC on Base chain.

Pricing (per request):
  /api/pools, /api/best-yield: $0.001
  /api/risk-score:             $0.005
  /api/rebalance:              $0.01
  /a2a, /a2a/stream:           $0.01
"""

from __future__ import annotations

import os

from x402.http import FacilitatorConfig, HTTPFacilitatorClient, PaymentOption
from x402.http.middleware.fastapi import PaymentMiddlewareASGI
from x402.http.types import RouteConfig
from x402.mechanisms.evm.exact import ExactEvmServerScheme
from x402.server import x402ResourceServer

# ── Configuration ───────────────────────────────────────────────

PAY_TO = os.environ.get("X402_WALLET_ADDRESS", "0x6a1175D0EA0e6817786Ce51F1C4F3294F907f410")

# Base Sepolia testnet = eip155:84532 (x402.org facilitator supports this)
# Base mainnet (eip155:8453) requires Coinbase CDP API keys
NETWORK = os.environ.get("X402_NETWORK", "eip155:84532")

# Facilitator: x402.org (free, supports Base Sepolia)
# For mainnet: https://api.cdp.coinbase.com/platform/v2/x402 (requires CDP keys)
FACILITATOR_URL = os.environ.get(
    "X402_FACILITATOR_URL",
    "https://x402.org/facilitator",
)

# ── Pricing tiers ───────────────────────────────────────────────

# Paid endpoints (analytics & actions)
ROUTE_PRICES = {
    "GET /api/risk-score/{pool_id}": "$0.005",
    "POST /api/rebalance": "$0.01",
    "POST /a2a": "$0.01",
    "POST /a2a/stream": "$0.01",
}

# Free endpoints — read-only data is free, analytics/actions are paid
FREE_ENDPOINTS = {
    "GET /health",
    "GET /api/pools",
    "GET /api/best-yield",
    "GET /api/pricing",
    "GET /.well-known/agent.json",
    "GET /docs",
    "GET /openapi.json",
}


def create_x402_middleware_config():
    """Create x402 middleware configuration for FastAPI."""
    facilitator = HTTPFacilitatorClient(
        FacilitatorConfig(url=FACILITATOR_URL)
    )

    server = x402ResourceServer(facilitator)
    server.register(NETWORK, ExactEvmServerScheme())

    routes: dict[str, RouteConfig] = {}

    for route, price in ROUTE_PRICES.items():
        routes[route] = RouteConfig(
            accepts=[
                PaymentOption(
                    scheme="exact",
                    pay_to=PAY_TO,
                    price=price,
                    network=NETWORK,
                ),
            ],
            mime_type="application/json",
            description=f"crvUSD Yield Optimizer - {route}",
        )

    return routes, server
