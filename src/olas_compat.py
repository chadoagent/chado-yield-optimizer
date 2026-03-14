"""Olas SDK compatibility layer.

Adds Olas-standard conventions on top of the existing FastAPI app:
- /healthcheck endpoint on port 8716
- Reading private key from ethereum_private_key.txt
- Mapping CONNECTION_CONFIGS_CONFIG_* env vars to internal config
- File logging to log.txt

This module does NOT modify existing endpoints or behavior.
Import and call `setup_olas_compat(app)` from main.py.
"""

import logging
import os
from pathlib import Path


logger = logging.getLogger("olas_compat")


def _setup_file_logging() -> None:
    """Set up file logging to log.txt as required by Olas."""
    log_path = Path("log.txt")
    file_handler = logging.FileHandler(str(log_path), mode="a")
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    root_logger = logging.getLogger()
    root_logger.addHandler(file_handler)
    root_logger.setLevel(logging.INFO)
    logger.info("Olas file logging initialized: %s", log_path.resolve())


def _read_private_key_file() -> str | None:
    """Read private key from ethereum_private_key.txt if it exists.

    Olas deploys agents with the private key written to this file
    in the working directory. Falls back to .env / environment.
    """
    key_file = Path("ethereum_private_key.txt")
    if key_file.exists():
        raw = key_file.read_text().strip()
        if raw:
            logger.info("Loaded private key from ethereum_private_key.txt")
            return raw
    return None


def _map_connection_configs() -> None:
    """Map Olas CONNECTION_CONFIGS_CONFIG_* env vars to internal config.

    Olas Quickstart sets environment like:
      CONNECTION_CONFIGS_CONFIG_SAFE_CONTRACT_ADDRESSES='["0x..."]'
    We map these to the env vars our app expects.
    """
    safe_addr_env = os.environ.get("CONNECTION_CONFIGS_CONFIG_SAFE_CONTRACT_ADDRESSES")
    if safe_addr_env:
        # Olas passes JSON array, extract first address
        import json
        try:
            addresses = json.loads(safe_addr_env)
            if isinstance(addresses, list) and addresses:
                addr = addresses[0]
            elif isinstance(addresses, str):
                addr = addresses
            else:
                addr = str(addresses)

            os.environ.setdefault("SAFE_ADDRESS", addr)
            logger.info("Mapped CONNECTION_CONFIGS Safe address: %s", addr)
        except (json.JSONDecodeError, IndexError) as e:
            logger.warning("Failed to parse CONNECTION_CONFIGS Safe addresses: %s", e)

    # Map other Olas env vars if present
    all_rpc = os.environ.get("CONNECTION_CONFIGS_CONFIG_ETHEREUM_RPC")
    if all_rpc:
        os.environ.setdefault("RPC_URL", all_rpc)
        logger.info("Mapped CONNECTION_CONFIGS RPC URL")


def setup_olas_compat(app) -> None:
    """Register Olas compatibility hooks and endpoints on the FastAPI app.

    Call this once after app creation. It:
    1. Sets up file logging to log.txt
    2. Reads ethereum_private_key.txt if present
    3. Maps CONNECTION_CONFIGS_CONFIG_* env vars
    4. Adds /healthcheck endpoint
    """
    from fastapi.responses import JSONResponse

    # 1. File logging
    _setup_file_logging()

    # 2. Private key from file (Olas convention)
    pk = _read_private_key_file()
    if pk:
        os.environ.setdefault("AGENT_PRIVATE_KEY", pk)

    # 3. Map Olas connection config env vars
    _map_connection_configs()

    # 4. /healthcheck endpoint (Olas standard)
    @app.get("/healthcheck")
    async def olas_healthcheck():
        """Olas-standard health check endpoint.

        Returns minimal JSON status for the Olas deployment
        orchestrator to verify the service is running.
        """
        return JSONResponse(content={
            "status": "ok",
            "agent": "chado-yield-optimizer",
            "version": "0.4.0",
        })

    logger.info("Olas compatibility layer initialized")
