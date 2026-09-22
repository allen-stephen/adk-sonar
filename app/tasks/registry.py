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

logger = logging.getLogger(__name__)


class TaskRegistry:
    """Singleton registry holding tracked remote coding tasks across harnesses."""

    MAX_CONCURRENT_TASKS = 20

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

            while f"task-{self._next_id}" in self._tasks:
                self._next_id += 1
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

        async with self._lock:
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
            handle.status = "completed"
            handle.approved_at = time.time()
            handle.consumed = False
            branch_label = handle.branch or f"agent/{handle.task_id}"
            handle.summary = (
                f"Approved and committed changes on branch {branch_label} for {handle.repo}."
            )
            repo_name = handle.repo

        await persist_handle(handle, ended=True)

        # Outside the lock: the commit above means the branch now holds the work,
        # so the checkout can be reclaimed. Kept off the lock (and off the event
        # loop) because `approve` already blocks on git while holding it, which
        # stalls the live audio stream.
        from app.workers.harnesses.base import release_worktree

        if await asyncio.to_thread(release_worktree, repo_name, task_id):
            handle.worktree_path = None

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
        if handle.status == "running":
            if handle.async_task is not None:
                handle.async_task.cancel()
            handle.status = "cancelled"
            await persist_handle(handle, ended=True)
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
