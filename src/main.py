from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from src.config import settings, wallet_settings
from src.agents.yield_optimizer import YieldOptimizer, SUPPORTED_CHAINS
from src.protocols.a2a import handle_a2a_request
from src.utils.logging import ExecutionLogger

app = FastAPI(title="Chado Yield Optimizer", version="0.3.0")
optimizer = YieldOptimizer()
logger = ExecutionLogger()

# ── Lazy wallet initialization ─────────────────────────────────────
# Wallet components are initialized on first use to avoid import errors
# if web3 is not installed or .env is missing wallet keys.
_safe_manager = None
_strategy_executor = None


def _get_safe_manager():
    global _safe_manager
    if _safe_manager is None:
        if not wallet_settings.is_configured:
            raise HTTPException(
                status_code=503,
                detail="Wallet not configured. Set AGENT_PRIVATE_KEY and SAFE_ADDRESS in .env",
            )
        from src.wallet.safe_manager import SafeManager

        _safe_manager = SafeManager(
            rpc_url=wallet_settings.rpc_url,
            private_key=wallet_settings.agent_private_key,
            safe_address=wallet_settings.safe_address,
        )
    return _safe_manager


def _get_strategy_executor():
    global _strategy_executor
    if _strategy_executor is None:
        from src.wallet.strategies import StrategyExecutor

        _strategy_executor = StrategyExecutor(_get_safe_manager())
    return _strategy_executor


# ── Request/Response Models ────────────────────────────────────────


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


class DepositRequest(BaseModel):
    pool_address: str = "0x0655977FEb2f289A4aB78af67BAB0d17aAb84367"  # scrvUSD default
    amount: float  # crvUSD amount (human-readable, e.g. 10.0)
    pool_type: str = "scrvusd"


class WithdrawRequest(BaseModel):
    pool_address: str = "0x0655977FEb2f289A4aB78af67BAB0d17aAb84367"  # scrvUSD default
    amount: float  # amount to withdraw (human-readable)
    pool_type: str = "scrvusd"
    is_shares: bool = False  # if True, amount is in shares (scrvUSD), else assets (crvUSD)


# ── Existing Endpoints ─────────────────────────────────────────────


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "agent": "chado-yield-optimizer",
        "version": "0.3.0",
        "sources": ["scrvusd", "llamalend", "crvusd_mint", "boosted_lp"],
        "chains": [c["name"] for c in SUPPORTED_CHAINS],
        "wallet_configured": wallet_settings.is_configured,
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


# ── Wallet Endpoints ──────────────────────────────────────────────


@app.get("/api/v1/wallet/status")
async def wallet_status():
    """Agent wallet status: Safe info, configuration, readiness."""
    if not wallet_settings.is_configured:
        return {
            "configured": False,
            "error": "Wallet not configured. Set AGENT_PRIVATE_KEY and SAFE_ADDRESS.",
        }

    try:
        safe = _get_safe_manager()
        info = safe.get_safe_info()
        return {
            "configured": True,
            "safe": info,
            "eoa_address": wallet_settings.agent_eoa_address,
            "rpc_url": wallet_settings.rpc_url,
        }
    except Exception as e:
        return {"configured": True, "error": str(e)}


@app.get("/api/v1/wallet/balances")
async def wallet_balances():
    """Get current Safe balances (ETH, crvUSD, scrvUSD)."""
    safe = _get_safe_manager()
    return safe.get_balances()


