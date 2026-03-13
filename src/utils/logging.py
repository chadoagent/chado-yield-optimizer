from __future__ import annotations

import time
import uuid
from typing import Any


class ExecutionLogger:
    """Structured execution log capture for ERC-8004 'Agents With Receipts' trust."""

    def __init__(self):
        self.logs: list[dict] = []

    def start(self, action: str, params: dict | None = None) -> dict:
        entry = {
            "id": uuid.uuid4().hex[:12],
            "action": action,
            "params": params or {},
            "started_at": time.time(),
            "status": "running",
        }
        self.logs.append(entry)
        return entry

    def end(self, entry: dict, result: Any = None, error: str | None = None):
        entry["ended_at"] = time.time()
        entry["duration_ms"] = int((entry["ended_at"] - entry["started_at"]) * 1000)
        if error:
            entry["status"] = "error"
            entry["error"] = error
        else:
            entry["status"] = "ok"
            entry["result_summary"] = _summarize(result)

    def get_logs(self, limit: int = 50) -> list[dict]:
        return self.logs[-limit:]

    def export(self) -> list[dict]:
        return [
            {
                "id": e["id"],
                "action": e["action"],
                "status": e["status"],
                "duration_ms": e.get("duration_ms"),
                "started_at": e["started_at"],
            }
            for e in self.logs
        ]


def _summarize(obj: Any) -> str:
    if obj is None:
        return ""
    if isinstance(obj, dict):
        keys = list(obj.keys())[:5]
        return f"dict({len(obj)} keys: {keys})"
    if isinstance(obj, list):
        return f"list({len(obj)} items)"
    return str(obj)[:200]
