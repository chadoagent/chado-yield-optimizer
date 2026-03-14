"""API Key authentication and rate limiting with pricing tiers.

Tiers:
  - free:       10 req/min, basic endpoints only (pools, best-yield)
  - pro:        60 req/min, all endpoints + A2A
  - enterprise: 300 req/min, all endpoints + A2A + priority support

API keys stored in api_keys.json. Generate with: python -m src.auth --generate <tier> <name>
No key = free tier with 5 req/min.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from fastapi import Header, HTTPException, Request

# ── Tier Configuration ──────────────────────────────────────────

TIERS = {
    "free": {
        "rate_limit": 10,        # requests per minute
        "rate_window": 60,       # seconds
        "endpoints": {"pools", "best-yield", "health"},
        "a2a_access": False,
        "rebalance_access": False,
        "risk_score_access": True,
        "price_usd": 0,
    },
    "pro": {
        "rate_limit": 60,
        "rate_window": 60,
        "endpoints": {"pools", "best-yield", "risk-score", "rebalance", "a2a"},
        "a2a_access": True,
        "rebalance_access": True,
        "risk_score_access": True,
        "price_usd": 49,
    },
    "enterprise": {
        "rate_limit": 300,
        "rate_window": 60,
        "endpoints": {"pools", "best-yield", "risk-score", "rebalance", "a2a"},
        "a2a_access": True,
        "rebalance_access": True,
        "risk_score_access": True,
        "price_usd": 199,
    },
}

# ── API Key Storage ─────────────────────────────────────────────

_KEYS_FILE = Path(__file__).parent.parent / "api_keys.json"
_keys_cache: dict[str, dict] | None = None
_keys_mtime: float = 0


def _load_keys() -> dict[str, dict]:
    """Load API keys from file, with mtime-based cache."""
    global _keys_cache, _keys_mtime

    if not _KEYS_FILE.exists():
        return {}

    mtime = _KEYS_FILE.stat().st_mtime
    if _keys_cache is not None and mtime == _keys_mtime:
        return _keys_cache

    with open(_KEYS_FILE) as f:
        _keys_cache = json.load(f)
    _keys_mtime = mtime
    return _keys_cache


def _save_keys(keys: dict[str, dict]) -> None:
    global _keys_cache, _keys_mtime
    with open(_KEYS_FILE, "w") as f:
        json.dump(keys, f, indent=2)
    _keys_cache = keys
    _keys_mtime = _KEYS_FILE.stat().st_mtime


def generate_api_key(tier: str, name: str) -> str:
    """Generate a new API key for the given tier."""
    if tier not in TIERS:
        raise ValueError(f"Invalid tier: {tier}. Valid: {list(TIERS.keys())}")

    raw = f"{name}:{tier}:{time.time()}:{os.urandom(16).hex()}"
    key = f"cyo_{hashlib.sha256(raw.encode()).hexdigest()[:32]}"

    keys = _load_keys()
    keys[key] = {
        "name": name,
        "tier": tier,
        "created": int(time.time()),
        "active": True,
    }
    _save_keys(keys)
    return key


# ── Rate Limiter ────────────────────────────────────────────────

_rate_buckets: dict[str, list[float]] = defaultdict(list)


def _check_rate_limit(key: str, tier_config: dict) -> None:
    """Check if request is within rate limit. Raises 429 if exceeded."""
    now = time.time()
    window = tier_config["rate_window"]
    limit = tier_config["rate_limit"]

    bucket = _rate_buckets[key]
    # Prune old entries
    cutoff = now - window
    _rate_buckets[key] = [t for t in bucket if t > cutoff]
    bucket = _rate_buckets[key]

    if len(bucket) >= limit:
        retry_after = int(bucket[0] + window - now) + 1
        raise HTTPException(
            status_code=429,
            detail={
                "error": "Rate limit exceeded",
                "tier": next((t for t, c in TIERS.items() if c is tier_config), "unknown"),
                "limit": f"{limit} requests per {window}s",
                "retry_after": retry_after,
                "upgrade": "Contact us for higher limits: api@chado.studio",
            },
            headers={"Retry-After": str(retry_after)},
        )

    bucket.append(now)


# ── Auth Dependency ─────────────────────────────────────────────

ANONYMOUS_TIER = "free"
ANONYMOUS_RATE_LIMIT = 5  # lower than free tier with key


async def verify_api_key(
    request: Request,
    x_api_key: str | None = Header(None, alias="X-API-Key"),
) -> dict:
    """FastAPI dependency: verify API key and return tier info.

    Returns dict with: tier, tier_config, key, name
    """
    # Also check query param for convenience
    api_key = x_api_key or request.query_params.get("api_key")

    if not api_key:
        # Anonymous access — very limited
        tier_config = {**TIERS[ANONYMOUS_TIER], "rate_limit": ANONYMOUS_RATE_LIMIT}
        _check_rate_limit("__anonymous__" + request.client.host, tier_config)
        return {"tier": "anonymous", "tier_config": tier_config, "key": None, "name": "anonymous"}

    keys = _load_keys()
    key_info = keys.get(api_key)

    if not key_info:
        raise HTTPException(status_code=401, detail="Invalid API key")

    if not key_info.get("active", True):
        raise HTTPException(status_code=403, detail="API key deactivated")

    tier = key_info.get("tier", "free")
    tier_config = TIERS.get(tier, TIERS["free"])

    _check_rate_limit(api_key, tier_config)

    return {
        "tier": tier,
        "tier_config": tier_config,
        "key": api_key,
        "name": key_info.get("name", "unknown"),
    }


def check_endpoint_access(auth_info: dict, endpoint: str) -> None:
    """Check if the tier has access to the given endpoint. Raises 403 if not."""
    tier_config = auth_info["tier_config"]
    allowed = tier_config.get("endpoints", set())

    if endpoint not in allowed:
        raise HTTPException(
            status_code=403,
            detail={
                "error": f"Endpoint '{endpoint}' not available on {auth_info['tier']} tier",
                "current_tier": auth_info["tier"],
                "upgrade": "Upgrade to pro ($49/mo) or enterprise ($199/mo) for full access",
                "pricing_url": "http://51.83.161.121:8717/api/pricing",
            },
        )


# ── CLI ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    if len(sys.argv) >= 4 and sys.argv[1] == "--generate":
        tier = sys.argv[2]
        name = sys.argv[3]
        key = generate_api_key(tier, name)
        print(f"Generated {tier} API key for '{name}':")
        print(f"  {key}")
    elif len(sys.argv) >= 2 and sys.argv[1] == "--list":
        keys = _load_keys()
        if not keys:
            print("No API keys found.")
        else:
            for k, info in keys.items():
                status = "active" if info.get("active", True) else "inactive"
                print(f"  {k[:12]}... | {info['tier']:12s} | {info['name']:20s} | {status}")
    elif len(sys.argv) >= 2 and sys.argv[1] == "--pricing":
        print("crvUSD Yield Optimizer API — Pricing Tiers\n")
        for name, config in TIERS.items():
            print(f"  {name.upper():12s}  ${config['price_usd']}/mo")
            print(f"    Rate limit:  {config['rate_limit']} req/min")
            print(f"    A2A access:  {'Yes' if config['a2a_access'] else 'No'}")
            print(f"    Rebalance:   {'Yes' if config['rebalance_access'] else 'No'}")
            print()
    else:
        print("Usage:")
        print("  python -m src.auth --generate <tier> <name>")
        print("  python -m src.auth --list")
        print("  python -m src.auth --pricing")
        print(f"\nTiers: {list(TIERS.keys())}")
