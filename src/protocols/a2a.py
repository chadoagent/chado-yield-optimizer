"""A2A v1.0 (Agent-to-Agent) protocol implementation.

Implements the A2A specification with:
- Task lifecycle (WORKING → COMPLETED/FAILED/CANCELED)
- Messages with parts (TextPart, DataPart)
- Artifacts for structured output
- SSE streaming via SendStreamingMessage
- JSON-RPC 2.0 and REST bindings
"""

from __future__ import annotations

import asyncio
import json
import time
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


# ── JSON-RPC 2.0 ─────────────────────────────────────────────────

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


async def handle_a2a_request(request_data: dict, handlers: dict) -> dict:
    """Process A2A JSON-RPC 2.0 request with task lifecycle support.

    Supports both legacy methods (optimize, yields, etc.) and
    A2A v1.0 methods (SendMessage, GetTask, ListTasks, CancelTask).
    """
    try:
        req = A2ARequest(**request_data)

        # A2A v1.0 built-in methods
        if req.method == "SendMessage":
            return await _handle_send_message(req, handlers)
        elif req.method == "GetTask":
            return _handle_get_task(req)
        elif req.method == "ListTasks":
            return _handle_list_tasks(req)
        elif req.method == "CancelTask":
            return _handle_cancel_task(req)

        # Legacy method dispatch
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


async def _handle_send_message(req: A2ARequest, handlers: dict) -> dict:
    """Handle SendMessage — create task, execute, return result."""
    params = req.params
    skill = params.get("skill", "optimize")
    message_data = params.get("message", {})

    # Build user message
    parts = message_data.get("parts", [])
    if not parts and "text" in params:
        parts = [{"type": "text", "text": params["text"]}]
    user_msg = Message(role="user", parts=parts)

    # Create task
    task = task_store.create(user_msg, context_id=params.get("contextId"))

    # Execute via handler
    handler = handlers.get(skill)
    if not handler:
        task_store.update_status(task.id, TaskState.FAILED, f"Unknown skill: {skill}")
        return A2AResponse(result={"task": task.model_dump()}, id=req.id).model_dump()

    try:
        # Extract params from DataPart if present
        handler_params = params.get("params", {})
        for part in parts:
            if part.get("type") == "data":
                handler_params.update(part.get("data", {}))

        result = await handler(handler_params)

        # Create artifact with result
        artifact = Artifact(
            title=f"{skill} result",
            parts=[{"type": "data", "data": result, "mimeType": "application/json"}],
        )
        task_store.add_artifact(task.id, artifact)

        # Agent response message
        agent_msg = Message(
            role="agent",
            parts=[{"type": "text", "text": f"Completed {skill} successfully."}],
        )
        task_store.add_message(task.id, agent_msg)
        task_store.update_status(task.id, TaskState.COMPLETED)
    except Exception as e:
        task_store.update_status(task.id, TaskState.FAILED, str(e))

    return A2AResponse(result={"task": task.model_dump()}, id=req.id).model_dump()


def _handle_get_task(req: A2ARequest) -> dict:
    """Handle GetTask — return task by ID."""
    task_id = req.params.get("taskId") or req.params.get("id")
    if not task_id:
        return A2AResponse(
            error={"code": -32602, "message": "taskId required"},
            id=req.id,
        ).model_dump()

    task = task_store.get(task_id)
    if not task:
        return A2AResponse(
            error={"code": -32001, "message": f"Task not found: {task_id}"},
            id=req.id,
        ).model_dump()

    return A2AResponse(result={"task": task.model_dump()}, id=req.id).model_dump()


def _handle_list_tasks(req: A2ARequest) -> dict:
    """Handle ListTasks — return tasks, optionally filtered by contextId."""
    context_id = req.params.get("contextId")
    limit = req.params.get("limit", 50)
    tasks = task_store.list_tasks(context_id=context_id, limit=limit)
    return A2AResponse(
        result={"tasks": [t.model_dump() for t in tasks]},
        id=req.id,
    ).model_dump()


def _handle_cancel_task(req: A2ARequest) -> dict:
    """Handle CancelTask — cancel a running task."""
    task_id = req.params.get("taskId") or req.params.get("id")
    if not task_id:
        return A2AResponse(
            error={"code": -32602, "message": "taskId required"},
            id=req.id,
        ).model_dump()

    task = task_store.get(task_id)
    if not task:
        return A2AResponse(
            error={"code": -32001, "message": f"Task not found: {task_id}"},
            id=req.id,
        ).model_dump()

    if task.status.state in (TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELED):
        return A2AResponse(
            error={"code": -32002, "message": f"Task already in terminal state: {task.status.state}"},
            id=req.id,
        ).model_dump()

    task_store.update_status(task_id, TaskState.CANCELED, "Canceled by client")
    return A2AResponse(result={"task": task.model_dump()}, id=req.id).model_dump()


# ── SSE Streaming ─────────────────────────────────────────────────

async def stream_task_events(task_id: str, handlers: dict, params: dict):
    """Generator for SSE streaming of task events.

    Yields SSE-formatted strings: "data: {json}\n\n"
    """
    skill = params.get("skill", "optimize")
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
        # Execute in background
        handler = handlers.get(skill)
        if not handler:
            task_store.update_status(task.id, TaskState.FAILED, f"Unknown skill: {skill}")
            yield f"data: {json.dumps({'type': 'status', 'task': task.model_dump()})}\n\n"
            return

        handler_params = params.get("params", {})
        for part in parts:
            if part.get("type") == "data":
                handler_params.update(part.get("data", {}))

        # Run handler
        try:
            result = await handler(handler_params)
            artifact = Artifact(
                title=f"{skill} result",
                parts=[{"type": "data", "data": result, "mimeType": "application/json"}],
            )
            task_store.add_artifact(task.id, artifact)
            agent_msg = Message(
                role="agent",
                parts=[{"type": "text", "text": f"Completed {skill}."}],
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

    async def send_message(self, skill: str, params: dict | None = None, text: str = "") -> dict:
        """Send a message (task) to a remote agent."""
        parts = []
        if text:
            parts.append({"type": "text", "text": text})
        if params:
            parts.append({"type": "data", "data": params, "mimeType": "application/json"})

        request = A2ARequest(
            method="SendMessage",
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
        """Get task status and results."""
        request = A2ARequest(method="GetTask", params={"taskId": task_id})
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.base_url}/a2a",
                json=request.model_dump(),
            )
            resp.raise_for_status()
            return resp.json()

    async def list_tasks(self, context_id: str | None = None) -> dict:
        """List tasks, optionally filtered by context."""
        params = {}
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
        """Cancel a running task."""
        request = A2ARequest(method="CancelTask", params={"taskId": task_id})
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.base_url}/a2a",
                json=request.model_dump(),
            )
            resp.raise_for_status()
            return resp.json()

    async def discover(self) -> dict:
        """Fetch agent card."""
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
