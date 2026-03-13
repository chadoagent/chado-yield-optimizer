# Chado Yield Optimizer

crvUSD yield monitoring and rebalancing agent. Synthesis Hackathon 2026 — Olas Pearl track.

## Architecture

```
YieldOptimizer
  ├── crvUSD Lending Markets  — fetch APYs from Curve prices API
  └── Stable Pool APYs        — fetch from Curve factory API
```

Compares current position against best yield opportunities across crvUSD lending markets and stable pools. Recommends rebalancing when yield improvement exceeds configurable threshold (default 5%).

ERC-8004 identity on Base chain. Execution logs for trust verification.

## Run

```bash
pip install -r requirements.txt
uvicorn src.main:app --host 0.0.0.0 --port 8717
```

## API

- `GET /health` — healthcheck
- `GET /api/v1/yields` — list current crvUSD yield opportunities
- `POST /api/v1/optimize` — optimize yield for a given position
- `POST /a2a` — A2A JSON-RPC 2.0 endpoint
- `GET /.well-known/agent.json` — agent card for A2A discovery

## ERC-8004

- IdentityRegistry: `0x8004A818BFB912233c491871b3d84c89A494BD9e` (Base)
- ReputationRegistry: `0x8004B663056A597Dffe9eCcC1965A193B7388713` (Base)

## License

MIT
