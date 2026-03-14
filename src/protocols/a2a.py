"""A2A v1.0 (Agent-to-Agent) protocol implementation.

Implements the A2A specification (https://a2a-protocol.org/latest/specification/) with:
- Task lifecycle (working -> completed/failed/canceled/input-required)
- Messages with parts (TextPart, DataPart, FilePart)
- Artifacts for structured output
- SSE streaming via message/stream
- JSON-RPC 2.0 bindings
- Natural language intent parsing for skill routing

Method names follow the A2A spec:
  message/send, message/stream, tasks/get, tasks/cancel

Legacy method names (SendMessage, GetTask, etc.) are also supported for
backward compatibility.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

import httpx
from pydantic import BaseModel, Field


# ── Task Status ───────────────────────────────────────────────────

class TaskState(str, Enum):
    WORKING = "working"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"
    INPUT_REQUIRED = "input-required"
    AUTH_REQUIRED = "auth-required"
    REJECTED = "rejected"


class TaskStatus(BaseModel):
    state: TaskState
    message: str = ""
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# ── Parts & Messages ─────────────────────────────────────────────

class TextPart(BaseModel):
    type: str = "text"
    text: str


class DataPart(BaseModel):
    type: str = "data"
    data: dict
    mimeType: str = "application/json"


class FilePart(BaseModel):
    type: str = "file"
    name: str
    mimeType: str
    uri: str | None = None
    bytes: str | None = None  # base64


class Message(BaseModel):
    role: str  # "user" or "agent"
    parts: list[dict]
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class Artifact(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    title: str = ""
    parts: list[dict] = []
    mimeType: str = "application/json"


# ── Task ──────────────────────────────────────────────────────────

class Task(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    contextId: str | None = None
    status: TaskStatus = Field(default_factory=lambda: TaskStatus(state=TaskState.WORKING))
    createdAt: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updatedAt: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    history: list[Message] = []
    artifacts: list[Artifact] = []


# ── Task Store (in-memory) ───────────────────────────────────────

class TaskStore:
    """In-memory task storage with SSE subscriber support."""

    def __init__(self, max_tasks: int = 1000):
        self._tasks: dict[str, Task] = {}
        self._subscribers: dict[str, list[asyncio.Queue]] = {}
        self._max_tasks = max_tasks

    def create(self, message: Message, context_id: str | None = None) -> Task:
        task = Task(contextId=context_id, history=[message])
        self._tasks[task.id] = task
        # Evict oldest if over limit
        if len(self._tasks) > self._max_tasks:
            oldest = next(iter(self._tasks))
            del self._tasks[oldest]
        return task

    def get(self, task_id: str) -> Task | None:
        return self._tasks.get(task_id)

    def list_tasks(self, context_id: str | None = None, limit: int = 50) -> list[Task]:
        tasks = list(self._tasks.values())
        if context_id:
            tasks = [t for t in tasks if t.contextId == context_id]
        return tasks[-limit:]

    def update_status(self, task_id: str, state: TaskState, message: str = "") -> Task | None:
        task = self._tasks.get(task_id)
        if not task:
            return None
        task.status = TaskStatus(state=state, message=message)
        task.updatedAt = datetime.now(timezone.utc).isoformat()
        self._notify(task_id, {"type": "status", "task": task.model_dump()})
        return task

    def add_artifact(self, task_id: str, artifact: Artifact) -> Task | None:
        task = self._tasks.get(task_id)
        if not task:
            return None
        task.artifacts.append(artifact)
        task.updatedAt = datetime.now(timezone.utc).isoformat()
        self._notify(task_id, {"type": "artifact", "taskId": task_id, "artifact": artifact.model_dump()})
        return task

    def add_message(self, task_id: str, message: Message) -> Task | None:
        task = self._tasks.get(task_id)
        if not task:
            return None
        task.history.append(message)
        task.updatedAt = datetime.now(timezone.utc).isoformat()
        self._notify(task_id, {"type": "message", "taskId": task_id, "message": message.model_dump()})
        return task

    def subscribe(self, task_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.setdefault(task_id, []).append(q)
        return q

    def unsubscribe(self, task_id: str, q: asyncio.Queue):
        subs = self._subscribers.get(task_id, [])
        if q in subs:
            subs.remove(q)

    def _notify(self, task_id: str, event: dict):
        for q in self._subscribers.get(task_id, []):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass


# Global store
task_store = TaskStore()


# ── Natural Language Intent Parser ────────────────────────────────

# Maps natural language patterns to (skill_id, extracted_params)
_INTENT_PATTERNS: list[tuple[re.Pattern, str, list[str]]] = [
    # Best yield / top pools (with amount before chain)
    (re.compile(
        r"(?:find|get|show|what(?:'s| is| are)?)\s+(?:the\s+)?(?:best|top|highest)\s+"
        r"(?:yield|apy|apr|pools?|opportunities?)"
        r"(?:\s+(?:for|with)\s+(\d[\d,]*)\s*(?:crvusd|usd|\$))?"
        r"(?:\s+(?:on|in)\s+([a-zA-Z]\w*))?"
        , re.IGNORECASE),
     "best-yield", ["amount", "chain"]),

    # Pool listing
    (re.compile(
        r"(?:list|show|get|find)\s+(?:all\s+)?(?:crvusd\s+)?pools?"
        r"(?:\s+(?:on|for|in)\s+(\w+))?"
        , re.IGNORECASE),
     "pools", ["chain"]),

    # Risk score
    (re.compile(
        r"(?:risk|assess|evaluate|score|rate|check)\s+(?:score\s+)?(?:for\s+)?(?:pool\s+)?"
        r"(0x[a-fA-F0-9]+|[a-f0-9]{12})"
        , re.IGNORECASE),
     "risk-score", ["pool_id"]),

    # Rebalance
    (re.compile(
        r"(?:rebalance|optimize|suggest|recommend)\s+(?:my\s+)?(?:portfolio|allocation|positions?)"
        r"(?:\s+(\d[\d,]*)\s*(?:crvusd|usd|\$))?"
        , re.IGNORECASE),
     "rebalance", ["amount"]),

    # Generic yield query
    (re.compile(
        r"(?:yield|apy|apr|earn|interest)\s+(?:on|for|in)\s+"
        r"(?:(\d[\d,]*)\s*)?(?:crvusd)"
        , re.IGNORECASE),
     "best-yield", ["amount"]),
]


def parse_intent(text: str) -> tuple[str, dict]:
    """Parse natural language text into a skill ID and parameters.

    Returns:
        (skill_id, params) — skill_id is the matched skill, params are extracted
        parameters. If no intent matches, returns ("best-yield", {}) as default.
    """
    text = text.strip()

    for pattern, skill, param_names in _INTENT_PATTERNS:
        match = pattern.search(text)
        if match:
            params = {}
            for i, name in enumerate(param_names):
                value = match.group(i + 1) if i + 1 <= len(match.groups()) else None
                if value:
                    value = value.strip().replace(",", "")
                    if name == "amount":
                        try:
                            params["position_size"] = float(value)
                        except ValueError:
                            pass
                    elif name == "chain":
                        params["chain"] = value.lower()
                    elif name == "pool_id":
                        params["pool_id"] = value
                    else:
                        params[name] = value
            return skill, params

    # Default: best-yield
    return "best-yield", {}


# ── JSON-RPC 2.0 ─────────────────────────────────────────────────

# A2A-specific error codes per spec
TASK_NOT_FOUND_CODE = -32001
TASK_NOT_CANCELABLE_CODE = -32002
INVALID_PARAMS_CODE = -32602
METHOD_NOT_FOUND_CODE = -32601
INTERNAL_ERROR_CODE = -32603


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


# Method name aliases: A2A spec names -> internal handler names
_METHOD_ALIASES = {
    # A2A spec method names
    "message/send": "message/send",
    "message/stream": "message/stream",
    "tasks/get": "tasks/get",
    "tasks/cancel": "tasks/cancel",
    # Legacy (backward compat)
    "SendMessage": "message/send",
    "GetTask": "tasks/get",
    "ListTasks": "tasks/list",
    "CancelTask": "tasks/cancel",
    "SendStreamingMessage": "message/stream",
}


async def handle_a2a_request(request_data: dict, handlers: dict) -> dict:
    """Process A2A JSON-RPC 2.0 request with task lifecycle support.

    Supports A2A spec methods (message/send, tasks/get, tasks/cancel)
    and legacy aliases (SendMessage, GetTask, CancelTask) for backward compat.
    """
    try:
        req = A2ARequest(**request_data)
        method = _METHOD_ALIASES.get(req.method, req.method)

        if method == "message/send":
            return await _handle_message_send(req, handlers)
        elif method == "tasks/get":
            return _handle_tasks_get(req)
        elif method == "tasks/list":
            return _handle_tasks_list(req)
        elif method == "tasks/cancel":
            return _handle_tasks_cancel(req)
        elif method == "message/stream":
            # Streaming is handled at the endpoint level, not here
            return A2AResponse(
                error={
                    "code": -32600,
                    "message": "Use the /a2a/stream endpoint for message/stream",
                },
                id=req.id,
            ).model_dump()

        # Direct skill dispatch (non-A2A legacy methods)
        handler = handlers.get(req.method)
        if not handler:
            return A2AResponse(
                error={"code": METHOD_NOT_FOUND_CODE, "message": f"Method not found: {req.method}"},
                id=req.id,
            ).model_dump()

        result = await handler(req.params)
        return A2AResponse(result=result, id=req.id).model_dump()
    except Exception as e:
        return A2AResponse(
            error={"code": INTERNAL_ERROR_CODE, "message": str(e)},
            id=request_data.get("id", 0),
        ).model_dump()


async def _handle_message_send(req: A2ARequest, handlers: dict) -> dict:
    """Handle message/send (A2A spec) — create task, route to skill, return result.

    Supports three ways to specify the skill:
    1. Explicit: params.skill = "best-yield"
    2. Configuration: params.configuration.skill = "pools"
    3. Natural language: extract intent from TextPart content
    """
    params = req.params
    message_data = params.get("message", {})

    # Build user message
    parts = message_data.get("parts", [])
    if not parts and "text" in params:
        parts = [{"type": "text", "text": params["text"]}]
    if not parts:
        return A2AResponse(
            error={"code": INVALID_PARAMS_CODE, "message": "message with parts is required"},
            id=req.id,
        ).model_dump()

    user_msg = Message(role="user", parts=parts)

    # Determine skill: explicit > configuration > NLP intent from text
    skill = params.get("skill")
    handler_params = params.get("params", {})

    if not skill:
        config = params.get("configuration", {})
        skill = config.get("skill")

    if not skill:
        # Try to parse intent from text parts
        text_content = " ".join(
            p.get("text", "") for p in parts if p.get("type") == "text"
        ).strip()
        if text_content:
            skill, nlp_params = parse_intent(text_content)
            # NLP params are defaults, explicit params override
            merged = {**nlp_params, **handler_params}
            handler_params = merged

    skill = skill or "best-yield"

    # Extract params from DataPart if present
    for part in parts:
        if part.get("type") == "data":
            handler_params.update(part.get("data", {}))

    # Create task
    task = task_store.create(user_msg, context_id=params.get("contextId"))

    # Execute via handler
    handler = handlers.get(skill)
    if not handler:
        task_store.update_status(task.id, TaskState.FAILED, f"Unknown skill: {skill}")
        return A2AResponse(result=task.model_dump(), id=req.id).model_dump()

    try:
        result = await handler(handler_params)

        # Create artifact with result
        artifact = Artifact(
            title=f"{skill} result",
            parts=[{"type": "data", "data": result, "mimeType": "application/json"}],
        )
        task_store.add_artifact(task.id, artifact)

        # Generate human-readable summary for agent response
        summary = _generate_summary(skill, result)
        agent_msg = Message(
            role="agent",
            parts=[{"type": "text", "text": summary}],
        )
        task_store.add_message(task.id, agent_msg)
        task_store.update_status(task.id, TaskState.COMPLETED)
    except Exception as e:
        task_store.update_status(task.id, TaskState.FAILED, str(e))

    return A2AResponse(result=task.model_dump(), id=req.id).model_dump()


def _generate_summary(skill: str, result: dict) -> str:
    """Generate a human-readable summary of skill results."""
    if skill == "best-yield":
        pools = result.get("pools", [])
        if not pools:
            return "No yield opportunities found matching your criteria."
        top = pools[0]
        return (
            f"Found {len(pools)} yield opportunities. "
            f"Best: {top.get('name', '?')} at {top.get('apy', 0):.2f}% APY "
            f"on {top.get('chain', 'ethereum')} "
            f"(TVL: ${top.get('tvl', 0):,.0f}, risk: {top.get('risk', '?')})."
        )
    elif skill == "pools":
        total = result.get("total", 0)
        return f"Found {total} crvUSD yield pools."
    elif skill == "risk-score":
        return (
            f"Risk score for {result.get('pool_name', '?')}: "
            f"{result.get('risk_score', '?')}/100 ({result.get('risk_level', '?')}). "
            f"{result.get('recommendation', '')}"
        )
    elif skill == "rebalance":
        return (
            f"Strategy: {result.get('strategy', '?')}. "
            f"Expected blended APY: {result.get('expected_blended_apy', 0):.2f}%. "
            f"{result.get('rationale', '')}"
        )
    return f"Completed {skill} successfully."


def _handle_tasks_get(req: A2ARequest) -> dict:
    """Handle tasks/get — return task by ID."""
    task_id = req.params.get("taskId") or req.params.get("id")
    if not task_id:
        return A2AResponse(
            error={"code": INVALID_PARAMS_CODE, "message": "taskId is required"},
            id=req.id,
        ).model_dump()

    task = task_store.get(task_id)
    if not task:
        return A2AResponse(
            error={"code": TASK_NOT_FOUND_CODE, "message": f"Task not found: {task_id}"},
            id=req.id,
        ).model_dump()

    return A2AResponse(result=task.model_dump(), id=req.id).model_dump()


def _handle_tasks_list(req: A2ARequest) -> dict:
    """Handle tasks/list — return tasks, optionally filtered by contextId."""
    context_id = req.params.get("contextId")
    limit = req.params.get("limit", 50)
    tasks = task_store.list_tasks(context_id=context_id, limit=limit)
    return A2AResponse(
        result={"tasks": [t.model_dump() for t in tasks]},
        id=req.id,
    ).model_dump()


def _handle_tasks_cancel(req: A2ARequest) -> dict:
    """Handle tasks/cancel — cancel a running task."""
    task_id = req.params.get("taskId") or req.params.get("id")
    if not task_id:
        return A2AResponse(
            error={"code": INVALID_PARAMS_CODE, "message": "taskId is required"},
            id=req.id,
        ).model_dump()

    task = task_store.get(task_id)
    if not task:
        return A2AResponse(
            error={"code": TASK_NOT_FOUND_CODE, "message": f"Task not found: {task_id}"},
            id=req.id,
        ).model_dump()

    if task.status.state in (TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELED):
        return A2AResponse(
            error={
                "code": TASK_NOT_CANCELABLE_CODE,
                "message": f"Task in terminal state: {task.status.state.value}",
            },
            id=req.id,
        ).model_dump()

    task_store.update_status(task_id, TaskState.CANCELED, "Canceled by client")
    task = task_store.get(task_id)
    return A2AResponse(result=task.model_dump(), id=req.id).model_dump()


# ── SSE Streaming ─────────────────────────────────────────────────

async def stream_task_events(task_id: str, handlers: dict, params: dict):
    """Generator for SSE streaming of task events.

    Yields SSE-formatted strings: "data: {json}\\n\\n"
    """
    skill = params.get("skill", "best-yield")
    message_data = params.get("message", {})
    parts = message_data.get("parts", [])
    if not parts and "text" in params:
        parts = [{"type": "text", "text": params["text"]}]

    user_msg = Message(role="user", parts=parts)
    task = task_store.create(user_msg, context_id=params.get("contextId"))

    # Send initial status
    yield f"data: {json.dumps({'type': 'status', 'task': task.model_dump()})}\n\n"

    # Subscribe for updates
    q = task_store.subscribe(task.id)

    try:
        handler = handlers.get(skill)
        if not handler:
            task_store.update_status(task.id, TaskState.FAILED, f"Unknown skill: {skill}")
            yield f"data: {json.dumps({'type': 'status', 'task': task.model_dump()})}\n\n"
            return

        handler_params = params.get("params", {})
        for part in parts:
            if part.get("type") == "data":
                handler_params.update(part.get("data", {}))

        try:
            result = await handler(handler_params)
            artifact = Artifact(
                title=f"{skill} result",
                parts=[{"type": "data", "data": result, "mimeType": "application/json"}],
            )
            task_store.add_artifact(task.id, artifact)
            summary = _generate_summary(skill, result)
            agent_msg = Message(
                role="agent",
                parts=[{"type": "text", "text": summary}],
            )
            task_store.add_message(task.id, agent_msg)
            task_store.update_status(task.id, TaskState.COMPLETED)
        except Exception as e:
            task_store.update_status(task.id, TaskState.FAILED, str(e))

        # Drain queued events
        while not q.empty():
            event = q.get_nowait()
            yield f"data: {json.dumps(event)}\n\n"

        # Final state
        task = task_store.get(task.id)
        yield f"data: {json.dumps({'type': 'end', 'task': task.model_dump()})}\n\n"
    finally:
        task_store.unsubscribe(task.id, q)


# ── Client ────────────────────────────────────────────────────────

class A2AClient:
    """Client for calling other agents via A2A v1.0 protocol."""

    def __init__(self, base_url: str, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def send_message(
        self,
        skill: str,
        params: dict | None = None,
        text: str = "",
    ) -> dict:
        """Send a message (task) to a remote agent via message/send."""
        parts = []
        if text:
            parts.append({"type": "text", "text": text})
        if params:
            parts.append({"type": "data", "data": params, "mimeType": "application/json"})

        request = A2ARequest(
            method="message/send",
            params={
                "skill": skill,
                "message": {"parts": parts},
                "params": params or {},
            },
        )
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.base_url}/a2a",
                json=request.model_dump(),
            )
            resp.raise_for_status()
            return resp.json()

    async def get_task(self, task_id: str) -> dict:
        """Get task status and results via tasks/get."""
        request = A2ARequest(method="tasks/get", params={"taskId": task_id})
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.base_url}/a2a",
                json=request.model_dump(),
            )
            resp.raise_for_status()
            return resp.json()

    async def list_tasks(self, context_id: str | None = None) -> dict:
        """List tasks, optionally filtered by context."""
        params: dict[str, Any] = {}
        if context_id:
            params["contextId"] = context_id
        request = A2ARequest(method="ListTasks", params=params)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.base_url}/a2a",
                json=request.model_dump(),
            )
            resp.raise_for_status()
            return resp.json()

    async def cancel_task(self, task_id: str) -> dict:
        """Cancel a running task via tasks/cancel."""
        request = A2ARequest(method="tasks/cancel", params={"taskId": task_id})
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.base_url}/a2a",
                json=request.model_dump(),
            )
            resp.raise_for_status()
            return resp.json()

    async def discover(self) -> dict:
        """Fetch agent card from /.well-known/agent.json."""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(f"{self.base_url}/.well-known/agent.json")
            resp.raise_for_status()
            return resp.json()

    # Legacy compatibility
    async def call(self, method: str, params: dict | None = None) -> Any:
        """Send a legacy JSON-RPC 2.0 request."""
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