@app.post("/api/v1/wallet/deposit")
async def wallet_deposit(req: DepositRequest):
    """Deposit crvUSD into a yield pool through the Safe.

    Default pool: scrvUSD savings vault.
    """
    log_entry = logger.start("wallet_deposit", {
        "pool": req.pool_address,
        "amount": req.amount,
        "pool_type": req.pool_type,
    })

    safe = _get_safe_manager()
    from web3 import Web3

    amount_wei = Web3.to_wei(req.amount, "ether")
    result = safe.deposit_to_pool(
        pool_address=req.pool_address,
        amount_wei=amount_wei,
        pool_type=req.pool_type,
    )

    logger.end(log_entry, {"success": result.success, "tx_hash": result.tx_hash})

    if not result.success:
        raise HTTPException(status_code=400, detail=result.error)

    return {
        "success": result.success,
        "tx_hash": result.tx_hash,
        "gas_used": result.gas_used,
        "amount": str(req.amount),
        "pool": req.pool_address,
    }


@app.post("/api/v1/wallet/withdraw")
async def wallet_withdraw(req: WithdrawRequest):
    """Withdraw from a yield pool back to Safe.

    Default pool: scrvUSD savings vault.
    """
    log_entry = logger.start("wallet_withdraw", {
        "pool": req.pool_address,
        "amount": req.amount,
        "pool_type": req.pool_type,
    })

    safe = _get_safe_manager()
    from web3 import Web3

    amount_wei = Web3.to_wei(req.amount, "ether")
    result = safe.withdraw_from_pool(
        pool_address=req.pool_address,
        amount_wei=amount_wei,
        pool_type=req.pool_type,
        is_shares=req.is_shares,
    )

    logger.end(log_entry, {"success": result.success, "tx_hash": result.tx_hash})

    if not result.success:
        raise HTTPException(status_code=400, detail=result.error)

    return {
        "success": result.success,
        "tx_hash": result.tx_hash,
        "gas_used": result.gas_used,
        "amount": str(req.amount),
        "pool": req.pool_address,
    }


@app.post("/api/v1/wallet/rebalance")
async def wallet_rebalance(req: OptimizeRequest):
    """Run optimizer and auto-execute the recommended strategy.

    Combines yield optimization with on-chain execution:
    1. Fetches current yields across all sources
    2. Determines optimal strategy (hold/enter/rebalance)
    3. Executes the strategy through the Safe
    """
    log_entry = logger.start("wallet_rebalance", {
        "current_pool": req.current_pool,
        "chains": req.chains,
    })

    # Step 1: Get optimizer recommendation
    result = await optimizer.run({
        "current_pool": req.current_pool,
        "min_tvl": req.min_tvl,
        "chains": req.chains or [c["name"] for c in SUPPORTED_CHAINS],
        "risk_filter": req.risk_filter,
        "position_size": req.position_size,
    })

    # Step 2: Execute strategy
    executor = _get_strategy_executor()
    rebalance_result = executor.auto_rebalance(result)

    logger.end(log_entry, rebalance_result.to_dict())

    return {
        "optimizer": {
            "strategy": result["strategy"],
            "rationale": result.get("rationale", ""),
            "best_yield": result["best_yield"],
            "current_apy": result["current_apy"],
        },
        "execution": rebalance_result.to_dict(),
    }


# ── A2A + Agent Card ──────────────────────────────────────────────


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
            "wallet_configured": wallet_settings.is_configured,
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
            "Multi-chain crvUSD yield optimizer with Gnosis Safe wallet execution. "
            "Sources: scrvUSD savings, LlamaLend deposit, Convex/StakeDAO boosted LP. "
            "Chains: Ethereum, Arbitrum, Optimism, Fraxtal."
        ),
        "version": "0.3.0",
        "services": [
            {"type": "api", "url": f"http://localhost:{settings.port}/api/v1"}
        ],
        "supportedTrust": ["execution-logs"],
        "capabilities": {
            "yield_sources": ["scrvusd", "llamalend", "crvusd_mint", "boosted_lp"],
            "chains": [c["name"] for c in SUPPORTED_CHAINS],
            "risk_levels": ["low", "medium", "high"],
            "wallet": {
                "type": "gnosis_safe",
                "supported_actions": ["deposit", "withdraw", "rebalance"],
                "supported_pools": ["scrvusd"],
            },
        },
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8717)
