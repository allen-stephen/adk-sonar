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


_STATUS_TO_EVENT_KIND = {
    "completed": "completed",
    "awaiting_input": "question",
    "awaiting_approval": "approval_needed",
    "failed": "error",
}


async def mark_handle_event_seen(
    handle: TaskHandle,
    *,
    principal_id: str = "default-user",
) -> None:
    """Advance the consumer cursor watermark when a task outcome has been delivered to a live session."""
    if not is_store_active() or not getattr(handle, "last_event_id", None):
        return
    try:
        from app.store.task_store import get_task_store

        await get_task_store().advance_cursor(principal_id, int(handle.last_event_id))
    except Exception as exc:
        logger.debug("Advancing cursor for %s skipped: %s", handle.task_id, exc)


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
                metadata={
                    "short_id": handle.task_id,
                    "repo": handle.repo,
                    "harness": handle.harness,
                    "status": "running",
                },
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
            event_kind = _STATUS_TO_EVENT_KIND.get(handle.status, handle.status)
            ev = await store.record_event(
                task_id=rec.id,
                kind=event_kind,
                message=handle.summary
                or handle.error
                or f"Task ended with status {handle.status}",
                metadata={
                    "short_id": handle.task_id,
                    "repo": handle.repo,
                    "harness": handle.harness,
                    "branch": handle.branch,
                    "status": handle.status,
                    "questions": list(handle.questions or []),
                    "diff_summary": handle.diff_summary,
                },
            )
            handle.last_event_id = ev.id

            # If no in-session non_blocking_tool generator is awaiting this task
            # (e.g. the user reconnected in a new session while the task was still
            # running, or dispatched from the UI), push to the active live queue if attached.
            if (
                not getattr(handle, "awaited_by_live_tool", False)
                and handle.status in _STATUS_TO_EVENT_KIND
            ):
                from app.tasks import get_task_registry

                live_queue = getattr(get_task_registry(), "active_live_queue", None)
                if live_queue is not None:
                    try:
                        from google.genai import types
                        from app.tools.task_tools import format_task_completion_for_voice

                        notice = format_task_completion_for_voice(handle)
                        live_queue.send_content(
                            types.Content(
                                role="user",
                                parts=[types.Part.from_text(text=f"[BACKGROUND TASK UPDATE] {notice}")],
                            ),
                            partial=False,
                        )
                        await store.advance_cursor("default-user", ev.id)
                    except Exception as inject_exc:
                        logger.debug("Live queue fallback injection skipped: %s", inject_exc)
    except Exception as exc:
        logger.debug("TaskStore persistence skipped: %s", exc)


# Backward-compatibility aliases
_is_store_active = is_store_active
_persist_handle = persist_handle
