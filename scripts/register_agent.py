#!/usr/bin/env python3
"""
ERC-8004 Agent Registration on Base chain.

Registers an agent identity by calling IdentityRegistry.register(agentURI).
The agentURI should point to a hosted agent.json file (HTTP or IPFS).

Usage:
    python scripts/register_agent.py --uri https://agent.chado.studio/agent.json
    python scripts/register_agent.py --uri ipfs://Qm.../agent.json
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

# ERC-8004 Identity Registry on Base
IDENTITY_REGISTRY = "0x8004A818BFB912233c491871b3d84c89A494BD9e"

# Minimal ABI for registration
REGISTRY_ABI = [
    {
        "inputs": [{"internalType": "string", "name": "agentURI", "type": "string"}],
        "name": "register",
        "outputs": [{"internalType": "uint256", "name": "agentId", "type": "uint256"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "address", "name": "owner", "type": "address"}],
        "name": "getAgentId",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "internalType": "uint256", "name": "agentId", "type": "uint256"},
            {"indexed": True, "internalType": "address", "name": "owner", "type": "address"},
            {"indexed": False, "internalType": "string", "name": "agentURI", "type": "string"},
        ],
        "name": "AgentRegistered",
        "type": "event",
    },
]


def get_web3(rpc_url: str) -> Web3:
    """Create Web3 instance with POA middleware for Base."""
    w3 = Web3(Web3.HTTPProvider(rpc_url))
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    if not w3.is_connected():
        raise ConnectionError(f"Cannot connect to {rpc_url}")
    return w3


def register_agent(w3: Web3, private_key: str, agent_uri: str) -> dict:
    """Register an agent on the ERC-8004 Identity Registry."""
    account = w3.eth.account.from_key(private_key)
    contract = w3.eth.contract(
        address=Web3.to_checksum_address(IDENTITY_REGISTRY),
        abi=REGISTRY_ABI,
    )

    # Check if already registered
    existing_id = contract.functions.getAgentId(account.address).call()
    if existing_id > 0:
        print(f"Agent already registered with ID: {existing_id}")
        print(f"Owner: {account.address}")
        return {"agent_id": existing_id, "owner": account.address, "already_registered": True}

    # Build transaction
    nonce = w3.eth.get_transaction_count(account.address)
    gas_price = w3.eth.gas_price

    tx = contract.functions.register(agent_uri).build_transaction(
        {
            "from": account.address,
            "nonce": nonce,
            "gasPrice": gas_price,
            "chainId": 8453,  # Base
        }
    )

    # Estimate gas
    tx["gas"] = w3.eth.estimate_gas(tx)

    # Sign and send
    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"Transaction sent: {tx_hash.hex()}")
    print("Waiting for confirmation...")

    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

    if receipt["status"] != 1:
        raise RuntimeError(f"Transaction failed: {receipt}")

    # Parse AgentRegistered event
    logs = contract.events.AgentRegistered().process_receipt(receipt)
    if logs:
        agent_id = logs[0]["args"]["agentId"]
        print(f"Agent registered successfully!")
        print(f"  Agent ID: {agent_id}")
        print(f"  Owner: {account.address}")
        print(f"  URI: {agent_uri}")
        print(f"  TX: https://basescan.org/tx/{tx_hash.hex()}")
        return {
            "agent_id": agent_id,
            "owner": account.address,
            "uri": agent_uri,
            "tx_hash": tx_hash.hex(),
        }

    raise RuntimeError("AgentRegistered event not found in receipt")


def main():
    parser = argparse.ArgumentParser(description="Register agent on ERC-8004 Identity Registry")
    parser.add_argument("--uri", required=True, help="Agent URI (HTTP or IPFS URL to agent.json)")
    parser.add_argument("--rpc", help="Base RPC URL (overrides BASE_RPC_URL env)")
    parser.add_argument("--key", help="Private key (overrides PRIVATE_KEY env)")
    parser.add_argument("--dry-run", action="store_true", help="Only estimate gas, don't send tx")
    args = parser.parse_args()

    load_dotenv()

    rpc_url = args.rpc or os.getenv("BASE_RPC_URL", "https://mainnet.base.org")
    private_key = args.key or os.getenv("PRIVATE_KEY")

    if not private_key:
        print("ERROR: No private key. Set PRIVATE_KEY env or use --key flag.")
        sys.exit(1)

    w3 = get_web3(rpc_url)
    print(f"Connected to Base (chain ID: {w3.eth.chain_id})")

    account = w3.eth.account.from_key(private_key)
    balance = w3.eth.get_balance(account.address)
    print(f"Account: {account.address}")
    print(f"Balance: {w3.from_wei(balance, 'ether')} ETH")

    if balance == 0:
        print("WARNING: Zero balance. You need ETH on Base for gas.")
        if not args.dry_run:
            sys.exit(1)

    if args.dry_run:
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(IDENTITY_REGISTRY),
            abi=REGISTRY_ABI,
        )
        tx = contract.functions.register(args.uri).build_transaction(
            {
                "from": account.address,
                "nonce": w3.eth.get_transaction_count(account.address),
                "gasPrice": w3.eth.gas_price,
                "chainId": 8453,
            }
        )
        gas = w3.eth.estimate_gas(tx)
        cost = w3.from_wei(gas * w3.eth.gas_price, "ether")
        print(f"Estimated gas: {gas}")
        print(f"Estimated cost: {cost} ETH")
        return

    result = register_agent(w3, private_key, args.uri)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
