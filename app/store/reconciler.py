"""Stateless background task reconciler for detached sandbox coding harness runs."""

from __future__ import annotations

import logging
import os
import socket
from typing import Any

from app.store.task_store import TaskStore, get_task_store
from app.workers.harnesses.base import SandboxContext, poll_detached_command

logger = logging.getLogger(__name__)

_REPLICA_ID = f"{socket.gethostname()}-{os.getpid()}"


async def reconcile_once(
    *,
    replica_id: str | None = None,
    limit: int = 20,
    store: TaskStore | None = None,
) -> int:
    """Claims pending runs via SKIP LOCKED lease, inspects status.json, tails stdout.jsonl, and updates the TaskStore."""
    store = store or get_task_store()
    owner = replica_id or _REPLICA_ID

    runs = await store.claim_runs_for_reconciliation(owner, limit=limit)
    if not runs:
        return 0

    reconciled_count = 0
    for run in runs:
        try:
            handle_data = run.run_handle or {}
            if not handle_data:
                continue

            sb_name = run.sandbox_resource_name
            context = SandboxContext(
                user_id="default_user",
                sandbox_name=sb_name or "default-sandbox",
                lb_host=os.getenv("VERTEX_SANDBOX_LB_HOST", "localhost"),
                routing_token=os.getenv("VERTEX_SANDBOX_ROUTING_TOKEN", ""),
                sandbox_token=os.getenv("VERTEX_SANDBOX_TOKEN", ""),
            )

            offset = int(handle_data.get("stdout_offset", 0))
            status_data, new_stdout, new_offset = await poll_detached_command(
                context, handle_data, offset=offset
            )

            # Record incremental output if new events streamed
            if new_stdout:
                handle_data["stdout_offset"] = new_offset
                for line in new_stdout.splitlines():
                    if line.strip():
                        await store.record_event(
                            task_id=run.task_id,
                            run_id=run.id,
                            kind="progress",
                            message=line.strip()[:300],
                        )

            # Check if run reached a terminal state
            state = status_data.get("state", "running")
            if state in ("completed", "failed", "cancelled"):
                exit_code = status_data.get("exit_code", 0 if state == "completed" else 1)
                await store.update_run(
                    run.id,
                    status="succeeded" if state == "completed" else state,
                    exit_code=exit_code,
                    ended=True,
                )
                await store.update_task_status(
                    run.task_id,
                    status="completed" if state == "completed" else state,
                    ended=True,
                )

            reconciled_count += 1
        except Exception as exc:
            logger.debug("Reconciliation error for run %s: %s", run.id, exc)

    return reconciled_count
