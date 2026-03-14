# Chado Yield Optimizer

Autonomous multi-chain crvUSD yield optimizer. Synthesis Hackathon 2026 — Olas Pearl track.

## Architecture

```
YieldOptimizer Agent
  ├── scrvUSD Savings Vault    — native crvUSD savings rate
  ├── LlamaLend Markets        — crvUSD lending across chains
  ├── Boosted LP (Convex)      — boosted Curve LP positions
  └── Gnosis Safe Wallet       — on-chain execution via Safe multisig
```

Monitors yield opportunities across Ethereum, Arbitrum, and Fraxtal. Recommends rebalancing when yield improvement exceeds configurable threshold (default 5%).

ERC-8004 identity on Ethereum Mainnet. A2A protocol for agent-to-agent communication.

## Run

```bash
pip install -r requirements.txt
uvicorn src.main:app --host 0.0.0.0 --port 8717
```

## API

- `GET /health` — healthcheck
- `GET /api/v1/yields` — list current crvUSD yield opportunities
- `GET /api/v1/chains` — supported chains
- `POST /api/v1/optimize` — optimize yield for a given position
- `POST /api/v1/wallet/deposit` — deposit crvUSD into a pool
- `POST /api/v1/wallet/withdraw` — withdraw from a pool
- `GET /api/v1/wallet/status` — wallet balances and Safe status
- `POST /a2a` — A2A JSON-RPC 2.0 endpoint
- `GET /.well-known/agent.json` — agent card for A2A discovery

## ERC-8004

- IdentityRegistry: `0x8004A169FB4a3325136EB29fA0ceB6D2e539a432` (Ethereum Mainnet)
- Token ID: #28626

## Frontend

Live: [llama.box/yield-optimizer](https://llama.box/yield-optimizer/)

## License

MIT
