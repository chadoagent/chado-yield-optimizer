from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, asdict, field
from enum import Enum

import httpx

logger = logging.getLogger(__name__)

REBALANCE_THRESHOLD = 1.05  # recommend rebalance if best > current * 1.05


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class YieldSource(str, Enum):
    SCRVUSD = "scrvusd"           # Savings crvUSD vault
    LLAMALEND = "llamalend"       # LlamaLend deposit (lend crvUSD)
    CRVUSD_MINT = "crvusd_mint"   # crvUSD minting markets (legacy)
    STABLE_LP = "stable_lp"       # Plain Curve LP pools
    BOOSTED_LP = "boosted_lp"     # Convex/StakeDAO boosted LP


# Chains with LlamaLend support (verified via API)
SUPPORTED_CHAINS = [
    {"name": "ethereum", "chain_id": 1, "label": "Ethereum", "gas_cost_usd": 15.0},
    {"name": "arbitrum", "chain_id": 42161, "label": "Arbitrum", "gas_cost_usd": 0.5},
    {"name": "optimism", "chain_id": 10, "label": "Optimism", "gas_cost_usd": 0.5},
    {"name": "fraxtal", "chain_id": 252, "label": "Fraxtal", "gas_cost_usd": 0.1},
]

# Risk classification per source
SOURCE_RISK = {
    YieldSource.SCRVUSD: RiskLevel.LOW,
    YieldSource.LLAMALEND: RiskLevel.MEDIUM,
    YieldSource.CRVUSD_MINT: RiskLevel.MEDIUM,
    YieldSource.STABLE_LP: RiskLevel.MEDIUM,
    YieldSource.BOOSTED_LP: RiskLevel.HIGH,
}

# Bridge cost estimate (from Ethereum, USD)
BRIDGE_COSTS = {
    "ethereum": 0.0,
    "arbitrum": 5.0,
    "optimism": 5.0,
    "fraxtal": 5.0,
}


@dataclass
class PoolInfo:
    name: str
    address: str
    apy: float
    tvl: float
    source: str          # YieldSource value
    chain: str = "ethereum"
    risk: str = "medium"  # RiskLevel value
    base_apy: float = 0.0
    reward_apy: float = 0.0
    gas_cost_usd: float = 15.0
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def net_apy_per_1000(self) -> float:
        """Net APY considering gas costs for a $1000 position (annualized)."""
        gas_drag = (self.gas_cost_usd / 1000) * 100  # as percentage
        return max(self.apy - gas_drag, 0)


