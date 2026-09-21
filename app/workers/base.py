"""Protocol defining worker backends for executing configurable coding harness jobs."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class WorkerExecutionResult:
    exit_code: int
    summary: str
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
    branch: str | None = None
    worktree_path: str | None = None
    harness: str = "claude"
    events: list[str] = field(default_factory=list)


@runtime_checkable
class WorkerBackend(Protocol):
    """Execution interface for long-running coding tasks across harnesses."""

    async def execute_task(
        self,
        *,
        goal: str,
        repo: str,
        task_id: str,
        mode: str = "execute",
        require_approval: bool = False,
        harness: str | None = None,
        session_id: str | None = None,
        on_event: Callable[[Any], None] | None = None,
    ) -> WorkerExecutionResult:
        """Run a task to completion (or plan/approval checkpoint) using the specified coding harness."""
        ...

    async def cancel_task(self, task_id: str) -> bool:
        """Cancel an in-flight task by ID."""
        ...
