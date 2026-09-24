"""TaskRegistry managing concurrency, remote task execution, HITL approval, and lifecycle."""

from __future__ import annotations

import asyncio
import inspect
import logging
import subprocess
import time
from pathlib import Path
from typing import Any

from app.tasks.handle import TaskHandle, apply_result_to_handle
from app.tasks.persistence import is_store_active, persist_handle
from app.workers.base import ApprovalCommitResult

logger = logging.getLogger(__name__)


class TaskRegistry:
    """Singleton registry holding tracked remote coding tasks across harnesses."""

    MAX_CONCURRENT_TASKS = 20

    def __init__(self) -> None:
        self._tasks: dict[str, TaskHandle] = {}
        self._lock = asyncio.Lock()
        self._next_id = 1
        self.active_live_queue: Any = None

    async def register(
        self,
        *,
        goal: str,
        repo: str,
        task_coro_fn: Any,
        harness: str = "claude",
        mode: str = "execute",
        require_approval: bool = False,
        task_id: str | None = None,
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

            if not task_id:
                import uuid

                while True:
                    candidate = f"task-{uuid.uuid4().hex[:8]}"
                    if candidate not in self._tasks:
                        task_id = candidate
                        break
            elif task_id in self._tasks:
                raise ValueError(f"Task with ID {task_id} already exists.")

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

                    apply_result_to_handle(handle, result)
                    await persist_handle(handle, ended=True)
                except asyncio.CancelledError:
                    if handle_ref:
                        handle_ref[0].summary = "Task was cancelled."
                        handle_ref[0].status = "cancelled"
                        await persist_handle(handle_ref[0], ended=True)
                    raise
                except Exception as exc:
                    logger.exception("Task %s failed", task_id)
                    if handle_ref:
                        handle_ref[0].error = str(exc)
                        handle_ref[0].exit_code = 1
                        handle_ref[0].status = "failed"
                        await persist_handle(handle_ref[0], ended=True)

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
                require_approval=require_approval,
                branch=f"agent/{task_id}",
                async_task=async_task,
                started_at=time.monotonic(),
                _status="running",
            )
            handle_holder.append(handle)
            self._tasks[task_id] = handle

        await persist_handle(handle, new_run=True)
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
        handle = await self.get(task_id)
        if not handle:
            raise ValueError(f"Task {task_id} was not found.")

        async with self._lock:
            if harness:
                handle.harness = harness
            handle.goal = f"{handle.goal} | Follow-up: {instruction}"
            handle.mode = mode
            handle.questions = []
            handle.awaiting_input = False
            handle.awaiting_approval = False
            handle.status = "running"
            handle.from_history = False
            handle.dismissed = False
            handle.ended_at_ts = None
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
                    apply_result_to_handle(handle, result)
                    await persist_handle(handle, ended=True)
                except asyncio.CancelledError:
                    handle.summary = "Task was cancelled."
                    handle.status = "cancelled"
                    await persist_handle(handle, ended=True)
                    raise
                except Exception as exc:
                    logger.exception("Task %s failed on resume", task_id)
                    handle.error = str(exc)
                    handle.exit_code = 1
                    handle.status = "failed"
                    await persist_handle(handle, ended=True)

            handle.async_task = asyncio.create_task(
                _resume_wrapper(),
                name=f"remote-resume-{task_id}",
            )

        await persist_handle(handle, new_run=True, instruction=instruction)
        return handle

    async def approve(self, task_id: str) -> TaskHandle:
        """Approve a task waiting at a CUJ 3 HITL approval gate, committing its worktree branch."""
        handle = await self.get(task_id)
        if not handle:
            raise ValueError(f"Task {task_id} was not found.")

        # Delegate to the worker backend: in the sandbox topology the worktree lives
        # inside the container, so host-side git here would silently no-op while we
        # still told the user their work was committed.
        from app.workers import get_worker_backend

        branch_label = handle.branch or f"agent/{handle.task_id}"
        try:
            commit_result = await get_worker_backend().commit_and_push_task(
                task_id=handle.task_id,
                branch=branch_label,
                message=f"Approved via voice: {handle.goal[:72]}",
                worktree_path=handle.worktree_path,
            )
        except Exception as exc:  # noqa: BLE001 - surfaced to the user, not swallowed
            logger.warning("approve: commit failed for %s: %s", handle.task_id, exc)
            commit_result = ApprovalCommitResult(
                committed=False, pushed=False, detail=f"Commit failed: {exc}"
            )

        async with self._lock:
            handle.awaiting_approval = False
            handle.status = "completed"
            handle.approved_at = time.time()
            # Describe only what actually happened. Anything stated here is spoken
            # to the user and re-injected into the next turn's system prompt.
            if commit_result.committed and commit_result.pushed:
                handle.summary = (
                    f"Approved, committed, and pushed branch {branch_label} for {handle.repo}."
                )
            elif commit_result.committed:
                handle.summary = (
                    f"Approved and committed branch {branch_label} for {handle.repo}, "
                    f"but it was not pushed. {commit_result.detail}".strip()
                )
            else:
                handle.summary = (
                    f"Approved {handle.task_id}, but nothing was committed on branch "
                    f"{branch_label}. {commit_result.detail}".strip()
                )
            handle.approval_detail = commit_result.detail
            handle.approval_committed = commit_result.committed
            handle.approval_pushed = commit_result.pushed
            repo_name = handle.repo

        await persist_handle(handle, ended=True)

        # Outside the lock: once the branch holds the work, the checkout can be
        # reclaimed. Kept off the lock (and off the event loop) because `approve`
        # already blocks on git while holding it, which stalls the live audio stream.
        # Only reclaim on a successful commit: releasing a worktree whose changes
        # were never committed would destroy the user's work.
        if commit_result.committed:
            from app.workers.harnesses.base import release_worktree

            if await asyncio.to_thread(release_worktree, repo_name, task_id):
                handle.worktree_path = None
        else:
            logger.warning(
                "approve: keeping worktree for %s because nothing was committed (%s)",
                task_id,
                commit_result.detail,
            )

        return handle

    async def get(self, task_id: str) -> TaskHandle | None:
        async with self._lock:
            existing = self._tasks.get(task_id)
            if existing is not None:
                return existing

        if is_store_active():
            try:
                from app.store.task_store import get_task_store

                rec = await get_task_store().get_task(task_id)
                if rec is not None:
                    hydrated = rec.to_task_handle()
                    async with self._lock:
                        self._tasks[rec.short_id] = hydrated
                    return hydrated
            except Exception as exc:
                logger.debug("TaskStore hydration on get skipped: %s", exc)
        return None

    async def list_all(self) -> list[TaskHandle]:
        if is_store_active():
            try:
                from app.store.task_store import get_task_store

                records = await get_task_store().list_tasks(limit=50)
                async with self._lock:
                    for rec in reversed(records):
                        if rec.short_id not in self._tasks:
                            self._tasks[rec.short_id] = rec.to_task_handle()
                        if rec.short_id.startswith("task-"):
                            suffix = rec.short_id.removeprefix("task-")
                            if suffix.isdigit():
                                self._next_id = max(self._next_id, int(suffix) + 1)
            except Exception as exc:
                logger.debug("TaskStore hydration on list_all skipped: %s", exc)

        async with self._lock:
            return list(self._tasks.values())

    def snapshot_tasks(self) -> list[TaskHandle]:
        """Return a synchronous snapshot of in-memory tasks (hydrated via list_all in before_agent_callback)."""
        return list(self._tasks.values())

    async def list_running(self) -> list[TaskHandle]:
        all_items = await self.list_all()
        return [t for t in all_items if t.status == "running"]

    async def cancel(self, task_id: str) -> bool:
        handle = await self.get(task_id)
        if not handle:
            return False
        if handle.status in {"running", "awaiting_input", "awaiting_approval"}:
            if handle.async_task is not None and not handle.async_task.done():
                handle.async_task.cancel()
            handle.awaiting_input = False
            handle.awaiting_approval = False
            handle.questions = []
            handle.status = "cancelled"
            handle.dismissed = True
            handle.ended_at_ts = time.time()
            await persist_handle(handle, ended=True)
            return True
        return False

    async def clear_all(self) -> None:
        """Cancel all in-flight tasks and clear in-memory task registry state."""
        async with self._lock:
            for handle in self._tasks.values():
                if handle.async_task is not None and not handle.async_task.done():
                    handle.async_task.cancel()
            self._tasks.clear()
            self._next_id = 1


_registry: TaskRegistry | None = None


def get_task_registry() -> TaskRegistry:
    global _registry
    if _registry is None:
        _registry = TaskRegistry()
    return _registry


def reset_task_registry() -> None:
    global _registry
    _registry = None