class YieldOptimizer:
    """crvUSD yield monitoring and optimization — multi-chain, multi-source.

    Sources: scrvUSD, LlamaLend, crvUSD mint markets, Curve LP, Convex boosted LP.
    Chains: Ethereum, Arbitrum, Optimism, Fraxtal.
    """

    LENDING_MARKETS_URL = "https://prices.curve.finance/v1/lending/markets/{chain}"
    CRVUSD_MARKETS_URL = "https://prices.curve.finance/v1/crvusd/markets/ethereum"
    DEFILLAMA_POOLS_URL = "https://yields.llama.fi/pools"
    CURVE_POOLS_URL = "https://api.curve.fi/api/getPools/{chain}/factory-crvusd"

    def __init__(
        self,
        rebalance_threshold: float = REBALANCE_THRESHOLD,
        chains: list[str] | None = None,
    ):
        self.rebalance_threshold = rebalance_threshold
        self.chains = chains or [c["name"] for c in SUPPORTED_CHAINS]
        self._chain_map = {c["name"]: c for c in SUPPORTED_CHAINS}

    async def run(self, task: dict) -> dict:
        """Main entry point. Fetches all yield sources, compares, returns strategy.

        Args:
            task: dict with optional keys:
                - current_pool: address of user's current pool
                - min_tvl: minimum TVL filter (default 100_000)
                - chains: list of chain names to query (default: all)
                - risk_filter: max risk level ("low", "medium", "high")
                - position_size: USD amount for gas-adjusted calcs (default 10_000)
        """
        current_pool = task.get("current_pool")
        min_tvl = task.get("min_tvl", 100_000)
        chains = task.get("chains", self.chains)
        risk_filter = task.get("risk_filter", "high")
        position_size = task.get("position_size", 10_000)

        async with httpx.AsyncClient(
            timeout=30, follow_redirects=True
        ) as client:
            # Fetch all sources in parallel
            results = await asyncio.gather(
                self._fetch_scrvusd_rate(client),
                self._fetch_llamalend_markets(client, chains),
                self._fetch_crvusd_mint_markets(client),
                self._fetch_boosted_lp_yields(client),
                return_exceptions=True,
            )

        all_pools: list[PoolInfo] = []
        source_names = ["scrvusd", "llamalend", "crvusd_mint", "boosted_lp"]
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error("Failed to fetch %s: %s", source_names[i], result)
            elif isinstance(result, list):
                all_pools.extend(result)

        # Apply filters
        risk_order = [RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH]
        max_risk_idx = next(
            (i for i, r in enumerate(risk_order) if r.value == risk_filter),
            len(risk_order) - 1,
        )
        allowed_risks = {r.value for r in risk_order[: max_risk_idx + 1]}

        filtered = [
            p for p in all_pools
            if p.tvl >= min_tvl and p.risk in allowed_risks
        ]
        if not filtered:
            filtered = [p for p in all_pools if p.risk in allowed_risks] or all_pools

        # Sort by APY descending
        filtered.sort(key=lambda p: p.apy, reverse=True)

        best = filtered[0] if filtered else None

        # Current pool lookup
        current_apy = 0.0
        current_info = None
        if current_pool:
            current_info = next(
                (p for p in all_pools if p.address.lower() == current_pool.lower()),
                None,
            )
            if current_info:
                current_apy = current_info.apy

        # Strategy decision
        strategy, rebalance_target, rationale = self._decide_strategy(
            current_apy, current_info, best, position_size
        )

        # Group by source for summary
        by_source = {}
        for p in all_pools:
            by_source.setdefault(p.source, []).append(p)

        source_summary = {
            src: {
                "count": len(pools),
                "best_apy": max(p.apy for p in pools) if pools else 0,
                "avg_apy": sum(p.apy for p in pools) / len(pools) if pools else 0,
            }
            for src, pools in by_source.items()
        }

        # Group by chain
        by_chain = {}
        for p in all_pools:
            by_chain.setdefault(p.chain, []).append(p)

        chain_summary = {
            chain: {
                "count": len(pools),
                "best_apy": max(p.apy for p in pools) if pools else 0,
            }
            for chain, pools in by_chain.items()
        }

        return {
            "pools": [p.to_dict() for p in filtered[:30]],
            "total_pools_found": len(all_pools),
            "best_yield": best.to_dict() if best else None,
            "current_pool": current_info.to_dict() if current_info else None,
            "current_apy": current_apy,
            "strategy": strategy,
            "rationale": rationale,
            "rebalance_target": rebalance_target,
            "rebalance_needed": strategy == "rebalance",
            "threshold": self.rebalance_threshold,
            "source_summary": source_summary,
            "chain_summary": chain_summary,
            "chains_queried": chains,
        }

    def _decide_strategy(
        self,
        current_apy: float,
        current_info: PoolInfo | None,
        best: PoolInfo | None,
        position_size: float,
    ) -> tuple[str, dict | None, str]:
        """Decide hold/rebalance/enter with rationale."""
        if not best:
            return "hold", None, "No yield opportunities found"

        if current_info is None:
            return (
                "enter",
                best.to_dict(),
                f"No current position. Best: {best.name} at {best.apy:.2f}% "
                f"({best.source}, {best.chain}, risk={best.risk})",
            )

        if best.address.lower() == current_info.address.lower():
            return "hold", None, f"Already in best pool ({best.name} at {best.apy:.2f}%)"

        if current_apy <= 0:
            return (
                "rebalance",
                best.to_dict(),
                f"Current pool has 0% APY. Move to {best.name} at {best.apy:.2f}%",
            )

        ratio = best.apy / current_apy
        # Factor in gas + bridge costs
        switch_cost = best.gas_cost_usd + BRIDGE_COSTS.get(best.chain, 0)
        if current_info.chain != best.chain:
            switch_cost += BRIDGE_COSTS.get(best.chain, 5.0)

        # Break-even: how many days to recoup switch cost
        apy_diff = best.apy - current_apy
        if apy_diff > 0 and position_size > 0:
            daily_gain = (apy_diff / 100) * position_size / 365
            breakeven_days = switch_cost / daily_gain if daily_gain > 0 else 999
        else:
            breakeven_days = 999

        if ratio >= self.rebalance_threshold and breakeven_days < 30:
            return (
                "rebalance",
                best.to_dict(),
                f"Switch from {current_info.name} ({current_apy:.2f}%) to "
                f"{best.name} ({best.apy:.2f}%) on {best.chain}. "
                f"Break-even in {breakeven_days:.0f} days. "
                f"Gas+bridge cost: ${switch_cost:.1f}",
            )

        if ratio >= self.rebalance_threshold:
            return (
                "hold",
                best.to_dict(),
                f"Better yield exists ({best.name} at {best.apy:.2f}%) but "
                f"break-even is {breakeven_days:.0f} days (>30). Hold current.",
            )

        return (
            "hold",
            None,
            f"Current {current_info.name} at {current_apy:.2f}% is within "
            f"threshold of best ({best.apy:.2f}%)",
        )

    # ─── Fetchers ────────────────────────────────────────────────

    async def _fetch_scrvusd_rate(
        self, client: httpx.AsyncClient
    ) -> list[PoolInfo]:
        """Fetch scrvUSD savings vault APY from DefiLlama."""
        try:
            resp = await client.get(self.DEFILLAMA_POOLS_URL)
            resp.raise_for_status()
            data = resp.json().get("data", [])

            # Find scrvUSD vault (project=crvusd, symbol=SCRVUSD, Ethereum)
            for pool in data:
                if (
                    pool.get("project") == "crvusd"
                    and pool.get("symbol", "").upper() == "SCRVUSD"
                    and pool.get("chain") == "Ethereum"
                ):
                    apy = self._parse_float(pool.get("apy", 0))
                    tvl = self._parse_float(pool.get("tvlUsd", 0))
                    logger.info("scrvUSD: APY=%.2f%%, TVL=$%,.0f", apy, tvl)
                    return [
                        PoolInfo(
                            name="scrvUSD (Savings Vault)",
                            address="0x0655977FEb2f289A4aB78af67BAB0d17aAb84367",
                            apy=apy,
                            tvl=tvl,
                            source=YieldSource.SCRVUSD.value,
                            chain="ethereum",
                            risk=RiskLevel.LOW.value,
                            base_apy=apy,
                            gas_cost_usd=15.0,
                            extra={
                                "description": "Passive crvUSD savings vault. "
                                "Yield from lending fees. Single-sided, no IL.",
                                "apy_1d_change": self._parse_float(
                                    pool.get("apyPct1D", 0)
                                ),
                                "apy_7d_change": self._parse_float(
                                    pool.get("apyPct7D", 0)
                                ),
                            },
                        )
                    ]

            logger.warning("scrvUSD pool not found in DefiLlama data")
            return []
        except Exception as exc:
            logger.error("Failed to fetch scrvUSD rate: %s", exc)
            return []

    async def _fetch_llamalend_markets(
        self, client: httpx.AsyncClient, chains: list[str]
    ) -> list[PoolInfo]:
        """Fetch LlamaLend deposit markets across chains.

        API: prices.curve.finance/v1/lending/markets/{chain}
        These have a direct lend_apy field.
        """
        tasks = [
            self._fetch_llamalend_chain(client, chain)
            for chain in chains
            if chain in self._chain_map
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        pools: list[PoolInfo] = []
        for result in results:
            if isinstance(result, list):
                pools.extend(result)
            elif isinstance(result, Exception):
                logger.error("LlamaLend fetch error: %s", result)
        return pools

    async def _fetch_llamalend_chain(
        self, client: httpx.AsyncClient, chain: str
    ) -> list[PoolInfo]:
        """Fetch LlamaLend markets for a single chain."""
        chain_info = self._chain_map.get(chain)
        if not chain_info:
            return []

        url = self.LENDING_MARKETS_URL.format(chain=chain)
        try:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json().get("data", [])
            pools = []

            for m in data:
                lend_apy = self._parse_float(m.get("lend_apy", 0))
                borrowed_token = m.get("borrowed_token", {})
                collateral_token = m.get("collateral_token", {})
                borrowed_symbol = borrowed_token.get("symbol", "?")
                collateral_symbol = collateral_token.get("symbol", "?")
                name = m.get("name", f"{collateral_symbol}/{borrowed_symbol}")

                total_assets_usd = self._parse_float(
                    m.get("total_assets_usd", 0)
                )

                # CRV rewards (if any)
                crv_0 = self._parse_float(m.get("lend_apr_crv_0_boost", 0))
                crv_max = self._parse_float(m.get("lend_apr_crv_max_boost", 0))
                extra_rewards = m.get("extra_reward_apr", [])

                # Skip markets with negligible TVL — APY is unreliable
                if total_assets_usd < 1000:
                    continue

                pools.append(
                    PoolInfo(
                        name=f"LlamaLend: {name}",
                        address=m.get("vault", m.get("controller", "")),
                        apy=lend_apy,
                        tvl=total_assets_usd,
                        source=YieldSource.LLAMALEND.value,
                        chain=chain,
                        risk=RiskLevel.MEDIUM.value,
                        base_apy=lend_apy,
                        reward_apy=crv_max,
                        gas_cost_usd=chain_info["gas_cost_usd"],
                        extra={
                            "borrow_apy": self._parse_float(
                                m.get("borrow_apy", 0)
                            ),
                            "n_loans": m.get("n_loans", 0),
                            "utilization": (
                                self._parse_float(m.get("total_debt_usd", 0))
                                / total_assets_usd
                                if total_assets_usd > 0
                                else 0
                            ),
                            "crv_boost_range": [crv_0, crv_max],
                            "extra_rewards": extra_rewards,
                            "borrowed_token": borrowed_symbol,
                            "collateral_token": collateral_symbol,
                        },
                    )
                )

            logger.info(
                "Fetched %d LlamaLend markets on %s", len(pools), chain
            )
            return pools
        except Exception as exc:
            logger.error(
                "Failed to fetch LlamaLend markets for %s: %s", chain, exc
            )
            return []

    async def _fetch_crvusd_mint_markets(
        self, client: httpx.AsyncClient
    ) -> list[PoolInfo]:
        """Fetch crvUSD minting markets (Ethereum only).

        These are borrow markets — lend APY is derived from borrow rate * utilization.
        Separate from LlamaLend deposit markets.
        """
        try:
            resp = await client.get(self.CRVUSD_MARKETS_URL)
            resp.raise_for_status()
            data = resp.json().get("data", [])
            pools = []

            for m in data:
                borrow_apy = self._parse_float(m.get("borrow_apy", 0))
                collateral = m.get("collateral_token", {})
                symbol = collateral.get("symbol", "?")

                total_debt = self._parse_float(m.get("total_debt_usd", 0))
                debt_ceiling = self._parse_float(m.get("debt_ceiling", 0))
                total_value = total_debt + self._parse_float(
                    m.get("collateral_amount_usd", 0)
                )

                # crvUSD Mint markets: borrow_apy is what BORROWERS PAY,
                # not what depositors earn. Fees flow to scrvUSD holders.
                # Show as info-only with apy=0 (no direct yield to user).
                pools.append(
                    PoolInfo(
                        name=f"crvUSD Mint: {symbol}",
                        address=m.get("address", ""),
                        apy=0.0,  # No direct yield — fees go to scrvUSD
                        tvl=total_value,
                        source=YieldSource.CRVUSD_MINT.value,
                        chain="ethereum",
                        risk=RiskLevel.MEDIUM.value,
                        base_apy=0.0,
                        gas_cost_usd=15.0,
                        extra={
                            "type": "minting_market",
                            "borrow_apy": borrow_apy,
                            "note": "Borrow rate (cost to borrowers). "
                            "Fees flow to scrvUSD holders, not direct yield.",
                            "total_debt_usd": total_debt,
                            "debt_ceiling": debt_ceiling,
                            "collateral_symbol": symbol,
                            "n_loans": m.get("n_loans", 0),
                        },
                    )
                )

            logger.info("Fetched %d crvUSD mint markets", len(pools))
            return pools
        except Exception as exc:
            logger.error("Failed to fetch crvUSD mint markets: %s", exc)
            return []

    async def _fetch_boosted_lp_yields(
        self, client: httpx.AsyncClient
    ) -> list[PoolInfo]:
        """Fetch Convex-boosted crvUSD LP pool yields from DefiLlama.

        Uses DefiLlama because it provides normalized APY data with
        pool names, making it easier than cross-referencing Convex numeric IDs
        with Curve pool addresses.
        """
        try:
            resp = await client.get(self.DEFILLAMA_POOLS_URL)
            resp.raise_for_status()
            data = resp.json().get("data", [])

            pools = []
            for pool in data:
                project = pool.get("project", "")
                symbol = pool.get("symbol", "").upper()
                chain = pool.get("chain", "").lower()

                # Filter: Convex or StakeDAO, crvUSD-related
                is_convex = project == "convex-finance"
                is_stakedao = project == "stakedao"
                has_crvusd = "CRVUSD" in symbol or "SCRVUSD" in symbol

                if not (is_convex or is_stakedao) or not has_crvusd:
                    continue

                apy = self._parse_float(pool.get("apy", 0))
                tvl = self._parse_float(pool.get("tvlUsd", 0))
                base_apy = self._parse_float(pool.get("apyBase", 0))
                reward_apy = self._parse_float(pool.get("apyReward", 0))

                # Map chain name
                chain_lower = chain.lower()
                chain_key = chain_lower if chain_lower in self._chain_map else "ethereum"
                chain_info = self._chain_map.get(chain_key, SUPPORTED_CHAINS[0])

                platform = "Convex" if is_convex else "StakeDAO"
                reward_tokens = pool.get("rewardTokens") or []

                pools.append(
                    PoolInfo(
                        name=f"{platform}: {symbol}",
                        address=pool.get("pool", ""),
                        apy=apy,
                        tvl=tvl,
                        source=YieldSource.BOOSTED_LP.value,
                        chain=chain_key,
                        risk=RiskLevel.HIGH.value,
                        base_apy=base_apy,
                        reward_apy=reward_apy,
                        gas_cost_usd=chain_info["gas_cost_usd"],
                        extra={
                            "platform": platform,
                            "project": project,
                            "reward_tokens": reward_tokens,
                            "il_risk": pool.get("ilRisk", "unknown"),
                            "exposure": pool.get("exposure", "unknown"),
                            "stablecoin": pool.get("stablecoin", False),
                            "apy_1d_change": self._parse_float(
                                pool.get("apyPct1D", 0)
                            ),
                            "apy_7d_change": self._parse_float(
                                pool.get("apyPct7D", 0)
                            ),
                        },
                    )
                )

            logger.info("Fetched %d boosted LP pools", len(pools))
            return pools
        except Exception as exc:
            logger.error("Failed to fetch boosted LP yields: %s", exc)
            return []

    # ─── Helpers ─────────────────────────────────────────────────

    @staticmethod
    def _parse_float(value) -> float:
        """Safely parse a numeric value to float."""
        if value is None:
            return 0.0
        try:
            return float(value)
        except (ValueError, TypeError):
            return 0.0

    def get_supported_chains(self) -> list[dict]:
        """Return list of supported chains with metadata."""
        return [
            c for c in SUPPORTED_CHAINS
            if c["name"] in self.chains
        ]

    def status(self) -> str:
        return "ready"
