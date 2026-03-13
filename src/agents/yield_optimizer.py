from __future__ import annotations

import logging
from dataclasses import dataclass, asdict

import httpx

logger = logging.getLogger(__name__)

REBALANCE_THRESHOLD = 1.05  # recommend rebalance if best > current * 1.05


@dataclass
class PoolInfo:
    name: str
    address: str
    apy: float
    tvl: float
    source: str  # "crvusd_lending" or "stable_pool"

    def to_dict(self) -> dict:
        return asdict(self)


class YieldOptimizer:
    """crvUSD yield monitoring and optimization for Olas Pearl track.

    Fetches crvUSD lending markets and stable pool APYs from Curve,
    compares yields, and recommends rebalancing when beneficial.
    """

    CRVUSD_MARKETS_URL = "https://prices.curve.finance/v1/crvusd/markets/ethereum"
    FACTORY_APYS_URL = "https://api.curve.finance/v1/getFactoryAPYs-ethereum"

    def __init__(self, rebalance_threshold: float = REBALANCE_THRESHOLD):
        self.rebalance_threshold = rebalance_threshold

    async def run(self, task: dict) -> dict:
        """Main entry point. Fetches data, compares yields, returns strategy.

        Args:
            task: dict with optional keys:
                - current_pool: address of user's current pool (for rebalance calc)
                - min_tvl: minimum TVL filter (default 100_000)
        """
        current_pool = task.get("current_pool")
        min_tvl = task.get("min_tvl", 100_000)

        async with httpx.AsyncClient(
            timeout=30, follow_redirects=True
        ) as client:
            lending_pools = await self._fetch_crvusd_markets(client)
            stable_pools = await self._fetch_stable_pool_apys(client)

        all_pools = lending_pools + stable_pools

        # Filter by minimum TVL
        filtered = [p for p in all_pools if p.tvl >= min_tvl]
        if not filtered:
            filtered = all_pools  # fallback: show all if nothing passes filter

        # Sort by APY descending
        filtered.sort(key=lambda p: p.apy, reverse=True)

        best = filtered[0] if filtered else None

        # Determine current pool APY (if specified)
        current_apy = 0.0
        current_info = None
        if current_pool:
            current_info = next(
                (p for p in all_pools if p.address.lower() == current_pool.lower()),
                None,
            )
            if current_info:
                current_apy = current_info.apy

        # Rebalance logic
        strategy, rebalance_target = self._decide_strategy(
            current_apy, current_info, best
        )

        return {
            "pools": [p.to_dict() for p in filtered[:20]],  # top 20
            "total_pools_found": len(all_pools),
            "best_yield": best.to_dict() if best else None,
            "current_pool": current_info.to_dict() if current_info else None,
            "current_apy": current_apy,
            "strategy": strategy,
            "rebalance_target": rebalance_target,
            "rebalance_needed": strategy == "rebalance",
            "threshold": self.rebalance_threshold,
        }

    def _decide_strategy(
        self,
        current_apy: float,
        current_info: PoolInfo | None,
        best: PoolInfo | None,
    ) -> tuple[str, dict | None]:
        """Decide hold/rebalance based on APY comparison."""
        if not best:
            return "hold", None

        if current_info is None:
            # No current position -- recommend the best pool
            return "enter", best.to_dict()

        if best.address.lower() == current_info.address.lower():
            return "hold", None

        if current_apy <= 0:
            return "rebalance", best.to_dict()

        ratio = best.apy / current_apy
        if ratio >= self.rebalance_threshold:
            return "rebalance", best.to_dict()

        return "hold", None

    async def _fetch_crvusd_markets(self, client: httpx.AsyncClient) -> list[PoolInfo]:
        """Fetch crvUSD lending markets from Curve prices API."""
        try:
            resp = await client.get(self.CRVUSD_MARKETS_URL)
            resp.raise_for_status()
            data = resp.json().get("data", [])
            pools = []
            for m in data:
                lend_apy = self._parse_float(m.get("rate", m.get("lend_apy", 0)))
                # rate is per-second borrow rate in some API versions;
                # lend_apy is annualized lend yield
                # Use lend_apy if available, otherwise approximate from borrow_apy
                if lend_apy == 0:
                    borrow_apy = self._parse_float(m.get("borrow_apy", 0))
                    utilization = self._parse_float(m.get("utilization", 0))
                    # lend APY ~ borrow APY * utilization (simplified)
                    lend_apy = borrow_apy * utilization if utilization else 0

                total_assets = self._parse_float(
                    m.get("total_assets_usd", m.get("total_debt", 0))
                )
                name = m.get("name", m.get("collateral_token", {}).get("symbol", "?"))

                pools.append(
                    PoolInfo(
                        name=f"crvUSD/{name}",
                        address=m.get("address", m.get("controller", "")),
                        apy=lend_apy * 100 if lend_apy < 1 else lend_apy,
                        tvl=total_assets,
                        source="crvusd_lending",
                    )
                )
            logger.info("Fetched %d crvUSD lending markets", len(pools))
            return pools
        except Exception as exc:
            logger.error("Failed to fetch crvUSD markets: %s", exc)
            return []

    async def _fetch_stable_pool_apys(
        self, client: httpx.AsyncClient
    ) -> list[PoolInfo]:
        """Fetch stable pool APYs from Curve factory API.

        Filters for pools containing crvUSD or major stables.
        """
        try:
            resp = await client.get(self.FACTORY_APYS_URL)
            resp.raise_for_status()
            body = resp.json()
            pool_data = body.get("data", body.get("poolDetails", []))

            # API may return dict {address: {apy, ...}} or list
            if isinstance(pool_data, dict):
                items = [
                    {"address": addr, **info} for addr, info in pool_data.items()
                ]
            else:
                items = pool_data

            pools = []
            stable_keywords = {"crvusd", "usdc", "usdt", "dai", "frax", "mkusd", "gho"}
            for item in items:
                pool_name = str(item.get("poolName", item.get("name", ""))).lower()
                # Only include pools related to stables / crvUSD
                if not any(kw in pool_name for kw in stable_keywords):
                    continue

                apy = self._parse_float(item.get("apy", item.get("apyWeekly", 0)))
                tvl = self._parse_float(
                    item.get("tvlUsd", item.get("usdTotal", item.get("tvl", 0)))
                )

                pools.append(
                    PoolInfo(
                        name=item.get("poolName", item.get("name", pool_name)),
                        address=item.get("address", item.get("poolAddress", "")),
                        apy=apy,
                        tvl=tvl,
                        source="stable_pool",
                    )
                )
            logger.info("Fetched %d stable pools", len(pools))
            return pools
        except Exception as exc:
            logger.error("Failed to fetch stable pool APYs: %s", exc)
            return []

    @staticmethod
    def _parse_float(value) -> float:
        """Safely parse a numeric value to float."""
        if value is None:
            return 0.0
        try:
            return float(value)
        except (ValueError, TypeError):
            return 0.0

    def status(self) -> str:
        return "ready"
