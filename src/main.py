from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from src.config import settings
from src.agents.yield_optimizer import YieldOptimizer, SUPPORTED_CHAINS
from src.protocols.a2a import handle_a2a_request
from src.utils.logging import ExecutionLogger

app = FastAPI(title="Chado Yield Optimizer", version="0.2.0")
optimizer = YieldOptimizer()
logger = ExecutionLogger()


class OptimizeRequest(BaseModel):
    current_pool: str | None = None
    min_tvl: float = 100_000
    chains: list[str] | None = None
    risk_filter: str = "high"
    position_size: float = 10_000


class PoolDict(BaseModel):
    name: str
    address: str
    apy: float
    tvl: float
    source: str
    chain: str = "ethereum"
    risk: str = "medium"
    base_apy: float = 0.0
    reward_apy: float = 0.0
    gas_cost_usd: float = 15.0
    extra: dict = {}


class YieldsResponse(BaseModel):
    pools: list[dict]
    total_pools_found: int
    best_yield: dict | None
    source_summary: dict = {}
    chain_summary: dict = {}
    chains_queried: list[str] = []


class OptimizeResponse(BaseModel):
    pools: list[dict]
    total_pools_found: int
    best_yield: dict | None
    current_pool: dict | None
    current_apy: float
    strategy: str
    rationale: str
    rebalance_target: dict | None
    rebalance_needed: bool
    threshold: float
    source_summary: dict = {}
    chain_summary: dict = {}
    chains_queried: list[str] = []
    execution_log_id: str = ""


class ChainInfo(BaseModel):
    name: str
    chain_id: int
    label: str
    gas_cost_usd: float


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "agent": "chado-yield-optimizer",
        "version": "0.2.0",
        "sources": ["scrvusd", "llamalend", "crvusd_mint", "boosted_lp"],
        "chains": [c["name"] for c in SUPPORTED_CHAINS],
    }


@app.get("/api/v1/chains", response_model=list[ChainInfo])
async def chains():
    """List supported chains with metadata."""
    return [ChainInfo(**c) for c in optimizer.get_supported_chains()]


@app.get("/api/v1/yields", response_model=YieldsResponse)
async def yields(
    chain: str | None = None,
    source: str | None = None,
    min_tvl: float = 0,
):
    """Fetch current crvUSD yield opportunities across all sources and chains."""
    log_entry = logger.start("yields", {"chain": chain, "source": source})

    task_params = {"min_tvl": min_tvl}
    if chain:
        task_params["chains"] = [chain]

    result = await optimizer.run(task_params)
    logger.end(log_entry, result)

    pools = result["pools"]
    if source:
        pools = [p for p in pools if p.get("source") == source]

    return YieldsResponse(
        pools=pools,
        total_pools_found=result["total_pools_found"],
        best_yield=result["best_yield"],
        source_summary=result.get("source_summary", {}),
        chain_summary=result.get("chain_summary", {}),
        chains_queried=result.get("chains_queried", []),
    )


@app.post("/api/v1/optimize", response_model=OptimizeResponse)
async def optimize(req: OptimizeRequest):
    """Optimize yield: compare current position against best opportunities."""
    log_entry = logger.start("optimize", {
        "current_pool": req.current_pool,
        "chains": req.chains,
        "risk_filter": req.risk_filter,
    })
    result = await optimizer.run({
        "current_pool": req.current_pool,
        "min_tvl": req.min_tvl,
        "chains": req.chains or [c["name"] for c in SUPPORTED_CHAINS],
        "risk_filter": req.risk_filter,
        "position_size": req.position_size,
    })
    logger.end(log_entry, result)

    return OptimizeResponse(
        pools=result["pools"],
        total_pools_found=result["total_pools_found"],
        best_yield=result["best_yield"],
        current_pool=result["current_pool"],
        current_apy=result["current_apy"],
        strategy=result["strategy"],
        rationale=result.get("rationale", ""),
        rebalance_target=result["rebalance_target"],
        rebalance_needed=result["rebalance_needed"],
        threshold=result["threshold"],
        source_summary=result.get("source_summary", {}),
        chain_summary=result.get("chain_summary", {}),
        chains_queried=result.get("chains_queried", []),
        execution_log_id=log_entry["id"],
    )


@app.post("/a2a")
async def a2a_endpoint(request: Request):
    """A2A (Agent-to-Agent) JSON-RPC 2.0 endpoint."""
    data = await request.json()
    handlers = {
        "optimize": lambda params: optimizer.run(params),
        "yields": lambda params: optimizer.run({**params, "min_tvl": 0}),
        "status": lambda _: {
            "optimizer": optimizer.status(),
            "chains": [c["name"] for c in SUPPORTED_CHAINS],
        },
        "chains": lambda _: optimizer.get_supported_chains(),
    }
    result = await handle_a2a_request(data, handlers)
    return JSONResponse(content=result)


@app.get("/.well-known/agent.json")
async def agent_card():
    """Serve agent.json for A2A discovery."""
    return {
        "format": "Registration-v1",
        "name": "Chado Yield Optimizer",
        "description": (
            "Multi-chain crvUSD yield optimizer. "
            "Sources: scrvUSD savings, LlamaLend deposit, Convex/StakeDAO boosted LP. "
            "Chains: Ethereum, Arbitrum, Optimism, Fraxtal."
        ),
        "version": "0.2.0",
        "services": [
            {"type": "api", "url": f"http://localhost:{settings.port}/api/v1"}
        ],
        "supportedTrust": ["execution-logs"],
        "capabilities": {
            "yield_sources": ["scrvusd", "llamalend", "crvusd_mint", "boosted_lp"],
            "chains": [c["name"] for c in SUPPORTED_CHAINS],
            "risk_levels": ["low", "medium", "high"],
        },
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8717)
