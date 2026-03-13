from __future__ import annotations

from web3 import Web3

from src.config import settings

IDENTITY_REGISTRY_ABI = [
    {
        "inputs": [{"name": "agentURI", "type": "string"}],
        "name": "register",
        "outputs": [{"name": "agentId", "type": "uint256"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "agentId", "type": "uint256"},
            {"name": "agentURI", "type": "string"},
        ],
        "name": "setAgentURI",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [{"name": "agentId", "type": "uint256"}],
        "name": "getAgent",
        "outputs": [
            {"name": "owner", "type": "address"},
            {"name": "agentURI", "type": "string"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
]


class ERC8004Registry:
    def __init__(self, rpc_url: str | None = None, private_key: str | None = None):
        self.w3 = Web3(Web3.HTTPProvider(rpc_url or settings.rpc_url))
        self.private_key = private_key or settings.private_key
        self.contract = self.w3.eth.contract(
            address=Web3.to_checksum_address(settings.identity_registry),
            abi=IDENTITY_REGISTRY_ABI,
        )

    @property
    def account(self) -> str:
        if not self.private_key:
            return ""
        return self.w3.eth.account.from_key(self.private_key).address

    async def register(self, agent_uri: str | None = None) -> int:
        uri = agent_uri or settings.agent_uri
        tx = self.contract.functions.register(uri).build_transaction({
            "from": self.account,
            "nonce": self.w3.eth.get_transaction_count(self.account),
            "chainId": settings.chain_id,
        })
        signed = self.w3.eth.account.sign_transaction(tx, self.private_key)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash)
        # Parse agentId from logs
        return receipt.get("blockNumber", 0)

    async def set_agent_uri(self, agent_id: int, uri: str) -> str:
        tx = self.contract.functions.setAgentURI(agent_id, uri).build_transaction({
            "from": self.account,
            "nonce": self.w3.eth.get_transaction_count(self.account),
            "chainId": settings.chain_id,
        })
        signed = self.w3.eth.account.sign_transaction(tx, self.private_key)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        return tx_hash.hex()

    async def get_agent(self, agent_id: int) -> dict:
        owner, uri = self.contract.functions.getAgent(agent_id).call()
        return {"agent_id": agent_id, "owner": owner, "uri": uri}
