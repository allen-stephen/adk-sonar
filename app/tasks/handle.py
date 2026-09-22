"""Task handle domain model, status resolution, and result normalization."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TaskHandle:
    task_id: str
    goal: str
    repo: str
    started_at: float
    async_task: asyncio.Task | None = None
    harness: str = "claude"
    mode: str = "execute"
    branch: str | None = None
    worktree_path: str | None = None
    consumed: bool = False
    exit_code: int | None = None
    summary: str | None = None
    error: str | None = None
    response_text: str | None = None
    claude_session_id: str | None = None
    files_changed: list[str] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    awaiting_input: bool = False
    awaiting_approval: bool = False
    diff_summary: str | None = None
    raw_diff: str | None = None
    pending_action: str | None = None
    approved_at: float | None = None
    latest_update: str | None = None
    events: list[str] = field(default_factory=list)
    _status: str | None = None

    @property
    def status(self) -> str:
        if self._status is not None:
            return self._status
        if self.async_task is not None:
            if self.async_task.cancelled():
                return "cancelled"
            if not self.async_task.done():
                return "running"
        if self.error or (self.exit_code is not None and self.exit_code != 0):
            return "failed"
        if self.awaiting_input or self.questions:
            return "awaiting_input"
        if self.awaiting_approval:
            return "awaiting_approval"
        return "completed"

    @status.setter
    def status(self, val: str) -> None:
        self._status = val

    @property
    def elapsed_seconds(self) -> int:
        if self.started_at:
            return int(time.monotonic() - self.started_at)
        return 0

    def record_event(self, event: Any) -> None:
        """Record a non-blocking progress event emitted by the running harness."""
        msg = getattr(event, "message", None) or str(event)
        self.latest_update = msg
        self.events.append(msg)


def apply_result_to_handle(handle: TaskHandle, result: Any) -> None:
    """Normalize and map polymorphic harness/worker execution results into the handle."""
    if hasattr(result, "exit_code"):
        handle.exit_code = result.exit_code
        handle.summary = result.summary or "Task completed."
        handle.response_text = (
            getattr(result, "response_text", None) or handle.summary
        )
        handle.claude_session_id = (
            getattr(result, "claude_session_id", None)
            or handle.claude_session_id
        )
        handle.files_changed = list(getattr(result, "files_changed", []) or [])
        handle.questions = list(getattr(result, "questions", []) or [])
        handle.awaiting_input = bool(
            getattr(result, "awaiting_input", False) or handle.questions
        )
        handle.awaiting_approval = bool(
            getattr(result, "awaiting_approval", False)
        )
        handle.diff_summary = getattr(result, "diff_summary", None)
        handle.raw_diff = getattr(result, "raw_diff", None)
        handle.pending_action = getattr(result, "pending_action", None)
        if getattr(result, "branch", None):
            handle.branch = result.branch
        if getattr(result, "worktree_path", None):
            handle.worktree_path = result.worktree_path
        if getattr(result, "harness", None):
            handle.harness = result.harness
        if getattr(result, "events", None) and not handle.events:
            handle.events.extend(result.events)
            handle.latest_update = handle.events[-1]
    elif isinstance(result, dict):
        handle.exit_code = result.get("exit_code", 0)
        handle.summary = result.get("summary", "Task completed.")
        handle.response_text = result.get("response_text", handle.summary)
        handle.claude_session_id = result.get("claude_session_id")
        handle.files_changed = list(result.get("files_changed", []))
        handle.questions = list(result.get("questions", []))
        handle.awaiting_input = bool(
            result.get("awaiting_input", False) or handle.questions
        )
        handle.awaiting_approval = bool(result.get("awaiting_approval", False))
        handle.diff_summary = result.get("diff_summary")
        handle.raw_diff = result.get("raw_diff")
        handle.pending_action = result.get("pending_action")
    else:
        handle.exit_code = 0
        handle.summary = str(result)
        handle.response_text = handle.summary

    if handle.error or (handle.exit_code is not None and handle.exit_code != 0):
        handle.status = "failed"
    elif handle.awaiting_input or handle.questions:
        handle.status = "awaiting_input"
    elif handle.awaiting_approval:
        handle.status = "awaiting_approval"
    else:
        handle.status = "completed"


# Backward-compatibility alias
_apply_result_to_handle = apply_result_to_handle
