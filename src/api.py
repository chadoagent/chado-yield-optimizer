"""REST API MVP for crvUSD Yield Optimizer.

Endpoints:
  GET  /api/pools          - list all pools with optional filters
  GET  /api/best-yield     - top N pools by APY
  GET  /api/risk-score/{pool_id} - risk assessment for a pool
  POST /api/rebalance      - simulate rebalance from current allocation

Run:
  python -m uvicorn src.api:app --host 0.0.0.0 --port 8717
  Swagger docs: http://localhost:8717/docs
"""

from __future__ import annotations

import hashlib
import time
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from src.agents.yield_optimizer import (
    YieldOptimizer,
    PoolInfo,
    RiskLevel,
    YieldSource,
    SOURCE_RISK,
    SUPPORTED_CHAINS,
    BRIDGE_COSTS,
)

# ── App ──────────────────────────────────────────────────────────

app = FastAPI(
    title="crvUSD Yield Optimizer API",
    description=(
        "Multi-chain crvUSD yield optimizer. "
        "Sources: scrvUSD, LlamaLend, Convex, StakeDAO. "
        "Chains: Ethereum, Arbitrum, Optimism, Fraxtal."
    ),
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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


# ── Endpoints ────────────────────────────────────────────────────


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "crvusd-yield-optimizer-api",
        "version": "1.0.0",
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
async def risk_score(pool_id: str):
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
async def simulate_rebalance(req: RebalanceRequest):
    """Simulate rebalance: accept current allocation, suggest optimal.

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
    # Withdraw from worse pools, deposit into top pools
    # Simple strategy: concentrate into top 1-3 pools
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
                    reason=f"Better opportunities available in top pools",
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


# ── Agent Card ───────────────────────────────────────────────────


@app.get("/.well-known/agent.json")
async def agent_card():
    """A2A Agent Card for service discovery."""
    return {
        "name": "crvUSD Yield Optimizer",
        "description": "Multi-chain crvUSD yield optimizer API. Real-time pool data, risk scoring, rebalance simulation.",
        "url": "http://localhost:8717",
        "version": "1.0.0",
        "provider": {"organization": "Chado Studio", "url": "https://chado.studio"},
        "capabilities": {"streaming": False, "pushNotifications": False},
        "skills": [
            {"id": "pools", "name": "Pool Discovery", "description": "List and filter crvUSD yield pools"},
            {"id": "best-yield", "name": "Best Yield", "description": "Find top APY opportunities"},
            {"id": "risk-score", "name": "Risk Assessment", "description": "Per-pool risk scoring (0-100)"},
            {"id": "rebalance", "name": "Rebalance Simulation", "description": "Optimal allocation recommendations"},
        ],
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8717)
