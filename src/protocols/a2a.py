"""A2A (Agent-to-Agent) protocol - JSON-RPC 2.0 communication."""

from typing import Any

import httpx
from pydantic import BaseModel


class A2ARequest(BaseModel):
    jsonrpc: str = "2.0"
    method: str
    params: dict = {}
    id: int | str = 1


class A2AResponse(BaseModel):
    jsonrpc: str = "2.0"
    result: Any = None
    error: dict | None = None
    id: int | str = 1


class A2AClient:
    """Client for calling other agents via A2A protocol."""

    def __init__(self, base_url: str, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def call(self, method: str, params: dict | None = None) -> Any:
        """Send a JSON-RPC 2.0 request to another agent."""
        request = A2ARequest(method=method, params=params or {})
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.base_url}/a2a",
                json=request.model_dump(),
            )
            resp.raise_for_status()
            response = A2AResponse(**resp.json())
            if response.error:
                raise RuntimeError(f"A2A error: {response.error}")
            return response.result

    async def discover(self) -> dict:
        """Fetch agent card from .well-known/agent.json."""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(f"{self.base_url}/.well-known/agent.json")
            resp.raise_for_status()
            return resp.json()


async def handle_a2a_request(request_data: dict, handlers: dict) -> dict:
    """Process an incoming A2A JSON-RPC 2.0 request.

    Args:
        request_data: Raw JSON-RPC 2.0 request dict.
        handlers: Mapping of method name -> async callable(params) -> result.

    Returns:
        JSON-RPC 2.0 response dict.
    """
    try:
        req = A2ARequest(**request_data)
        handler = handlers.get(req.method)
        if not handler:
            return A2AResponse(
                error={"code": -32601, "message": f"Method not found: {req.method}"},
                id=req.id,
            ).model_dump()

        result = await handler(req.params)
        return A2AResponse(result=result, id=req.id).model_dump()
    except Exception as e:
        return A2AResponse(
            error={"code": -32603, "message": str(e)},
            id=request_data.get("id", 0),
        ).model_dump()
