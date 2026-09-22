"""Persistence bridge connecting TaskHandle state to durable TaskStore."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.tasks.handle import TaskHandle

logger = logging.getLogger(__name__)


def is_store_active() -> bool:
    """Returns True when persistent TaskStore read/write-through should run."""
    if os.getenv("ORCHESTRATOR_PERSIST_TASKS", "1").lower() in {"0", "false", "no"}:
        return bool(os.getenv("TASK_DB_URL") or os.getenv("DATABASE_URL"))
    return True


async def persist_handle(
    handle: TaskHandle,
    *,
    ended: bool = False,
    new_run: bool = False,
    instruction: str | None = None,
) -> None:
    """Synchronize task handle state with the persistent database ledger."""
    if not is_store_active():
        return
    try:
        from app.store.task_store import get_task_store

        store = get_task_store()
        rec = await store.sync_from_handle(handle, ended=ended)
        if not rec:
            return
        if new_run:
            await store.create_run(
                task_id=rec.id,
                harness=handle.harness,
                mode=handle.mode,
                instruction=instruction,
                status="running",
            )
            await store.record_event(
                task_id=rec.id,
                kind="progress",
                message=f"Started {handle.task_id} on {handle.repo} in {handle.mode} mode ({handle.harness})",
            )
        elif ended:
            if rec.current_run_id:
                await store.update_run(
                    rec.current_run_id,
                    status="succeeded"
                    if handle.status == "completed"
                    else handle.status,
                    exit_code=handle.exit_code,
                    summary=handle.summary,
                    error=handle.error,
                    ended=True,
                )
            await store.record_event(
                task_id=rec.id,
                kind="completed"
                if handle.status == "completed"
                else handle.status,
                message=handle.summary
                or f"Task ended with status {handle.status}",
            )
    except Exception as exc:
        logger.debug("TaskStore persistence skipped: %s", exc)


# Backward-compatibility aliases
_is_store_active = is_store_active
_persist_handle = persist_handle
