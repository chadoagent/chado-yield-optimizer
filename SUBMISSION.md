# Chado Yield Optimizer

**Tagline:** Autonomous AI agent that finds and optimizes the best crvUSD yield opportunities across DeFi protocols

**Hackathon:** Synthesis (March 13-22, 2026)
**Track:** Olas Pearl -- Agent Integration via Olas SDK
**License:** MIT

---

## The Problem

crvUSD holders face a fragmented yield landscape. Opportunities are scattered across multiple protocols -- LlamaLend markets, scrvUSD savings vault, Convex boosted LPs, StakeDAO positions -- each with different APYs, risk profiles, and gas costs. Worse, these exist across multiple chains (Ethereum, Arbitrum, Optimism, Fraxtal), making manual comparison impractical.

Today a crvUSD holder must:
- Monitor 50+ yield pools across 4 chains manually
- Compare base APY vs reward APY vs gas costs vs bridge fees
- Assess smart contract risk for each protocol
- Execute multi-step transactions (approve, deposit, stake) through separate UIs
- Repeat this process regularly as rates shift

This is exactly the kind of repetitive, data-intensive, multi-step workflow that autonomous agents should handle.

## What Chado Yield Optimizer Does

Chado Yield Optimizer is an autonomous AI agent deployed on the Olas network that continuously monitors crvUSD yield opportunities and executes optimal strategies through a Gnosis Safe wallet.

The agent:
1. **Discovers** yield pools across LlamaLend, scrvUSD, Convex, and StakeDAO on 4 chains simultaneously
2. **Analyzes** each pool's risk-adjusted return (base APY + rewards - gas - bridge costs)
3. **Recommends** whether to hold, enter, or rebalance -- with full rationale
4. **Executes** deposit/withdraw/rebalance transactions through a Safe multisig
5. **Communicates** with other agents via the A2A protocol for composable DeFi strategies

## Key Features

- **57 yield pools** across 3 active chains (Ethereum, Arbitrum, Optimism)
- **Real-time APY comparison** from LlamaLend, Convex, StakeDAO, scrvUSD savings vault
- **Risk scoring** (Low/Medium/High) per pool based on protocol type and TVL
- **Deposit/Withdraw/Claim** via Gnosis Safe -- no private key exposure
- **Portfolio rebalancing simulation** with configurable threshold (default 5% improvement)
- **REST API** with full Swagger/OpenAPI documentation
- **A2A Protocol v1.0** support -- JSON-RPC 2.0 + SSE streaming for agent-to-agent communication
- **ERC-8004** on-chain identity on Ethereum mainnet
- **Docker CI/CD** via GitHub Actions -- automatic build and push to GHCR
- **Olas SDK compatible** -- standard healthcheck endpoint, file logging, key file support
- **Open-source** under MIT license

## Architecture

```
Chado Yield Optimizer Agent
  |
  +-- YieldOptimizer Engine
  |     +-- scrvUSD Fetcher        (Curve savings vault rate)
  |     +-- LlamaLend Fetcher      (multi-chain lending markets)
  |     +-- Boosted LP Fetcher     (Convex/StakeDAO positions)
  |     +-- Risk Scorer            (per-pool risk classification)
  |     +-- Rebalance Optimizer    (threshold-based strategy)
  |
  +-- Wallet Layer
  |     +-- SafeManager            (raw Web3 + Safe ABI, no safe-eth-py)
  |     +-- StrategyExecutor       (auto-rebalance orchestration)
  |
  +-- Protocol Layer
  |     +-- A2A v1.0               (JSON-RPC + SSE streaming)
  |     +-- REST API               (FastAPI + Swagger)
  |     +-- ERC-8004 Registry      (on-chain identity)
  |
  +-- Infrastructure
        +-- Docker + GHCR          (CI/CD via GitHub Actions)
        +-- Olas Service #57       (on-chain registration)
        +-- IPFS metadata          (agent/service/component hashes)
```

## On-chain Deployments

| Asset | ID | Registry |
|-------|-----|----------|
| Olas Service | #57 | ServiceRegistry `0x48b6af7B12C71f09e2fC8aF4855De4Ff54e775cA` |
| Olas Agent | #104 | AgentRegistry (Ethereum) |
| Olas Component | #316 | ComponentRegistry (Ethereum) |
| ERC-8004 Identity | #28683 | IdentityRegistry `0x8004A169FB4a3325136EB29fA0ceB6D2e539a432` |
| Gnosis Safe | -- | `0xe63e82C57F4e5dF84bF923bBDBA1A8DA30e753f0` |

## Challenges We Ran Into

### Olas Service Lifecycle
Deploying a service on the Olas registry is not a simple one-step process. The ServiceManager requires a strict lifecycle: `terminate() -> unbond() -> update() -> activateRegistration() -> registerAgents() -> deploy()`. Each step has specific prerequisites (token approvals, agent instance registration, correct multisig implementation). We wrote custom scripts (`service_lifecycle.py`) to handle the full flow programmatically.

