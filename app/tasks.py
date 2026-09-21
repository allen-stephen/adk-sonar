"""Task management, per-task worktree tracking, and HITL approval gates for sandboxed coding harnesses."""

from __future__ import annotations

import asyncio
import inspect
import logging
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class TaskHandle:
    task_id: str
    goal: str
    repo: str
    async_task: asyncio.Task
    started_at: float
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

    @property
    def status(self) -> str:
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

    @property
    def elapsed_seconds(self) -> int:
        return int(time.monotonic() - self.started_at)

    def record_event(self, event: Any) -> None:
        """Record a non-blocking progress event emitted by the running harness."""
        msg = getattr(event, "message", None) or str(event)
        self.latest_update = msg
        self.events.append(msg)


def _apply_result_to_handle(handle: TaskHandle, result: Any) -> None:
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




class TaskRegistry:
    """Singleton registry holding tracked remote coding tasks across harnesses."""

    MAX_CONCURRENT_TASKS = 5

    def __init__(self) -> None:
        self._tasks: dict[str, TaskHandle] = {}
        self._lock = asyncio.Lock()
        self._next_id = 1

    async def register(
        self,
        *,
        goal: str,
        repo: str,
        task_coro_fn: Any,
        harness: str = "claude",
        mode: str = "execute",
    ) -> tuple[str, TaskHandle]:
        async with self._lock:
            running_count = sum(
                1 for t in self._tasks.values() if t.status == "running"
            )
            if running_count >= self.MAX_CONCURRENT_TASKS:
                raise ValueError(
                    f"Concurrency limit reached ({self.MAX_CONCURRENT_TASKS} active tasks). "
                    "Wait for a task to finish or cancel an existing one."
                )

            task_id = f"task-{self._next_id}"
            self._next_id += 1

            async def _run_wrapper(handle_ref: list[TaskHandle]) -> None:
                try:
                    await asyncio.sleep(0)
                    handle = handle_ref[0]

                    sig = inspect.signature(task_coro_fn)
                    if len(sig.parameters) >= 2:
                        result = await task_coro_fn(task_id, handle.record_event)
                    elif len(sig.parameters) == 1:
                        result = await task_coro_fn(task_id)
                    else:
                        result = await task_coro_fn()

                    _apply_result_to_handle(handle, result)
                except asyncio.CancelledError:
                    if handle_ref:
                        handle_ref[0].summary = "Task was cancelled."
                    raise
                except Exception as exc:
                    logger.exception("Task %s failed", task_id)
                    if handle_ref:
                        handle_ref[0].error = str(exc)
                        handle_ref[0].exit_code = 1

            handle_holder: list[Any] = []
            async_task = asyncio.create_task(
                _run_wrapper(handle_holder),
                name=f"remote-{task_id}",
            )
            handle = TaskHandle(
                task_id=task_id,
                goal=goal,
                repo=repo,
                harness=harness,
                mode=mode,
                branch=f"agent/{task_id}",
                async_task=async_task,
                started_at=time.monotonic(),
            )
            handle_holder.append(handle)
            self._tasks[task_id] = handle
            return task_id, handle

    async def resume(
        self,
        task_id: str,
        *,
        instruction: str,
        mode: str,
        task_coro_fn: Any,
        harness: str | None = None,
    ) -> TaskHandle:
        async with self._lock:
            handle = self._tasks.get(task_id)
            if not handle:
                raise ValueError(f"Task {task_id} was not found.")

            if harness:
                handle.harness = harness
            handle.goal = f"{handle.goal} | Follow-up: {instruction}"
            handle.mode = mode
            handle.questions = []
            handle.awaiting_input = False
            handle.awaiting_approval = False
            handle.consumed = False
            handle.error = None
            handle.exit_code = 0

            async def _resume_wrapper() -> None:
                try:
                    sig = inspect.signature(task_coro_fn)
                    if len(sig.parameters) >= 2:
                        result = await task_coro_fn(task_id, handle.record_event)
                    elif len(sig.parameters) == 1:
                        result = await task_coro_fn(task_id)
                    else:
                        result = await task_coro_fn()
                    _apply_result_to_handle(handle, result)
                except asyncio.CancelledError:
                    handle.summary = "Task was cancelled."
                    raise
                except Exception as exc:
                    logger.exception("Task %s failed on resume", task_id)
                    handle.error = str(exc)
                    handle.exit_code = 1

            handle.async_task = asyncio.create_task(
                _resume_wrapper(),
                name=f"remote-resume-{task_id}",
            )
            return handle

    async def approve(self, task_id: str) -> TaskHandle:
        """Approve a task waiting at a CUJ 3 HITL approval gate, committing its worktree branch."""
        async with self._lock:
            handle = self._tasks.get(task_id)
            if not handle:
                raise ValueError(f"Task {task_id} was not found.")

            if handle.worktree_path and Path(handle.worktree_path).exists():
                subprocess.run(
                    ["git", "-C", handle.worktree_path, "add", "."],
                    check=False,
                    capture_output=True,
                )
                subprocess.run(
                    [
                        "git",
                        "-C",
                        handle.worktree_path,
                        "-c",
                        "user.name=VoiceOrchestrator",
                        "-c",
                        "user.email=orchestrator@example.com",
                        "commit",
                        "-m",
                        f"Approved via voice: {handle.goal[:72]}",
                    ],
                    check=False,
                    capture_output=True,
                )

            handle.awaiting_approval = False
            handle.approved_at = time.time()
            handle.consumed = False
            branch_label = handle.branch or f"agent/{handle.task_id}"
            handle.summary = (
                f"Approved and committed changes on branch {branch_label} for {handle.repo}."
            )
            return handle

    async def get(self, task_id: str) -> TaskHandle | None:
        async with self._lock:
            return self._tasks.get(task_id)

    async def list_all(self) -> list[TaskHandle]:
        async with self._lock:
            return list(self._tasks.values())

    async def list_running(self) -> list[TaskHandle]:
        async with self._lock:
            return [t for t in self._tasks.values() if t.status == "running"]

    async def cancel(self, task_id: str) -> bool:
        async with self._lock:
            handle = self._tasks.get(task_id)
        if not handle:
            return False
        if handle.status == "running":
            handle.async_task.cancel()
            return True
        return False

    async def wait_next_unconsumed(self, timeout_s: float = 30.0) -> TaskHandle | None:
        """Wait until any unconsumed finished, awaiting_input, or awaiting_approval task is ready."""
        terminal_or_checkpoint = {
            "completed",
            "failed",
            "cancelled",
            "awaiting_input",
            "awaiting_approval",
        }
        async with self._lock:
            for h in self._tasks.values():
                if h.status in terminal_or_checkpoint and not h.consumed:
                    h.consumed = True
                    return h

            active = [
                h
                for h in self._tasks.values()
                if not h.consumed and not h.async_task.done()
            ]

        if not active:
            return None

        done, _ = await asyncio.wait(
            [h.async_task for h in active],
            timeout=timeout_s,
            return_when=asyncio.FIRST_COMPLETED,
        )

        if not done:
            return None

        async with self._lock:
            for h in active:
                if h.async_task in done and not h.consumed:
                    h.consumed = True
                    return h
        return None


_registry: TaskRegistry | None = None


def get_task_registry() -> TaskRegistry:
    global _registry
    if _registry is None:
        _registry = TaskRegistry()
    return _registry


def reset_task_registry() -> None:
    global _registry
    _registry = None
