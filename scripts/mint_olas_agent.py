#!/usr/bin/env python3
"""
Olas Protocol Agent Registration (Ethereum Mainnet).

Mints an agent NFT on the Olas AgentRegistry via RegistriesManager.
Optionally pins metadata to IPFS via Pinata.

Usage:
    # Dry run (estimate gas only):
    python scripts/mint_olas_agent.py --dry-run

    # Mint with metadata hash:
    python scripts/mint_olas_agent.py --hash <ipfs_cid_bytes32>

    # Pin to IPFS first (needs PINATA_API_KEY env):
    python scripts/mint_olas_agent.py --pin-ipfs

Contract addresses (Ethereum mainnet):
    AgentRegistry:     0x2F1f7D38e4772884b88f3eCd8B6b9faCdC319112
    RegistriesManager: 0x9eC9156dEF5C613B2a7D4c46C383F9B58DfcD6fE
"""

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
from web3 import Web3

# ── Olas Contracts (Ethereum Mainnet) ──────────────────────────────

AGENT_REGISTRY = "0x2F1f7D38e4772884b88f3eCd8B6b9faCdC319112"
REGISTRIES_MANAGER = "0x9eC9156dEF5C613B2a7D4c46C383F9B58DfcD6fE"
DEFAULT_RPC = "https://ethereum-rpc.publicnode.com"

# RegistriesManager ABI — create(unitType, unitOwner, unitHash, dependencies)
# unitType: 0 = component, 1 = agent
MANAGER_ABI = [
    {
        "name": "create",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "unitType", "type": "uint256"},
            {"name": "unitOwner", "type": "address"},
            {"name": "unitHash", "type": "bytes32"},
            {"name": "dependencies", "type": "uint32[]"},
        ],
        "outputs": [{"name": "unitId", "type": "uint256"}],
    },
]

REGISTRY_ABI = [
    {
        "name": "totalSupply",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "ownerOf",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "tokenId", "type": "uint256"}],
        "outputs": [{"name": "", "type": "address"}],
    },
]


def pin_to_ipfs(metadata: dict, api_key: str, secret_key: str) -> str:
    """Pin JSON metadata to IPFS via Pinata. Returns CID."""
    import httpx

    resp = httpx.post(
        "https://api.pinata.cloud/pinning/pinJSONToIPFS",
        json={
            "pinataContent": metadata,
            "pinataMetadata": {"name": "chado-yield-optimizer-agent"},
        },
        headers={
            "pinata_api_key": api_key,
            "pinata_secret_api_key": secret_key,
        },
        timeout=30,
    )
    resp.raise_for_status()
    cid = resp.json()["IpfsHash"]
    print(f"Pinned to IPFS: ipfs://{cid}")
    return cid


def cid_to_bytes32(cid: str) -> bytes:
    """Convert IPFS CID to bytes32 hash for on-chain storage.

    For Olas, the unitHash is typically the SHA256 of the metadata.
    """
    # Simple approach: SHA256 of the CID string
    return hashlib.sha256(cid.encode()).digest()


def metadata_to_bytes32(metadata: dict) -> bytes:
    """Convert metadata dict to bytes32 hash."""
    data = json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(data).digest()


