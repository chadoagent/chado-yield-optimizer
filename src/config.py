from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_prefix": "CHADO_", "env_file": ".env", "extra": "ignore"}

    # Chain config (Base)
    chain_name: str = "base"
    rpc_url: str = "https://mainnet.base.org"
    chain_id: int = 8453

    # ERC-8004 contracts
    identity_registry: str = "0x8004A169FB4a3325136EB29fA0ceB6D2e539a432"
    reputation_registry: str = "0x8004BAa17C55a88189AE136b182e5fdA19dE9b63"

    # Agent identity
    agent_uri: str = "https://chado.studio/yield/agent.json"
    private_key: str = ""

    # Server
    host: str = "0.0.0.0"
    port: int = 8717


class WalletSettings(BaseSettings):
    """Wallet configuration — loaded from .env without prefix."""

    model_config = {"env_file": ".env", "extra": "ignore"}

    agent_private_key: str = ""
    agent_eoa_address: str = ""
    safe_address: str = ""
    rpc_url: str = "https://ethereum-rpc.publicnode.com"

    @property
    def is_configured(self) -> bool:
        return bool(self.agent_private_key and self.safe_address)


settings = Settings()
wallet_settings = WalletSettings()
