from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from src.config import settings
from src.agents.yield_optimizer import YieldOptimizer
from src.protocols.a2a import handle_a2a_request
from src.utils.logging import ExecutionLogger

app = FastAPI(title="Chado Yield Optimizer", version="0.1.0")
optimizer = YieldOptimizer()
logger = ExecutionLogger()


class OptimizeRequest(BaseModel):
    current_pool: str | None = None
    min_tvl: float = 100_000


class YieldsResponse(BaseModel):
    pools: list[dict]
    total_pools_found: int
    best_yield: dict | None


class OptimizeResponse(BaseModel):
    pools: list[dict]
    total_pools_found: int
    best_yield: dict | None
    current_pool: dict | None
    current_apy: float
    strategy: str
    rebalance_target: dict | None
    rebalance_needed: bool
    threshold: float
    execution_log_id: str


@app.get("/health")
async def health():
    return {"status": "ok", "agent": "chado-yield-optimizer", "version": "0.1.0"}


@app.get("/api/v1/yields", response_model=YieldsResponse)
async def yields():
    """Fetch current crvUSD yield opportunities."""
    log_entry = logger.start("yields")
    result = await optimizer.run({"min_tvl": 0})
    logger.end(log_entry, result)
    return YieldsResponse(
        pools=result["pools"],
        total_pools_found=result["total_pools_found"],
        best_yield=result["best_yield"],
    )


@app.post("/api/v1/optimize", response_model=OptimizeResponse)
async def optimize(req: OptimizeRequest):
    """Optimize yield: compare current position against best opportunities."""
    log_entry = logger.start("optimize", {"current_pool": req.current_pool})
    result = await optimizer.run({
        "current_pool": req.current_pool,
        "min_tvl": req.min_tvl,
    })
    logger.end(log_entry, result)
    return OptimizeResponse(
        pools=result["pools"],
        total_pools_found=result["total_pools_found"],
        best_yield=result["best_yield"],
        current_pool=result["current_pool"],
        current_apy=result["current_apy"],
        strategy=result["strategy"],
        rebalance_target=result["rebalance_target"],
        rebalance_needed=result["rebalance_needed"],
        threshold=result["threshold"],
        execution_log_id=log_entry["id"],
    )


@app.post("/a2a")
async def a2a_endpoint(request: Request):
    """A2A (Agent-to-Agent) JSON-RPC 2.0 endpoint."""
    data = await request.json()
    handlers = {
        "optimize": lambda params: optimizer.run(params),
        "yields": lambda _: optimizer.run({"min_tvl": 0}),
        "status": lambda _: {"optimizer": optimizer.status()},
    }
    result = await handle_a2a_request(data, handlers)
    return JSONResponse(content=result)


@app.get("/.well-known/agent.json")
async def agent_card():
    """Serve agent.json for A2A discovery."""
    return {
        "format": "Registration-v1",
        "name": "Chado Yield Optimizer",
        "description": "crvUSD yield monitoring and rebalancing agent",
        "services": [{"type": "api", "url": f"http://localhost:{settings.port}/api/v1"}],
        "supportedTrust": ["execution-logs"],
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8717)
