"""Repository class implementing transactional CRUD and reconciliation operations for the Work Ledger."""

from __future__ import annotations

import datetime
import logging
import uuid
from typing import Any

from sqlalchemy import desc, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.store.engine import get_session_factory
from app.store.models import (
    CursorRecord,
    RunRecord,
    TaskArtifactRecord,
    TaskEventRecord,
    TaskRecord,
)

logger = logging.getLogger(__name__)


class TaskStore:
    """Async repository managing persistent Tasks, Runs, Events, and Cursors."""

    def __init__(self, session_factory=None) -> None:
        self._session_factory = session_factory or get_session_factory()
        self._initialized = False

    async def ensure_initialized(self) -> None:
        """Lazily bootstraps the database tables if not already present."""
        if not self._initialized:
            from app.store.engine import init_db

            try:
                await init_db()
            except Exception as exc:
                logger.debug("Database auto-init skipped or already ready: %s", exc)
            self._initialized = True

    async def create_task(
        self,
        *,
        goal: str,
        repo: str,
        short_id: str | None = None,
        principal_id: str = "default-user",
        harness: str = "claude",
        mode: str = "execute",
        branch: str | None = None,
        worktree_path: str | None = None,
        status: str = "queued",
        require_approval: bool = False,
    ) -> TaskRecord:
        """Creates and stores a new TaskRecord."""
        await self.ensure_initialized()
        task_uuid = str(uuid.uuid4())
        async with self._session_factory() as session:
            if not short_id:
                count_res = await session.execute(select(func.count(TaskRecord.id)))
                count = count_res.scalar() or 0
                short_id = f"task-{count + 1}"

            record = TaskRecord(
                id=task_uuid,
                short_id=short_id,
                principal_id=principal_id,
                goal=goal,
                repo=repo,
                harness=harness,
                mode=mode,
                branch=branch or f"agent/{short_id}",
                worktree_path=worktree_path,
                status=status,
                require_approval=require_approval,
            )
            session.add(record)
            await session.commit()
            await session.refresh(record)
            return record

    async def sync_from_handle(
        self,
        handle: Any,
        *,
        ended: bool = False,
    ) -> TaskRecord | None:
        """Persists or updates the full TaskHandle state into TaskRecord."""
        await self.ensure_initialized()
        record = await self.get_task(handle.task_id)
        now = datetime.datetime.now(datetime.timezone.utc)
        values = {
            "goal": handle.goal,
            "repo": handle.repo,
            "harness": handle.harness,
            "mode": handle.mode,
            "branch": handle.branch,
            "worktree_path": handle.worktree_path,
            "status": handle.status,
            "exit_code": handle.exit_code,
            "summary": handle.summary,
            "response_text": handle.response_text,
            "error": handle.error,
            "diff_summary": handle.diff_summary,
            "raw_diff": handle.raw_diff,
            "pending_action": handle.pending_action,
            "harness_session_id": handle.claude_session_id,
            "files_changed": list(handle.files_changed or []),
            "questions": list(handle.questions or []),
            "events_list": list(handle.events or []),
            "updated_at": now,
        }
        if ended:
            values["ended_at"] = now

        if not record:
            task_uuid = str(uuid.uuid4())
            async with self._session_factory() as session:
                new_rec = TaskRecord(
                    id=task_uuid,
                    short_id=handle.task_id,
                    principal_id="default-user",
                    **values,
                )
                session.add(new_rec)
                await session.commit()
                await session.refresh(new_rec)
                return new_rec

        async with self._session_factory() as session:
            await session.execute(
                update(TaskRecord).where(TaskRecord.id == record.id).values(**values)
            )
            await session.commit()
        return await self.get_task(record.id)

    async def get_task(self, identifier: str) -> TaskRecord | None:
        """Fetch task by UUID or short_id (e.g. 'task-1')."""
        await self.ensure_initialized()
        ident = identifier.strip().lower()
        async with self._session_factory() as session:
            stmt = select(TaskRecord).where(
                (TaskRecord.id == ident) | (TaskRecord.short_id == ident)
            )
            res = await session.execute(stmt)
            return res.scalar_one_or_none()

    async def list_tasks(
        self,
        *,
        principal_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[TaskRecord]:
        """Lists tasks with optional status and principal filtering."""
        await self.ensure_initialized()
        async with self._session_factory() as session:
            stmt = select(TaskRecord).order_by(desc(TaskRecord.created_at)).limit(limit)
            if principal_id:
                stmt = stmt.where(TaskRecord.principal_id == principal_id)
            if status:
                stmt = stmt.where(TaskRecord.status == status)
            res = await session.execute(stmt)
            return list(res.scalars().all())

    async def update_task_status(
        self,
        identifier: str,
        *,
        status: str,
        ended: bool = False,
    ) -> TaskRecord | None:
        """Updates task status."""
        record = await self.get_task(identifier)
        if not record:
            return None
        async with self._session_factory() as session:
            stmt = (
                update(TaskRecord)
                .where(TaskRecord.id == record.id)
                .values(
                    status=status,
                    ended_at=datetime.datetime.now(datetime.timezone.utc) if ended else None,
                )
            )
            await session.execute(stmt)
            await session.commit()
            return await self.get_task(record.id)

    async def create_run(
        self,
        *,
        task_id: str,
        harness: str = "claude",
        mode: str = "execute",
        instruction: str | None = None,
        status: str = "queued",
        harness_session_id: str | None = None,
        sandbox_resource_name: str | None = None,
    ) -> RunRecord:
        """Creates a new Run attempt for a task."""
        run_uuid = str(uuid.uuid4())
        async with self._session_factory() as session:
            # Determine next seq for this task
            seq_res = await session.execute(
                select(func.coalesce(func.max(RunRecord.seq), 0)).where(RunRecord.task_id == task_id)
            )
            next_seq = (seq_res.scalar() or 0) + 1

            run = RunRecord(
                id=run_uuid,
                task_id=task_id,
                seq=next_seq,
                harness=harness,
                mode=mode,
                instruction=instruction,
                status=status,
                harness_session_id=harness_session_id,
                sandbox_resource_name=sandbox_resource_name,
                started_at=datetime.datetime.now(datetime.timezone.utc),
            )
            session.add(run)

            # Point task's current_run_id to this new run
            await session.execute(
                update(TaskRecord).where(TaskRecord.id == task_id).values(current_run_id=run_uuid)
            )
            await session.commit()
            await session.refresh(run)
            return run

    async def update_run(
        self,
        run_id: str,
        *,
        status: str | None = None,
        run_handle: dict[str, Any] | None = None,
        exit_code: int | None = None,
        summary: str | None = None,
        error: str | None = None,
        ended: bool = False,
    ) -> None:
        """Updates run execution details."""
        values: dict[str, Any] = {}
        if status is not None:
            values["status"] = status
        if run_handle is not None:
            values["run_handle"] = run_handle
        if exit_code is not None:
            values["exit_code"] = exit_code
        if summary is not None:
            values["summary"] = summary
        if error is not None:
            values["error"] = error
        if ended:
            values["ended_at"] = datetime.datetime.now(datetime.timezone.utc)

        if not values:
            return

        async with self._session_factory() as session:
            await session.execute(
                update(RunRecord).where(RunRecord.id == run_id).values(**values)
            )
            await session.commit()

    async def record_event(
        self,
        *,
        task_id: str,
        kind: str,
        message: str,
        run_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TaskEventRecord:
        """Appends a monotonic progress or state event to the log."""
        async with self._session_factory() as session:
            event = TaskEventRecord(
                task_id=task_id,
                run_id=run_id,
                kind=kind,
                message=message,
                metadata_json=metadata,
            )
            session.add(event)
            await session.commit()
            await session.refresh(event)
            return event

    async def get_unseen_events(
        self,
        principal_id: str,
        limit: int = 20,
    ) -> list[TaskEventRecord]:
        """Fetches events that occurred after the principal's last seen cursor."""
        async with self._session_factory() as session:
            cursor_res = await session.execute(
                select(CursorRecord.last_seen_event_id).where(
                    CursorRecord.principal_id == principal_id
                )
            )
            last_id = cursor_res.scalar() or 0

            events_res = await session.execute(
                select(TaskEventRecord)
                .join(TaskRecord, TaskRecord.id == TaskEventRecord.task_id)
                .where(
                    (TaskRecord.principal_id == principal_id)
                    & (TaskEventRecord.id > last_id)
                    & (TaskEventRecord.kind.in_(["completed", "error", "approval_needed", "question"]))
                )
                .order_by(TaskEventRecord.id)
                .limit(limit)
            )
            return list(events_res.scalars().all())

    async def advance_cursor(self, principal_id: str, event_id: int) -> None:
        """Advances the principal's event watermark."""
        async with self._session_factory() as session:
            cursor = await session.get(CursorRecord, principal_id)
            if cursor:
                if event_id > cursor.last_seen_event_id:
                    cursor.last_seen_event_id = event_id
            else:
                cursor = CursorRecord(principal_id=principal_id, last_seen_event_id=event_id)
                session.add(cursor)
            await session.commit()

    async def claim_runs_for_reconciliation(
        self,
        lease_owner: str,
        *,
        limit: int = 20,
        lease_duration_seconds: int = 30,
    ) -> list[RunRecord]:
        """Atomically claim active runs for reconciliation using SKIP LOCKED where supported."""
        await self.ensure_initialized()
        now = datetime.datetime.now(datetime.timezone.utc)
        expires_at = now + datetime.timedelta(seconds=lease_duration_seconds)

        async with self._session_factory() as session:
            stmt = (
                select(RunRecord)
                .where(
                    RunRecord.status.in_(["queued", "launching", "running"]),
                    (RunRecord.lease_expires_at.is_(None) | (RunRecord.lease_expires_at < now)),
                )
                .order_by(RunRecord.started_at.asc().nulls_first())
                .limit(limit)
            )
            bind = session.bind
            if bind and "postgresql" in getattr(bind.url, "drivername", ""):
                stmt = stmt.with_for_update(skip_locked=True)

            res = await session.execute(stmt)
            runs = list(res.scalars().all())

            for r in runs:
                r.lease_owner = lease_owner
                r.lease_expires_at = expires_at

            if runs:
                await session.commit()
            return runs


_store: TaskStore | None = None


def get_task_store() -> TaskStore:
    global _store
    if _store is None:
        _store = TaskStore()
    return _store


def reset_task_store() -> None:
    global _store
    _store = None
