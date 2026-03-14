"""REST API + A2A protocol for crvUSD Yield Optimizer.

Endpoints:
  GET  /api/pools              - list all pools with optional filters
  GET  /api/best-yield         - top N pools by APY
  GET  /api/risk-score/{id}    - risk assessment for a pool
  POST /api/rebalance          - simulate rebalance from current allocation
  POST /a2a                    - A2A JSON-RPC 2.0 endpoint (message/send, tasks/get, tasks/cancel)
  POST /a2a/stream             - A2A SSE streaming endpoint
  GET  /.well-known/agent.json - A2A Agent Card for service discovery

Run:
  python -m uvicorn src.api:app --host 0.0.0.0 --port 8717
  Swagger docs: http://localhost:8717/docs
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from src.auth import TIERS, check_endpoint_access, verify_api_key

from src.agents.yield_optimizer import (
    YieldOptimizer,
    PoolInfo,
    RiskLevel,
    YieldSource,
    SOURCE_RISK,
    SUPPORTED_CHAINS,
    BRIDGE_COSTS,
)
from src.protocols.a2a import (
    handle_a2a_request,
    stream_task_events,
    task_store,
    TaskState,
)

logger = logging.getLogger(__name__)

# ── App ──────────────────────────────────────────────────────────

app = FastAPI(
    title="crvUSD Yield Optimizer API",
    description=(
        "Multi-chain crvUSD yield optimizer with A2A protocol support. "
        "Sources: scrvUSD, LlamaLend, Convex, StakeDAO. "
        "Chains: Ethereum, Arbitrum, Optimism, Fraxtal. "
        "Supports Google A2A (Agent-to-Agent) protocol for agent interoperability."
    ),
    version="1.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── x402 Payment Middleware ──────────────────────────────────────
try:
    from src.x402_payments import create_x402_middleware_config
    from x402.http.middleware.fastapi import PaymentMiddlewareASGI
    x402_routes, x402_server = create_x402_middleware_config()
    # Pre-initialize to catch RouteConfigurationError at startup, not on first request
    x402_server.initialize()
    app.add_middleware(PaymentMiddlewareASGI, routes=x402_routes, server=x402_server)
    logger.info("x402 payment middleware enabled")
except Exception as e:
    logger.warning(f"x402 middleware not loaded (payments disabled): {e}")

optimizer = YieldOptimizer()

# ── Simple cache to avoid hammering APIs ─────────────────────────

_cache: dict[str, Any] = {"data": None, "ts": 0}
CACHE_TTL = 60  # seconds


async def _get_pools() -> list[dict]:
    """Fetch all pools, cached for CACHE_TTL seconds."""
    now = time.time()
    if _cache["data"] is not None and (now - _cache["ts"]) < CACHE_TTL:
        return _cache["data"]

    result = await optimizer.run({"min_tvl": 0, "risk_filter": "high"})
    pools = result.get("pools", [])
    _cache["data"] = pools
    _cache["ts"] = now
    return pools


def _pool_id(pool: dict) -> str:
    """Generate a stable short ID for a pool (first 12 chars of hash)."""
    key = f"{pool.get('address', '')}|{pool.get('chain', '')}|{pool.get('source', '')}"
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def _enrich(pool: dict) -> dict:
    """Add pool_id to a pool dict."""
    pool = dict(pool)
    pool["pool_id"] = _pool_id(pool)
    return pool


# ── Response Models ──────────────────────────────────────────────


class PoolResponse(BaseModel):
    pool_id: str
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


class PoolsListResponse(BaseModel):
    pools: list[PoolResponse]
    total: int
    filters_applied: dict = {}


class BestYieldResponse(BaseModel):
    pools: list[PoolResponse]
    count: int
    chain_filter: str | None = None


class RiskScoreResponse(BaseModel):
    pool_id: str
    pool_name: str
    address: str
    chain: str
    source: str
    risk_level: str
    risk_score: int = Field(description="0-100, higher = riskier")
    factors: list[str]
    recommendation: str


class AllocationItem(BaseModel):
    pool_address: str
    chain: str = "ethereum"
    amount_usd: float


class RebalanceRequest(BaseModel):
    current_allocation: list[AllocationItem]
    risk_tolerance: str = "high"  # low, medium, high
    position_size: float = 10_000


class RebalanceAction(BaseModel):
    action: str  # "keep", "withdraw", "deposit"
    pool_name: str
    pool_address: str
    chain: str
    current_amount_usd: float = 0
    suggested_amount_usd: float = 0
    apy: float = 0
    reason: str = ""


class RebalanceResponse(BaseModel):
    strategy: str  # "hold", "rebalance", "enter"
    actions: list[RebalanceAction]
    current_total_usd: float
    expected_blended_apy: float
    rationale: str


# ── REST Endpoints ───────────────────────────────────────────────


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "crvusd-yield-optimizer-api",
        "version": "1.1.0",
        "a2a": True,
    }


@app.get("/api/pools", response_model=PoolsListResponse)
async def list_pools(
    chain: str | None = Query(None, description="Filter by chain (ethereum, arbitrum, optimism, fraxtal)"),
    source: str | None = Query(None, description="Filter by source (scrvusd, llamalend, boosted_lp, crvusd_mint)"),
    min_apy: float = Query(0, description="Minimum APY threshold"),
    min_tvl: float = Query(0, description="Minimum TVL in USD"),
    risk: str | None = Query(None, description="Filter by risk level (low, medium, high)"),
    sort_by: str = Query("apy", description="Sort field (apy, tvl, risk, name)"),
    order: str = Query("desc", description="Sort order (asc, desc)"),
    limit: int = Query(100, ge=1, le=500, description="Max results"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
    auth: dict = Depends(verify_api_key),
):
    """List all yield pools with optional filters.

    Examples:
    - /api/pools?chain=ethereum&source=llamalend&min_apy=5
    - /api/pools?risk=low&sort_by=tvl&order=desc
    - /api/pools?min_tvl=100000&limit=10
    """
    pools = await _get_pools()
    enriched = [_enrich(p) for p in pools]

    filters_applied = {}

    if chain:
        chain_lower = chain.lower()
        enriched = [p for p in enriched if p.get("chain", "").lower() == chain_lower]
        filters_applied["chain"] = chain_lower

    if source:
        source_lower = source.lower()
        enriched = [p for p in enriched if p.get("source", "").lower() == source_lower]
        filters_applied["source"] = source_lower

    if min_apy > 0:
        enriched = [p for p in enriched if p.get("apy", 0) >= min_apy]
        filters_applied["min_apy"] = min_apy

    if min_tvl > 0:
        enriched = [p for p in enriched if p.get("tvl", 0) >= min_tvl]
        filters_applied["min_tvl"] = min_tvl

    if risk:
        risk_lower = risk.lower()
        enriched = [p for p in enriched if p.get("risk", "").lower() == risk_lower]
        filters_applied["risk"] = risk_lower

    # Sort
    sort_key = sort_by if sort_by in ("apy", "tvl", "name", "risk") else "apy"
    reverse = order.lower() != "asc"

    if sort_key == "risk":
        risk_order = {"low": 0, "medium": 1, "high": 2}
        enriched.sort(key=lambda p: risk_order.get(p.get("risk", "medium"), 1), reverse=reverse)
    elif sort_key == "name":
        enriched.sort(key=lambda p: p.get("name", "").lower(), reverse=reverse)
    else:
        enriched.sort(key=lambda p: p.get(sort_key, 0), reverse=reverse)

    total = len(enriched)
    enriched = enriched[offset : offset + limit]

    return PoolsListResponse(
        pools=[PoolResponse(**p) for p in enriched],
        total=total,
        filters_applied=filters_applied,
    )


@app.get("/api/best-yield", response_model=BestYieldResponse)
async def best_yield(
    top: int = Query(5, ge=1, le=50, description="Number of top pools to return"),
    chain: str | None = Query(None, description="Filter by chain"),
    source: str | None = Query(None, description="Filter by source"),
    risk: str | None = Query(None, description="Max risk level (low, medium, high)"),
    auth: dict = Depends(verify_api_key),
):
    """Return top N pools by APY.

    Examples:
    - /api/best-yield?top=5&chain=ethereum
    - /api/best-yield?top=10&risk=medium
    """
    pools = await _get_pools()
    enriched = [_enrich(p) for p in pools]

    if chain:
        chain_lower = chain.lower()
        enriched = [p for p in enriched if p.get("chain", "").lower() == chain_lower]

    if source:
        source_lower = source.lower()
        enriched = [p for p in enriched if p.get("source", "").lower() == source_lower]

    if risk:
        risk_order = {"low": 0, "medium": 1, "high": 2}
        max_risk = risk_order.get(risk.lower(), 2)
        enriched = [
            p for p in enriched
            if risk_order.get(p.get("risk", "high"), 2) <= max_risk
        ]

    # Sort by APY descending
    enriched.sort(key=lambda p: p.get("apy", 0), reverse=True)
    enriched = enriched[:top]

    return BestYieldResponse(
        pools=[PoolResponse(**p) for p in enriched],
        count=len(enriched),
        chain_filter=chain,
    )


@app.get("/api/risk-score/{pool_id}", response_model=RiskScoreResponse)
async def risk_score(pool_id: str, auth: dict = Depends(verify_api_key)):
    """Risk assessment for a specific pool.

    The pool_id is a 12-character hash from /api/pools response.
    You can also use the pool address (0x...) as pool_id.
    """
    pools = await _get_pools()
    enriched = [_enrich(p) for p in pools]

    # Find by pool_id or by address prefix
    pool = None
    for p in enriched:
        if p["pool_id"] == pool_id:
            pool = p
            break
        if p.get("address", "").lower().startswith(pool_id.lower()):
            pool = p
            break
        if pool_id.lower() in p.get("address", "").lower():
            pool = p
            break

    if not pool:
        raise HTTPException(status_code=404, detail=f"Pool not found: {pool_id}")

    # Calculate risk score and factors
    source = pool.get("source", "")
    chain = pool.get("chain", "ethereum")
    apy = pool.get("apy", 0)
    tvl = pool.get("tvl", 0)
    risk_level = pool.get("risk", "medium")

    score = 0
    factors = []

    # Source risk
    source_scores = {
        "scrvusd": 10,
        "llamalend": 35,
        "crvusd_mint": 30,
        "stable_lp": 40,
        "boosted_lp": 60,
    }
    source_score = source_scores.get(source, 50)
    score += source_score
    factors.append(f"Source ({source}): base risk {source_score}/100")

    # APY risk (unusually high APY = higher risk)
    if apy > 50:
        score += 20
        factors.append(f"Very high APY ({apy:.1f}%): +20 risk - unsustainable yields")
    elif apy > 20:
        score += 10
        factors.append(f"High APY ({apy:.1f}%): +10 risk - may be temporary")
    elif apy > 5:
        factors.append(f"Moderate APY ({apy:.1f}%): normal range")
    else:
        score -= 5
        factors.append(f"Low APY ({apy:.1f}%): -5 risk - conservative yield")

    # TVL risk (low TVL = higher risk)
    if tvl < 100_000:
        score += 15
        factors.append(f"Low TVL (${tvl:,.0f}): +15 risk - low liquidity")
    elif tvl < 1_000_000:
        score += 5
        factors.append(f"Moderate TVL (${tvl:,.0f}): +5 risk")
    else:
        score -= 5
        factors.append(f"High TVL (${tvl:,.0f}): -5 risk - deep liquidity")

    # Chain risk
    if chain != "ethereum":
        score += 5
        factors.append(f"L2 chain ({chain}): +5 risk - bridge dependency")

    # Utilization risk (for lending markets)
    utilization = pool.get("extra", {}).get("utilization", 0)
    if utilization > 0.9:
        score += 10
        factors.append(f"High utilization ({utilization:.0%}): +10 risk - withdrawal risk")
    elif utilization > 0.7:
        score += 5
        factors.append(f"Moderate utilization ({utilization:.0%}): +5 risk")

    # Clamp
    score = max(0, min(100, score))

    # Recommendation
    if score <= 25:
        recommendation = "Low risk. Suitable for conservative strategies and large positions."
    elif score <= 50:
        recommendation = "Moderate risk. Good for balanced portfolios. Monitor utilization."
    elif score <= 75:
        recommendation = "Elevated risk. Consider position sizing. Higher APY compensates."
    else:
        recommendation = "High risk. Small position sizes recommended. APY may be unsustainable."

    return RiskScoreResponse(
        pool_id=pool["pool_id"],
        pool_name=pool.get("name", ""),
        address=pool.get("address", ""),
        chain=chain,
        source=source,
        risk_level=risk_level,
        risk_score=score,
        factors=factors,
        recommendation=recommendation,
    )


@app.post("/api/rebalance", response_model=RebalanceResponse)
async def simulate_rebalance(req: RebalanceRequest, auth: dict = Depends(verify_api_key)):
    """Simulate rebalance: accept current allocation, suggest optimal.
    Requires pro or enterprise tier.

    Send your current positions and get recommendations for optimal allocation.

    Example body:
    ```json
    {
      "current_allocation": [
        {"pool_address": "0x0655977FEb2f289A4aB78af67BAB0d17aAb84367", "chain": "ethereum", "amount_usd": 5000}
      ],
      "risk_tolerance": "medium",
      "position_size": 10000
    }
    ```
    """
    check_endpoint_access(auth, "rebalance")
    pools = await _get_pools()
    enriched = [_enrich(p) for p in pools]

    # Filter pools by risk tolerance
    risk_order = {"low": 0, "medium": 1, "high": 2}
    max_risk = risk_order.get(req.risk_tolerance.lower(), 2)
    eligible = [
        p for p in enriched
        if risk_order.get(p.get("risk", "high"), 2) <= max_risk
        and p.get("apy", 0) > 0
        and p.get("tvl", 0) >= 10_000
    ]

    # Sort eligible by APY desc
    eligible.sort(key=lambda p: p.get("apy", 0), reverse=True)

    total_current = sum(a.amount_usd for a in req.current_allocation)
    total_position = max(total_current, req.position_size)

    actions: list[RebalanceAction] = []

    # Match current allocations to pools
    current_pools = {}
    for alloc in req.current_allocation:
        addr_lower = alloc.pool_address.lower()
        matched = None
        for p in enriched:
            if p.get("address", "").lower() == addr_lower and p.get("chain", "").lower() == alloc.chain.lower():
                matched = p
                break
        current_pools[addr_lower] = {
            "alloc": alloc,
            "pool": matched,
        }

    # If no eligible pools, recommend hold
    if not eligible:
        return RebalanceResponse(
            strategy="hold",
            actions=[],
            current_total_usd=total_current,
            expected_blended_apy=0,
            rationale="No eligible pools found matching risk tolerance. Hold current positions.",
        )

    best = eligible[0]

    # Check if already in best pool
    best_addr = best.get("address", "").lower()
    already_in_best = best_addr in current_pools

    if already_in_best and len(current_pools) == 1:
        cur = current_pools[best_addr]
        return RebalanceResponse(
            strategy="hold",
            actions=[
                RebalanceAction(
                    action="keep",
                    pool_name=best.get("name", ""),
                    pool_address=best.get("address", ""),
                    chain=best.get("chain", "ethereum"),
                    current_amount_usd=cur["alloc"].amount_usd,
                    suggested_amount_usd=cur["alloc"].amount_usd,
                    apy=best.get("apy", 0),
                    reason="Already in the best pool",
                ),
            ],
            current_total_usd=total_current,
            expected_blended_apy=best.get("apy", 0),
            rationale=f"Already in the optimal pool: {best.get('name', '')} at {best.get('apy', 0):.2f}% APY.",
        )

    # Build rebalance plan
    top_n = min(3, len(eligible))
    top_pools = eligible[:top_n]

    # Allocate: weight by APY
    total_top_apy = sum(p.get("apy", 0) for p in top_pools)
    if total_top_apy <= 0:
        total_top_apy = 1

    # Withdraw actions for current positions not in top pools
    for addr_lower, info in current_pools.items():
        pool = info["pool"]
        alloc = info["alloc"]
        in_top = any(p.get("address", "").lower() == addr_lower for p in top_pools)

        if not in_top:
            actions.append(
                RebalanceAction(
                    action="withdraw",
                    pool_name=pool.get("name", "Unknown") if pool else "Unknown",
                    pool_address=alloc.pool_address,
                    chain=alloc.chain,
                    current_amount_usd=alloc.amount_usd,
                    suggested_amount_usd=0,
                    apy=pool.get("apy", 0) if pool else 0,
                    reason="Better opportunities available in top pools",
                ),
            )

    # Deposit actions into top pools
    for p in top_pools:
        weight = p.get("apy", 0) / total_top_apy
        suggested = total_position * weight
        p_addr = p.get("address", "").lower()

        current_amount = 0
        if p_addr in current_pools:
            current_amount = current_pools[p_addr]["alloc"].amount_usd

        action_type = "keep" if current_amount > 0 and abs(current_amount - suggested) < 100 else "deposit"

        actions.append(
            RebalanceAction(
                action=action_type,
                pool_name=p.get("name", ""),
                pool_address=p.get("address", ""),
                chain=p.get("chain", "ethereum"),
                current_amount_usd=current_amount,
                suggested_amount_usd=round(suggested, 2),
                apy=p.get("apy", 0),
                reason=f"APY {p.get('apy', 0):.2f}% | Risk: {p.get('risk', 'medium')} | TVL: ${p.get('tvl', 0):,.0f}",
            ),
        )

    # Calculate blended APY
    blended_apy = 0
    for p in top_pools:
        weight = p.get("apy", 0) / total_top_apy
        blended_apy += p.get("apy", 0) * weight

    # Determine strategy
    has_changes = any(a.action != "keep" for a in actions)
    strategy = "rebalance" if has_changes and total_current > 0 else "enter" if total_current == 0 else "hold"

    top_names = ", ".join(f"{p.get('name', '')} ({p.get('apy', 0):.2f}%)" for p in top_pools)
    rationale = f"Optimal allocation across top {top_n} pools: {top_names}. Expected blended APY: {blended_apy:.2f}%."

    return RebalanceResponse(
        strategy=strategy,
        actions=actions,
        current_total_usd=total_current,
        expected_blended_apy=round(blended_apy, 2),
        rationale=rationale,
    )


# ── A2A Skill Handlers ──────────────────────────────────────────
# These bridge A2A task requests to existing API logic.
# Each handler takes a params dict and returns a result dict.


async def _a2a_handle_pools(params: dict) -> dict:
    """A2A handler: list/filter pools."""
    pools = await _get_pools()
    enriched = [_enrich(p) for p in pools]

    chain = params.get("chain")
    source = params.get("source")
    min_apy = params.get("min_apy", 0)
    min_tvl = params.get("min_tvl", 0)
    risk = params.get("risk")
    limit = params.get("limit", 100)

    if chain:
        enriched = [p for p in enriched if p.get("chain", "").lower() == chain.lower()]
    if source:
        enriched = [p for p in enriched if p.get("source", "").lower() == source.lower()]
    if min_apy > 0:
        enriched = [p for p in enriched if p.get("apy", 0) >= min_apy]
    if min_tvl > 0:
        enriched = [p for p in enriched if p.get("tvl", 0) >= min_tvl]
    if risk:
        enriched = [p for p in enriched if p.get("risk", "").lower() == risk.lower()]

    enriched.sort(key=lambda p: p.get("apy", 0), reverse=True)
    enriched = enriched[:limit]

    return {"pools": enriched, "total": len(enriched)}


async def _a2a_handle_best_yield(params: dict) -> dict:
    """A2A handler: find best yield opportunities."""
    pools = await _get_pools()
    enriched = [_enrich(p) for p in pools]

    chain = params.get("chain")
    risk = params.get("risk")
    top = params.get("top", params.get("count", 5))

    if chain:
        enriched = [p for p in enriched if p.get("chain", "").lower() == chain.lower()]

    if risk:
        risk_order = {"low": 0, "medium": 1, "high": 2}
        max_risk = risk_order.get(risk.lower(), 2)
        enriched = [
            p for p in enriched
            if risk_order.get(p.get("risk", "high"), 2) <= max_risk
        ]

    enriched.sort(key=lambda p: p.get("apy", 0), reverse=True)
    enriched = enriched[:top]

    return {
        "pools": enriched,
        "count": len(enriched),
        "chain_filter": chain,
    }


async def _a2a_handle_risk_score(params: dict) -> dict:
    """A2A handler: risk assessment for a pool."""
    pool_id = params.get("pool_id")
    if not pool_id:
        raise ValueError("pool_id is required for risk-score skill")

    pools = await _get_pools()
    enriched = [_enrich(p) for p in pools]

    pool = None
    for p in enriched:
        if p["pool_id"] == pool_id:
            pool = p
            break
        if p.get("address", "").lower().startswith(pool_id.lower()):
            pool = p
            break
        if pool_id.lower() in p.get("address", "").lower():
            pool = p
            break

    if not pool:
        raise ValueError(f"Pool not found: {pool_id}")

    # Reuse the same scoring logic
    source = pool.get("source", "")
    chain = pool.get("chain", "ethereum")
    apy = pool.get("apy", 0)
    tvl = pool.get("tvl", 0)

    score = 0
    factors = []
    source_scores = {"scrvusd": 10, "llamalend": 35, "crvusd_mint": 30, "stable_lp": 40, "boosted_lp": 60}
    source_score = source_scores.get(source, 50)
    score += source_score
    factors.append(f"Source ({source}): base risk {source_score}/100")

    if apy > 50:
        score += 20
        factors.append(f"Very high APY ({apy:.1f}%): +20 risk")
    elif apy > 20:
        score += 10
        factors.append(f"High APY ({apy:.1f}%): +10 risk")
    elif apy > 5:
        factors.append(f"Moderate APY ({apy:.1f}%): normal range")
    else:
        score -= 5
        factors.append(f"Low APY ({apy:.1f}%): -5 risk")

    if tvl < 100_000:
        score += 15
        factors.append(f"Low TVL (${tvl:,.0f}): +15 risk")
    elif tvl < 1_000_000:
        score += 5
        factors.append(f"Moderate TVL (${tvl:,.0f}): +5 risk")
    else:
        score -= 5
        factors.append(f"High TVL (${tvl:,.0f}): -5 risk")

    if chain != "ethereum":
        score += 5
        factors.append(f"L2 chain ({chain}): +5 risk")

    score = max(0, min(100, score))

    if score <= 25:
        recommendation = "Low risk. Suitable for conservative strategies."
    elif score <= 50:
        recommendation = "Moderate risk. Good for balanced portfolios."
    elif score <= 75:
        recommendation = "Elevated risk. Consider position sizing."
    else:
        recommendation = "High risk. Small positions recommended."

    return {
        "pool_id": pool["pool_id"],
        "pool_name": pool.get("name", ""),
        "address": pool.get("address", ""),
        "chain": chain,
        "source": source,
        "risk_level": pool.get("risk", "medium"),
        "risk_score": score,
        "factors": factors,
        "recommendation": recommendation,
    }


async def _a2a_handle_rebalance(params: dict) -> dict:
    """A2A handler: rebalance simulation."""
    pools = await _get_pools()
    enriched = [_enrich(p) for p in pools]

    risk_tolerance = params.get("risk_tolerance", "high")
    position_size = params.get("position_size", 10_000)
    current_allocation = params.get("current_allocation", [])

    risk_order = {"low": 0, "medium": 1, "high": 2}
    max_risk = risk_order.get(risk_tolerance.lower(), 2)
    eligible = [
        p for p in enriched
        if risk_order.get(p.get("risk", "high"), 2) <= max_risk
        and p.get("apy", 0) > 0
        and p.get("tvl", 0) >= 10_000
    ]
    eligible.sort(key=lambda p: p.get("apy", 0), reverse=True)

    total_current = sum(a.get("amount_usd", 0) for a in current_allocation)
    total_position = max(total_current, position_size)

    if not eligible:
        return {
            "strategy": "hold",
            "actions": [],
            "current_total_usd": total_current,
            "expected_blended_apy": 0,
            "rationale": "No eligible pools found matching risk tolerance.",
        }

    top_n = min(3, len(eligible))
    top_pools = eligible[:top_n]
    total_top_apy = sum(p.get("apy", 0) for p in top_pools) or 1

    actions = []
    for p in top_pools:
        weight = p.get("apy", 0) / total_top_apy
        suggested = total_position * weight
        actions.append({
            "action": "deposit",
            "pool_name": p.get("name", ""),
            "pool_address": p.get("address", ""),
            "chain": p.get("chain", "ethereum"),
            "current_amount_usd": 0,
            "suggested_amount_usd": round(suggested, 2),
            "apy": p.get("apy", 0),
            "reason": f"APY {p.get('apy', 0):.2f}% | Risk: {p.get('risk', 'medium')}",
        })

    blended_apy = sum(
        (p.get("apy", 0) / total_top_apy) * p.get("apy", 0) for p in top_pools
    )

    strategy = "enter" if total_current == 0 else "rebalance"
    top_names = ", ".join(f"{p.get('name', '')} ({p.get('apy', 0):.2f}%)" for p in top_pools)

    return {
        "strategy": strategy,
        "actions": actions,
        "current_total_usd": total_current,
        "expected_blended_apy": round(blended_apy, 2),
        "rationale": f"Optimal allocation across top {top_n} pools: {top_names}.",
    }


# Skill handler registry: maps skill IDs to handler functions
_A2A_HANDLERS: dict[str, Any] = {
    "pools": _a2a_handle_pools,
    "best-yield": _a2a_handle_best_yield,
    "risk-score": _a2a_handle_risk_score,
    "rebalance": _a2a_handle_rebalance,
    # Legacy aliases
    "optimize": _a2a_handle_best_yield,
    "yields": _a2a_handle_best_yield,
    "risk": _a2a_handle_risk_score,
}


# ── A2A JSON-RPC Endpoint ───────────────────────────────────────


@app.post("/a2a")
async def a2a_endpoint(request: Request, auth: dict = Depends(verify_api_key)):
    """A2A JSON-RPC 2.0 endpoint.

    Supports A2A protocol methods:
    - message/send: Send a task to the agent (natural language or structured)
    - tasks/get: Retrieve task status and results by ID
    - tasks/cancel: Cancel a running task

    Legacy aliases also supported: SendMessage, GetTask, CancelTask, ListTasks.

    Example - Natural language:
    ```json
    {
      "jsonrpc": "2.0",
      "method": "message/send",
      "params": {
        "message": {
          "role": "user",
          "parts": [{"type": "text", "text": "find best yield for 10000 crvUSD"}]
        }
      },
      "id": 1
    }
    ```

    Example - Explicit skill:
    ```json
    {
      "jsonrpc": "2.0",
      "method": "message/send",
      "params": {
        "skill": "best-yield",
        "message": {
          "role": "user",
          "parts": [{"type": "data", "data": {"chain": "ethereum", "top": 5}}]
        }
      },
      "id": 1
    }
    ```
    """
    check_endpoint_access(auth, "a2a")
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400,
            content={
                "jsonrpc": "2.0",
                "error": {"code": -32700, "message": "Parse error: invalid JSON"},
                "id": None,
            },
        )

    result = await handle_a2a_request(body, _A2A_HANDLERS)
    return JSONResponse(content=result)


@app.post("/a2a/stream")
async def a2a_stream_endpoint(request: Request, auth: dict = Depends(verify_api_key)):
    """A2A SSE streaming endpoint (message/stream).

    Same request format as /a2a but returns Server-Sent Events stream
    with real-time task status updates and results.

    Example:
    ```json
    {
      "skill": "best-yield",
      "message": {
        "role": "user",
        "parts": [{"type": "text", "text": "find best yield"}]
      }
    }
    ```
    """
    check_endpoint_access(auth, "a2a")
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400,
            content={"error": "Invalid JSON"},
        )

    # For streaming, we work directly with params
    params = body.get("params", body)

    async def event_generator():
        async for event in stream_task_events("", _A2A_HANDLERS, params):
            yield event

    return EventSourceResponse(event_generator())


# ── Pricing ──────────────────────────────────────────────────────


@app.get("/api/pricing")
async def pricing():
    """API pricing — pay-per-request via x402 protocol (USDC on Base)."""
    return {
        "payment_protocol": "x402 (HTTP 402 Payment Required)",
        "currency": "USDC",
        "network": "Base Sepolia testnet (eip155:84532)",
        "pricing": {
            "GET /api/risk-score/{pool_id}": "$0.005",
            "POST /api/rebalance": "$0.01",
            "POST /a2a": "$0.01",
            "POST /a2a/stream": "$0.01",
        },
        "free_endpoints": [
            "GET /health",
            "GET /api/pools",
            "GET /api/best-yield",
            "GET /api/pricing",
            "GET /.well-known/agent.json",
            "GET /docs",
        ],
        "how_it_works": (
            "1. Request any paid endpoint without payment → get 402 with payment details. "
            "2. Sign a USDC payment on Base using the returned parameters. "
            "3. Resend request with X-PAYMENT header → get 200 with data."
        ),
        "wallet": "0x6a1175D0EA0e6817786Ce51F1C4F3294F907f410",
        "contact": "api@chado.studio",
    }


# ── A2A Agent Card ───────────────────────────────────────────────

_AGENT_BASE_URL = "http://51.83.161.121:8717"


@app.get("/.well-known/agent.json")
async def agent_card():
    """A2A Agent Card for service discovery.

    Follows the A2A protocol specification for agent interoperability.
    Other AI agents can discover this agent's capabilities and interact
    via the /a2a JSON-RPC endpoint.
    """
    return {
        "name": "crvUSD Yield Optimizer",
        "description": (
            "Multi-chain crvUSD yield optimizer agent. Discovers yield opportunities "
            "across scrvUSD, LlamaLend, Convex and StakeDAO on Ethereum, Arbitrum, "
            "Optimism and Fraxtal. Provides real-time APY data, risk scoring (0-100), "
            "and portfolio rebalance recommendations. Accepts natural language queries."
        ),
        "url": _AGENT_BASE_URL,
        "version": "1.1.0",
        "protocolVersion": "0.2.5",
        "provider": {
            "organization": "Chado Studio",
            "url": "https://chado.studio",
        },
        "capabilities": {
            "streaming": True,
            "pushNotifications": False,
            "stateTransitionHistory": True,
        },
        "defaultInputModes": ["application/json", "text/plain"],
        "defaultOutputModes": ["application/json", "text/plain"],
        "skills": [
            {
                "id": "best-yield",
                "name": "Best Yield Finder",
                "description": (
                    "Find the highest-yielding crvUSD opportunities. Accepts natural language "
                    "like 'find best yield for 10000 crvUSD on ethereum' or structured params "
                    "(chain, risk, top)."
                ),
                "tags": ["yield", "apy", "defi", "crvusd"],
                "examples": [
                    "What's the best yield for crvUSD?",
                    "Find top 5 pools on ethereum",
                    "Show highest APY with low risk",
                ],
            },
            {
                "id": "pools",
                "name": "Pool Discovery",
                "description": (
                    "List and filter all crvUSD yield pools across chains and sources. "
                    "Filter by chain, source, APY, TVL, risk level."
                ),
                "tags": ["pools", "list", "filter", "defi"],
                "examples": [
                    "List all pools on arbitrum",
                    "Show llamalend pools with APY above 5%",
                ],
            },
            {
                "id": "risk-score",
                "name": "Risk Assessment",
                "description": (
                    "Calculate risk score (0-100) for a specific pool. Analyzes source risk, "
                    "APY sustainability, TVL depth, chain risk, and utilization."
                ),
                "tags": ["risk", "assessment", "score", "safety"],
                "examples": [
                    "Risk score for pool 0x0655977FEb2f289A4aB78af67BAB0d17aAb84367",
                    "Assess risk for pool abc123def456",
                ],
            },
            {
                "id": "rebalance",
                "name": "Rebalance Advisor",
                "description": (
                    "Simulate portfolio rebalancing. Given current positions and risk tolerance, "
                    "recommends optimal crvUSD allocation across pools."
                ),
                "tags": ["rebalance", "portfolio", "allocation", "strategy"],
                "examples": [
                    "Rebalance my portfolio of 10000 crvUSD",
                    "Optimize my allocation with medium risk",
                ],
            },
        ],
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8717)
