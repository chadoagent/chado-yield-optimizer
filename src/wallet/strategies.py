"""Strategy executor — bridges yield recommendations to Safe transactions.

Takes output from YieldOptimizer and executes deposit/withdrawal/rebalance
operations through the SafeManager.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from web3 import Web3

from src.wallet.safe_manager import SafeManager, TxResult, CRVUSD_ADDRESS, SCRVUSD_ADDRESS

logger = logging.getLogger(__name__)


@dataclass
class RebalanceResult:
    """Result of an auto-rebalance operation."""

    action: str  # "deposit", "withdraw", "rebalance", "hold"
    success: bool
    withdraw_tx: TxResult | None = None
    deposit_tx: TxResult | None = None
    amount_moved: str = "0"
    from_pool: str = ""
    to_pool: str = ""
    rationale: str = ""
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "success": self.success,
            "withdraw_tx": (
                {"tx_hash": self.withdraw_tx.tx_hash, "gas_used": self.withdraw_tx.gas_used}
                if self.withdraw_tx
                else None
            ),
            "deposit_tx": (
                {"tx_hash": self.deposit_tx.tx_hash, "gas_used": self.deposit_tx.gas_used}
                if self.deposit_tx
                else None
            ),
            "amount_moved": self.amount_moved,
            "from_pool": self.from_pool,
            "to_pool": self.to_pool,
            "rationale": self.rationale,
            "errors": self.errors,
        }


# Pool type mapping: source name -> pool_type for SafeManager
SOURCE_TO_POOL_TYPE = {
    "scrvusd": "scrvusd",
    "llamalend": "llamalend",
    "stable_lp": "generic",
    "boosted_lp": "generic",
    "crvusd_mint": "generic",
}


class StrategyExecutor:
    """Executes yield strategies through Gnosis Safe.

    Hackathon PoC scope:
    - Deposit crvUSD into scrvUSD savings vault
    - Withdraw from scrvUSD back to crvUSD
    - Auto-rebalance based on optimizer recommendations
    """

    def __init__(self, safe_manager: SafeManager):
        self.safe = safe_manager

    def deposit_scrvusd(self, amount_crvusd: float) -> TxResult:
        """Deposit crvUSD into scrvUSD savings vault.

        Args:
            amount_crvusd: Amount of crvUSD to deposit (in human-readable units, e.g. 10.0).
        """
        amount_wei = Web3.to_wei(amount_crvusd, "ether")
        logger.info("Depositing %.4f crvUSD into scrvUSD vault", amount_crvusd)
        return self.safe.deposit_to_pool(
            pool_address=str(SCRVUSD_ADDRESS),
            amount_wei=amount_wei,
            pool_type="scrvusd",
        )

    def withdraw_scrvusd(self, amount_crvusd: float) -> TxResult:
        """Withdraw crvUSD from scrvUSD savings vault.

        Args:
            amount_crvusd: Amount of crvUSD to withdraw (in human-readable units).
        """
        amount_wei = Web3.to_wei(amount_crvusd, "ether")
        logger.info("Withdrawing %.4f crvUSD from scrvUSD vault", amount_crvusd)
        return self.safe.withdraw_from_pool(
            pool_address=str(SCRVUSD_ADDRESS),
            amount_wei=amount_wei,
            pool_type="scrvusd",
        )

    def auto_rebalance(self, optimizer_result: dict) -> RebalanceResult:
        """Execute rebalance based on optimizer recommendation.

        Takes the output of YieldOptimizer.run() and executes the strategy:
        - "hold": do nothing
        - "enter": deposit into best pool
        - "rebalance": withdraw from current, deposit into best

        For hackathon PoC: only scrvUSD deposits are fully supported.
        Other pool types return a plan without execution.

        Args:
            optimizer_result: Output dict from YieldOptimizer.run().
        """
        strategy = optimizer_result.get("strategy", "hold")
        rebalance_target = optimizer_result.get("rebalance_target")
        current_pool = optimizer_result.get("current_pool")
        rationale = optimizer_result.get("rationale", "")

        if strategy == "hold":
            return RebalanceResult(
                action="hold",
                success=True,
                rationale=rationale,
            )

        if not rebalance_target:
            return RebalanceResult(
                action="hold",
                success=True,
                rationale="No rebalance target identified",
            )

        target_source = rebalance_target.get("source", "")
        target_address = rebalance_target.get("address", "")
        target_pool_type = SOURCE_TO_POOL_TYPE.get(target_source, "generic")

        # For PoC: only execute scrvUSD strategies on-chain
        if target_pool_type != "scrvusd":
            return RebalanceResult(
                action=strategy,
                success=True,
                to_pool=target_address,
                rationale=(
                    f"[DRY RUN] {rationale}. "
                    f"Pool type '{target_source}' execution not yet implemented. "
                    f"Only scrvUSD deposits are supported in this PoC."
                ),
            )

        # Get current balances to determine amount
        balances = self.safe.get_balances()
        available_crvusd = float(balances["crvusd_balance"])

        if strategy == "enter":
            if available_crvusd <= 0:
                return RebalanceResult(
                    action="enter",
                    success=False,
                    rationale="No crvUSD available to deposit",
                    errors=["crvUSD balance is 0"],
                )

            # Deposit all available crvUSD
            deposit_result = self.deposit_scrvusd(available_crvusd)
            return RebalanceResult(
                action="enter",
                success=deposit_result.success,
                deposit_tx=deposit_result,
                amount_moved=str(available_crvusd),
                to_pool=target_address,
                rationale=rationale,
                errors=[deposit_result.error] if deposit_result.error else [],
            )

        if strategy == "rebalance":
            errors = []
            withdraw_result = None
            deposit_result = None

            # Step 1: Withdraw from current pool (if scrvUSD)
            if current_pool:
                current_source = current_pool.get("source", "")
                current_pool_type = SOURCE_TO_POOL_TYPE.get(current_source, "generic")

                if current_pool_type == "scrvusd":
                    scrvusd_balance = float(balances["scrvusd_balance"])
                    if scrvusd_balance > 0:
                        withdraw_result = self.withdraw_scrvusd(
                            float(balances["scrvusd_as_crvusd"])
                        )
                        if not withdraw_result.success:
                            errors.append(f"Withdraw failed: {withdraw_result.error}")
                            return RebalanceResult(
                                action="rebalance",
                                success=False,
                                withdraw_tx=withdraw_result,
                                from_pool=current_pool.get("address", ""),
                                to_pool=target_address,
                                rationale=rationale,
                                errors=errors,
                            )
                else:
                    errors.append(
                        f"Cannot auto-withdraw from '{current_source}' pool — "
                        f"only scrvUSD supported in PoC"
                    )

            # Step 2: Deposit into target pool
            # Re-read balances after withdrawal
            balances = self.safe.get_balances()
            available_crvusd = float(balances["crvusd_balance"])

            if available_crvusd <= 0:
                return RebalanceResult(
                    action="rebalance",
                    success=False,
                    withdraw_tx=withdraw_result,
                    from_pool=current_pool.get("address", "") if current_pool else "",
                    to_pool=target_address,
                    rationale="No crvUSD available after withdrawal",
                    errors=errors + ["crvUSD balance is 0 after withdraw"],
                )

            deposit_result = self.deposit_scrvusd(available_crvusd)
            if not deposit_result.success:
                errors.append(f"Deposit failed: {deposit_result.error}")

            return RebalanceResult(
                action="rebalance",
                success=deposit_result.success,
                withdraw_tx=withdraw_result,
                deposit_tx=deposit_result,
                amount_moved=str(available_crvusd),
                from_pool=current_pool.get("address", "") if current_pool else "",
                to_pool=target_address,
                rationale=rationale,
                errors=errors,
            )

        # Unknown strategy
        return RebalanceResult(
            action=strategy,
            success=False,
            rationale=f"Unknown strategy: {strategy}",
            errors=[f"Unsupported strategy: {strategy}"],
        )