### ERC-8004 Identity Registry
While integrating on-chain identity via ERC-8004, we discovered that the Olas marketplace frontend uses a `generateName()` function to derive display names from the token ID rather than reading the on-chain metadata URI. This means setting a custom name via `setAgentURI()` has no visible effect on the marketplace. We documented this discrepancy and filed [PR #335](https://github.com/valory-xyz/autonolas-frontend-mono/pull/335) to the Olas frontend repo proposing a fix.

### Gnosis Safe Without Dependencies
The standard `safe-eth-py` library pulls in heavy dependencies and has version conflicts with modern Python. We implemented Safe transaction signing and execution using raw Web3.py + the Safe contract ABI directly -- encoding `execTransaction()` calls manually with proper signature packing. This keeps the Docker image lean and avoids dependency hell.

### Multi-chain Data Aggregation
Fetching yield data from 4 chains simultaneously requires handling different RPC endpoints, varying response formats, and chain-specific quirks (Fraxtal's non-standard block structure, Arbitrum's L2 gas model). We use `httpx` with concurrent requests and per-chain error isolation so one chain's downtime doesn't block the others.

### Gas Optimization
Every on-chain operation on Ethereum mainnet costs real money. We implemented gas estimation, bridge cost modeling, and a minimum improvement threshold (5%) to ensure rebalancing recommendations are net-positive after all costs. The optimizer factors in gas for approve + deposit + potential withdrawal from current position.

## Technologies Used

| Category | Technologies |
|----------|-------------|
| Backend | Python 3.12, FastAPI, uvicorn, httpx, Pydantic |
| Blockchain | Web3.py, eth-account, Gnosis Safe (raw ABI) |
| Olas Integration | Olas SDK, ServiceRegistry, AgentRegistry, ComponentRegistry |
| Identity | ERC-8004 IdentityRegistry, IPFS metadata |
| Protocols | A2A Protocol v1.0 (JSON-RPC 2.0 + SSE), REST/OpenAPI |
| Infrastructure | Docker, GitHub Actions (CI/CD), GHCR |
| AI | Claude AI (development assistance) |
| Data Sources | Curve Finance API, LlamaLend API, DeFiLlama |

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/health` | Service health + version |
| `GET` | `/api/v1/yields` | All crvUSD yield opportunities |
| `GET` | `/api/v1/chains` | Supported chains |
| `POST` | `/api/v1/optimize` | Optimize yield for a position |
| `POST` | `/api/v1/wallet/deposit` | Deposit crvUSD into a pool |
| `POST` | `/api/v1/wallet/withdraw` | Withdraw from a pool |
| `GET` | `/api/v1/wallet/status` | Wallet/Safe status |
| `POST` | `/api/v1/wallet/rebalance` | Auto-execute optimal strategy |
| `POST` | `/a2a` | A2A JSON-RPC 2.0 |
| `GET` | `/.well-known/agent.json` | A2A Agent Card |

## Links

| Resource | URL |
|----------|-----|
| GitHub | https://github.com/chadoagent/chado-yield-optimizer |
| Live API | http://51.83.161.121:8717 |
| API Docs (Swagger) | http://51.83.161.121:8717/docs |
| Agent Card | http://51.83.161.121:8717/.well-known/agent.json |
| Frontend | https://llama.box/yield-optimizer/ |
| Olas Marketplace | https://marketplace.olas.network/ethereum/ai-agents/57 |
| Docker Image | `ghcr.io/chadoagent/chado-yield-optimizer:latest` |
| A2A Registry | https://a2aregistry.org/agents/0397efdd-0f43-4354-b305-593b5717ae69 |
| PR #335 (Olas contribution) | https://github.com/valory-xyz/autonolas-frontend-mono/pull/335 |

## How It Was Built

The project was built over 9 days during the Synthesis hackathon:

- **Days 1-2:** Core yield aggregation engine -- fetching and normalizing data from scrvUSD, LlamaLend, and Convex across multiple chains
- **Days 3-4:** Gnosis Safe wallet integration -- deposit, withdraw, and rebalance execution without external Safe libraries
- **Days 5-6:** Olas network deployment -- service registration, agent minting, component registration, IPFS metadata, full lifecycle management
- **Days 7-8:** A2A protocol implementation, ERC-8004 identity, Docker CI/CD, frontend dashboard
- **Day 9:** Testing, documentation, A2A Registry submission, Olas marketplace PR #335

## Team

**Chado Studio** -- a team of autonomous AI agents and their human collaborators, building tools for the Curve Finance ecosystem.

---

*Built for the Synthesis Hackathon 2026. Powered by Olas, Curve Finance, and Claude AI.*
