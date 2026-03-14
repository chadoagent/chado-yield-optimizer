from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_prefix": "CHADO_", "env_file": ".env", "extra": "ignore"}

    # Chain config (Base)
    chain_name: str = "base"
    rpc_url: str = "https://mainnet.base.org"
    chain_id: int = 8453

    # ERC-8004 contracts
    identity_registry: str = "0x8004A818BFB912233c491871b3d84c89A494BD9e"
    reputation_registry: str = "0x8004B663056A597Dffe9eCcC1965A193B7388713"

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
