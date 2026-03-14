"""Gnosis Safe transaction manager for crvUSD yield optimizer.

Executes transactions through a Gnosis Safe with a single EOA owner.
Uses raw Web3 + Safe contract ABI (no safe-eth-py dependency).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import IntEnum

from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware
from eth_account import Account
from eth_account.signers.local import LocalAccount

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────

DEFAULT_RPC = "https://ethereum-rpc.publicnode.com"

CRVUSD_ADDRESS = Web3.to_checksum_address("0xf939E0A03FB07F59A73314E73794Be0E57ac1b4E")
SCRVUSD_ADDRESS = Web3.to_checksum_address("0x0655977FEb2f289A4aB78af67BAB0d17aAb84367")

# Minimal ERC-20 ABI
ERC20_ABI = [
    {
        "name": "balanceOf",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "account", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "allowance",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "approve",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
    },
    {
        "name": "decimals",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint8"}],
    },
    {
        "name": "symbol",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "string"}],
    },
]

# scrvUSD vault ABI (ERC-4626 deposit/withdraw)
VAULT_ABI = [
    {
        "name": "deposit",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "assets", "type": "uint256"},
            {"name": "receiver", "type": "address"},
        ],
        "outputs": [{"name": "shares", "type": "uint256"}],
    },
    {
        "name": "withdraw",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "assets", "type": "uint256"},
            {"name": "receiver", "type": "address"},
            {"name": "owner", "type": "address"},
        ],
        "outputs": [{"name": "shares", "type": "uint256"}],
    },
    {
        "name": "redeem",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "shares", "type": "uint256"},
            {"name": "receiver", "type": "address"},
            {"name": "owner", "type": "address"},
        ],
        "outputs": [{"name": "assets", "type": "uint256"}],
    },
    {
        "name": "convertToAssets",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "shares", "type": "uint256"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "convertToShares",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "assets", "type": "uint256"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "totalAssets",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
    },
] + ERC20_ABI  # vault is also ERC-20

# Gnosis Safe minimal ABI for execTransaction
SAFE_ABI = [
    {
        "name": "execTransaction",
        "type": "function",
        "stateMutability": "payable",
        "inputs": [
            {"name": "to", "type": "address"},
            {"name": "value", "type": "uint256"},
            {"name": "data", "type": "bytes"},
            {"name": "operation", "type": "uint8"},
            {"name": "safeTxGas", "type": "uint256"},
            {"name": "baseGas", "type": "uint256"},
            {"name": "gasPrice", "type": "uint256"},
            {"name": "gasToken", "type": "address"},
            {"name": "refundReceiver", "type": "address"},
            {"name": "signatures", "type": "bytes"},
        ],
        "outputs": [{"name": "success", "type": "bool"}],
    },
    {
        "name": "nonce",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "getThreshold",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "getOwners",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "address[]"}],
    },
    {
        "name": "getTransactionHash",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "to", "type": "address"},
            {"name": "value", "type": "uint256"},
            {"name": "data", "type": "bytes"},
            {"name": "operation", "type": "uint8"},
            {"name": "safeTxGas", "type": "uint256"},
            {"name": "baseGas", "type": "uint256"},
            {"name": "gasPrice", "type": "uint256"},
            {"name": "gasToken", "type": "address"},
            {"name": "refundReceiver", "type": "address"},
            {"name": "_nonce", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bytes32"}],
    },
]


class SafeOperation(IntEnum):
    CALL = 0
    DELEGATE_CALL = 1


@dataclass
class TxResult:
    success: bool
    tx_hash: str
    gas_used: int = 0
    error: str = ""


class SafeManager:
    """Manages transactions through a Gnosis Safe (single-owner, 1/1 threshold).

    For hackathon PoC: assumes the EOA is the sole owner with threshold=1.
    Signs and executes Safe transactions directly on-chain.
    """

    def __init__(
        self,
        rpc_url: str | None = None,
        private_key: str | None = None,
        safe_address: str | None = None,
    ):
        self.rpc_url = rpc_url or os.getenv("RPC_URL", DEFAULT_RPC)
        self.w3 = Web3(Web3.HTTPProvider(self.rpc_url))
        # POA middleware for non-mainnet chains
        self.w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

        pk = private_key or os.getenv("AGENT_PRIVATE_KEY", "")
        if not pk:
            raise ValueError("AGENT_PRIVATE_KEY not set")
        self.account: LocalAccount = Account.from_key(pk)
        self.eoa_address = self.account.address

        safe_addr = safe_address or os.getenv("SAFE_ADDRESS", "")
        if not safe_addr:
            raise ValueError("SAFE_ADDRESS not set")
        self.safe_address = Web3.to_checksum_address(safe_addr)

        self.safe_contract = self.w3.eth.contract(
            address=self.safe_address, abi=SAFE_ABI
        )

        logger.info(
            "SafeManager initialized: EOA=%s, Safe=%s, RPC=%s",
            self.eoa_address,
            self.safe_address,
            self.rpc_url,
        )

    # ── Read Methods ───────────────────────────────────────────────

    def get_balances(self) -> dict:
        """Get ETH, crvUSD and scrvUSD balances of the Safe."""
        eth_balance = self.w3.eth.get_balance(self.safe_address)
        eoa_eth = self.w3.eth.get_balance(self.eoa_address)

        crvusd = self.w3.eth.contract(address=CRVUSD_ADDRESS, abi=ERC20_ABI)
        scrvusd = self.w3.eth.contract(address=SCRVUSD_ADDRESS, abi=VAULT_ABI)

        crvusd_balance = crvusd.functions.balanceOf(self.safe_address).call()
        scrvusd_balance = scrvusd.functions.balanceOf(self.safe_address).call()

        # Convert scrvUSD shares to crvUSD equivalent
        scrvusd_as_crvusd = 0
        if scrvusd_balance > 0:
            try:
                scrvusd_as_crvusd = scrvusd.functions.convertToAssets(
                    scrvusd_balance
                ).call()
            except Exception:
                scrvusd_as_crvusd = scrvusd_balance  # fallback 1:1

        return {
            "safe_address": self.safe_address,
            "eoa_address": self.eoa_address,
            "eth_balance": str(self.w3.from_wei(eth_balance, "ether")),
            "eoa_eth_balance": str(self.w3.from_wei(eoa_eth, "ether")),
            "crvusd_balance": str(self.w3.from_wei(crvusd_balance, "ether")),
            "scrvusd_balance": str(self.w3.from_wei(scrvusd_balance, "ether")),
            "scrvusd_as_crvusd": str(self.w3.from_wei(scrvusd_as_crvusd, "ether")),
            "total_crvusd_value": str(
                self.w3.from_wei(crvusd_balance + scrvusd_as_crvusd, "ether")
            ),
        }

    def get_safe_info(self) -> dict:
        """Get Safe configuration info."""
        # Check if Safe is deployed (counterfactual Safes have no bytecode until first tx)
        code = self.w3.eth.get_code(self.safe_address)
        if not code or code == b"" or code == b"\x00":
            return {
                "address": self.safe_address,
                "deployed": False,
                "note": "Safe not yet deployed on-chain (counterfactual). "
                "It will be deployed on first transaction.",
                "eoa_address": self.eoa_address,
            }

        try:
            threshold = self.safe_contract.functions.getThreshold().call()
            owners = self.safe_contract.functions.getOwners().call()
            nonce = self.safe_contract.functions.nonce().call()
            return {
                "address": self.safe_address,
                "deployed": True,
                "threshold": threshold,
                "owners": owners,
                "nonce": nonce,
                "eoa_is_owner": self.eoa_address in owners,
            }
        except Exception as e:
            logger.error("Failed to get Safe info: %s", e)
            return {"address": self.safe_address, "deployed": True, "error": str(e)}

    # ── Write Methods (via Safe) ───────────────────────────────────

    def _exec_safe_tx(
        self,
        to: str,
        value: int = 0,
        data: bytes = b"",
        operation: SafeOperation = SafeOperation.CALL,
        gas_limit: int = 300_000,
    ) -> TxResult:
        """Build, sign and execute a transaction through the Safe.

        For 1/1 Safe: sign the Safe tx hash with the EOA, then call execTransaction.
        """
        to = Web3.to_checksum_address(to)
        nonce = self.safe_contract.functions.nonce().call()

        # Safe tx parameters (no gas refund — simpler)
        safe_tx_gas = 0
        base_gas = 0
        gas_price = 0
        gas_token = "0x" + "00" * 20
        refund_receiver = "0x" + "00" * 20

        # Get the Safe transaction hash
        tx_hash = self.safe_contract.functions.getTransactionHash(
            to,
            value,
            data,
            operation,
            safe_tx_gas,
            base_gas,
            gas_price,
            Web3.to_checksum_address(gas_token),
            Web3.to_checksum_address(refund_receiver),
            nonce,
        ).call()

        # Sign with EOA (eth_sign style: Safe expects signature type = 1)
        # web3 v7: signHash → unsafe_sign_hash
        signed = self.account.unsafe_sign_hash(tx_hash)

        # Construct signature: r (32 bytes) + s (32 bytes) + v (1 byte)
        # For eth_sign: v += 4 (Safe convention)
        signature = (
            signed.r.to_bytes(32, "big")
            + signed.s.to_bytes(32, "big")
            + (signed.v + 4).to_bytes(1, "big")
        )

        # Build the execTransaction call
        exec_tx = self.safe_contract.functions.execTransaction(
            to,
            value,
            data,
            operation,
            safe_tx_gas,
            base_gas,
            gas_price,
            Web3.to_checksum_address(gas_token),
            Web3.to_checksum_address(refund_receiver),
            signature,
        ).build_transaction(
            {
                "from": self.eoa_address,
                "nonce": self.w3.eth.get_transaction_count(self.eoa_address),
                "gas": gas_limit,
                "maxFeePerGas": self.w3.eth.gas_price * 2,
                "maxPriorityFeePerGas": self.w3.to_wei(1, "gwei"),
            }
        )

        # Sign and send
        signed_tx = self.account.sign_transaction(exec_tx)
        tx_hash_sent = self.w3.eth.send_raw_transaction(signed_tx.raw_transaction)

        logger.info("Safe tx sent: %s", tx_hash_sent.hex())

        # Wait for receipt
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash_sent, timeout=120)

        success = receipt["status"] == 1
        if not success:
            logger.error("Safe tx FAILED: %s", tx_hash_sent.hex())

        return TxResult(
            success=success,
            tx_hash=tx_hash_sent.hex(),
            gas_used=receipt["gasUsed"],
            error="" if success else "Transaction reverted",
        )

    def approve_token(
        self,
        token_address: str,
        spender: str,
        amount: int | None = None,
    ) -> TxResult:
        """Approve a spender to use Safe's tokens.

        Args:
            token_address: ERC-20 token to approve.
            spender: Address to approve.
            amount: Amount in wei. None = max uint256 (unlimited).
        """
        if amount is None:
            amount = 2**256 - 1  # max approval

        token = self.w3.eth.contract(
            address=Web3.to_checksum_address(token_address), abi=ERC20_ABI
        )
        data = token.functions.approve(
            Web3.to_checksum_address(spender), amount
        ).build_transaction({"from": self.safe_address})["data"]

        logger.info(
            "Approving %s to spend tokens at %s (amount=%s)",
            spender,
            token_address,
            "unlimited" if amount == 2**256 - 1 else amount,
        )
        return self._exec_safe_tx(to=token_address, data=bytes.fromhex(data[2:]))

    def deposit_to_pool(
        self,
        pool_address: str,
        amount_wei: int,
        pool_type: str = "scrvusd",
    ) -> TxResult:
        """Deposit crvUSD into a yield pool via Safe.

        Args:
            pool_address: Pool/vault contract address.
            amount_wei: Amount of crvUSD in wei.
            pool_type: Pool type — "scrvusd" (ERC-4626) or "llamalend".
        """
        pool_address = Web3.to_checksum_address(pool_address)

        # Check and set approval if needed
        crvusd = self.w3.eth.contract(address=CRVUSD_ADDRESS, abi=ERC20_ABI)
        current_allowance = crvusd.functions.allowance(
            self.safe_address, pool_address
        ).call()
        if current_allowance < amount_wei:
            logger.info("Insufficient allowance, approving first...")
            approve_result = self.approve_token(CRVUSD_ADDRESS, pool_address)
            if not approve_result.success:
                return TxResult(
                    success=False,
                    tx_hash=approve_result.tx_hash,
                    error=f"Approval failed: {approve_result.error}",
                )

        if pool_type == "scrvusd":
            # ERC-4626 deposit(assets, receiver)
            vault = self.w3.eth.contract(address=pool_address, abi=VAULT_ABI)
            data = vault.functions.deposit(
                amount_wei, self.safe_address
            ).build_transaction({"from": self.safe_address})["data"]
        else:
            # Generic: assume deposit(uint256) for other pool types
            # This covers basic Curve pool deposit patterns
            deposit_sig = Web3.keccak(text="deposit(uint256)")[:4]
            data = deposit_sig.hex() + hex(amount_wei)[2:].zfill(64)
            data = bytes.fromhex(data)
            return self._exec_safe_tx(
                to=pool_address, data=data, gas_limit=400_000
            )

        logger.info(
            "Depositing %s wei crvUSD into %s (type=%s)",
            amount_wei,
            pool_address,
            pool_type,
        )
        return self._exec_safe_tx(
            to=pool_address, data=bytes.fromhex(data[2:]), gas_limit=400_000
        )

    def withdraw_from_pool(
        self,
        pool_address: str,
        amount_wei: int,
        pool_type: str = "scrvusd",
        is_shares: bool = False,
    ) -> TxResult:
        """Withdraw from a yield pool via Safe.

        Args:
            pool_address: Pool/vault contract address.
            amount_wei: Amount in wei (assets or shares depending on is_shares).
            pool_type: Pool type — "scrvusd" (ERC-4626) or "llamalend".
            is_shares: If True, use redeem(shares) instead of withdraw(assets).
        """
        pool_address = Web3.to_checksum_address(pool_address)

        if pool_type == "scrvusd":
            vault = self.w3.eth.contract(address=pool_address, abi=VAULT_ABI)
            if is_shares:
                data = vault.functions.redeem(
                    amount_wei, self.safe_address, self.safe_address
                ).build_transaction({"from": self.safe_address})["data"]
            else:
                data = vault.functions.withdraw(
                    amount_wei, self.safe_address, self.safe_address
                ).build_transaction({"from": self.safe_address})["data"]
        else:
            # Generic withdraw(uint256)
            withdraw_sig = Web3.keccak(text="withdraw(uint256)")[:4]
            data = withdraw_sig.hex() + hex(amount_wei)[2:].zfill(64)
            data = bytes.fromhex(data)
            return self._exec_safe_tx(
                to=pool_address, data=data, gas_limit=400_000
            )

        logger.info(
            "Withdrawing %s wei from %s (type=%s, is_shares=%s)",
            amount_wei,
            pool_address,
            pool_type,
            is_shares,
        )
        return self._exec_safe_tx(
            to=pool_address, data=bytes.fromhex(data[2:]), gas_limit=400_000
        )