def main():
    parser = argparse.ArgumentParser(description="Mint agent NFT on Olas AgentRegistry")
    parser.add_argument("--rpc", help="Ethereum RPC URL")
    parser.add_argument("--key", help="Private key (overrides AGENT_PRIVATE_KEY env)")
    parser.add_argument("--hash", help="Metadata hash (bytes32 hex) for registration")
    parser.add_argument("--pin-ipfs", action="store_true", help="Pin metadata to IPFS via Pinata first")
    parser.add_argument("--dry-run", action="store_true", help="Only estimate gas, don't send tx")
    parser.add_argument("--deps", nargs="*", type=int, default=[], help="Component dependency IDs")
    args = parser.parse_args()

    load_dotenv()

    rpc_url = args.rpc or os.getenv("RPC_URL", DEFAULT_RPC)
    private_key = args.key or os.getenv("AGENT_PRIVATE_KEY")
    if not private_key:
        print("ERROR: No private key. Set AGENT_PRIVATE_KEY or use --key.")
        sys.exit(1)

    w3 = Web3(Web3.HTTPProvider(rpc_url))
    if not w3.is_connected():
        print(f"ERROR: Cannot connect to {rpc_url}")
        sys.exit(1)

    account = w3.eth.account.from_key(private_key)
    balance = w3.eth.get_balance(account.address)
    print(f"Chain ID: {w3.eth.chain_id}")
    print(f"Account: {account.address}")
    print(f"Balance: {w3.from_wei(balance, 'ether')} ETH")

    # Load metadata
    metadata_path = Path(__file__).parent.parent / "agent_metadata.json"
    with open(metadata_path) as f:
        metadata = json.load(f)
    print(f"Metadata: {metadata['name']} v{metadata['version']}")

    # Get unit hash
    if args.pin_ipfs:
        pinata_key = os.getenv("PINATA_API_KEY")
        pinata_secret = os.getenv("PINATA_SECRET_KEY")
        if not pinata_key or not pinata_secret:
            print("ERROR: Set PINATA_API_KEY and PINATA_SECRET_KEY for IPFS pinning.")
            sys.exit(1)
        cid = pin_to_ipfs(metadata, pinata_key, pinata_secret)
        unit_hash = cid_to_bytes32(cid)
    elif args.hash:
        unit_hash = bytes.fromhex(args.hash.removeprefix("0x"))
    else:
        unit_hash = metadata_to_bytes32(metadata)
        print(f"Using metadata SHA256: 0x{unit_hash.hex()}")

    # Check current supply
    registry = w3.eth.contract(
        address=Web3.to_checksum_address(AGENT_REGISTRY), abi=REGISTRY_ABI
    )
    total = registry.functions.totalSupply().call()
    print(f"Current agents on Olas: {total}")

    # Build mint transaction via RegistriesManager
    manager = w3.eth.contract(
        address=Web3.to_checksum_address(REGISTRIES_MANAGER), abi=MANAGER_ABI
    )

    deps = [int(d) for d in args.deps]
    tx = manager.functions.create(
        1,  # unitType=1 for agents
        account.address,
        unit_hash,
        deps,
    ).build_transaction({
        "from": account.address,
        "nonce": w3.eth.get_transaction_count(account.address),
        "chainId": w3.eth.chain_id,
    })

    try:
        gas = w3.eth.estimate_gas(tx)
        tx["gas"] = gas
    except Exception as e:
        print(f"Gas estimation failed: {e}")
        print("This may mean the contract has additional requirements (e.g. OLAS token bond).")
        sys.exit(1)

    gas_price = w3.eth.gas_price
    cost = w3.from_wei(gas * gas_price, "ether")
    print(f"Estimated gas: {gas}")
    print(f"Gas price: {w3.from_wei(gas_price, 'gwei')} gwei")
    print(f"Estimated cost: {cost} ETH")
    affordable = (gas * gas_price) < balance
    print(f"Affordable: {'YES' if affordable else 'NO'}")

    if args.dry_run:
        print("\nDry run complete. Use without --dry-run to mint.")
        return

    if not affordable:
        print("ERROR: Insufficient ETH for gas.")
        sys.exit(1)

    # Sign and send
    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"TX sent: {tx_hash.hex()}")
    print("Waiting for confirmation...")

    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
    if receipt["status"] == 1:
        new_total = registry.functions.totalSupply().call()
        agent_id = new_total  # Our agent is the latest
        print(f"Agent minted! ID: {agent_id}")
        print(f"TX: https://etherscan.io/tx/{tx_hash.hex()}")
    else:
        print(f"TX FAILED: https://etherscan.io/tx/{tx_hash.hex()}")
        sys.exit(1)


if __name__ == "__main__":
    main()
