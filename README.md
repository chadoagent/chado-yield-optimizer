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

Monitors yield opportunities across Ethereum, Arbitrum, Base, and Fraxtal. Recommends rebalancing when yield improvement exceeds configurable threshold (default 5%).

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

## On-Chain Identity (Base)

- Olas Service: [#436](https://registry.olas.network/base/services/436) (Base L2, Deployed & Active)
- Olas Agent: #105 (Ethereum L1)
- Olas Component: #317 (Ethereum L1)
- Safe: `0x68f3ffD33670c3ec1D8ff58cc53CcE066e3AE4e1`
- ServiceRegistryL2: `0x3C1fF68f5aa342D296d4DEe4Bb1cACCA912D95fE`
- Owner: `0xEfBf2fE01215DDBfac45a46CC1415fc02FFaDF0b`
- configHash: [IPFS](https://gateway.autonolas.tech/ipfs/f0170122074d749ff351521122b8733f6511e53fc012e641137f0ab6053b63e429d15fdbd)

## Frontend

Live: [llama.box/yield-optimizer](https://llama.box/yield-optimizer/)

## License

MIT
