"""SQLAlchemy 2 declarative models for the ADK Sonar durable Work Ledger."""

from __future__ import annotations

import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

# Use JSONB for PostgreSQL when available, falling back to standard JSON for SQLite
JSON_TYPE = JSON().with_variant(JSONB, "postgresql")


class Base(DeclarativeBase):
    pass


class TaskRecord(Base):
    """Represents a durable high-level coding or engineering task."""

    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    short_id: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    principal_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False, default="default-user")
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    repo: Mapped[str] = mapped_column(String(128), nullable=False)
    harness: Mapped[str] = mapped_column(String(32), nullable=False, default="claude")
    mode: Mapped[str] = mapped_column(String(32), nullable=False, default="execute")
    branch: Mapped[str | None] = mapped_column(String(128), nullable=True)
    worktree_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    status: Mapped[str] = mapped_column(String(32), index=True, nullable=False, default="queued")
    require_approval: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    current_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    diff_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_diff: Mapped[str | None] = mapped_column(Text, nullable=True)
    pending_action: Mapped[str | None] = mapped_column(Text, nullable=True)
    harness_session_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    files_changed: Mapped[list[str] | None] = mapped_column(JSON_TYPE, nullable=True)
    questions: Mapped[list[str] | None] = mapped_column(JSON_TYPE, nullable=True)
    events_list: Mapped[list[str] | None] = mapped_column(JSON_TYPE, nullable=True)

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    ended_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Relationships
    runs: Mapped[list[RunRecord]] = relationship(
        "RunRecord", back_populates="task", cascade="all, delete-orphan", order_by="RunRecord.seq"
    )
    events: Mapped[list[TaskEventRecord]] = relationship(
        "TaskEventRecord", back_populates="task", cascade="all, delete-orphan", order_by="TaskEventRecord.id"
    )
    artifacts: Mapped[list[TaskArtifactRecord]] = relationship(
        "TaskArtifactRecord", back_populates="task", cascade="all, delete-orphan"
    )

    def to_task_handle(self) -> Any:
        """Hydrates an in-memory TaskHandle from the persistent database record."""
        import time

        from app.tasks import TaskHandle

        events = list(self.events_list or [])
        created_ts = (
            self.created_at.timestamp() if self.created_at is not None else time.time()
        )
        ended_dt = self.ended_at or (
            self.updated_at
            if self.status in {"completed", "failed", "cancelled", "orphaned"}
            else None
        )
        ended_ts = ended_dt.timestamp() if ended_dt is not None else None
        return TaskHandle(
            task_id=self.short_id,
            goal=self.goal,
            repo=self.repo,
            started_at=time.monotonic(),
            async_task=None,
            harness=self.harness or "claude",
            mode=self.mode or "execute",
            branch=self.branch,
            worktree_path=self.worktree_path,
            exit_code=self.exit_code,
            summary=self.summary,
            error=self.error,
            response_text=self.response_text,
            claude_session_id=self.harness_session_id,
            files_changed=list(self.files_changed or []),
            questions=list(self.questions or []),
            awaiting_input=(self.status == "awaiting_input"),
            awaiting_approval=(self.status == "awaiting_approval"),
            diff_summary=self.diff_summary,
            raw_diff=self.raw_diff,
            pending_action=self.pending_action,
            created_at_ts=created_ts,
            ended_at_ts=ended_ts,
            latest_update=events[-1] if events else self.summary,
            events=events,
            _status=self.status,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "short_id": self.short_id,
            "principal_id": self.principal_id,
            "goal": self.goal,
            "repo": self.repo,
            "harness": self.harness,
            "mode": self.mode,
            "branch": self.branch,
            "worktree_path": self.worktree_path,
            "status": self.status,
            "require_approval": self.require_approval,
            "current_run_id": self.current_run_id,
            "summary": self.summary,
            "response_text": self.response_text,
            "error": self.error,
            "exit_code": self.exit_code,
            "diff_summary": self.diff_summary,
            "pending_action": self.pending_action,
            "files_changed": list(self.files_changed or []),
            "questions": list(self.questions or []),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
        }


class RunRecord(Base):
    """Represents an individual execution attempt of a task by a coding harness."""

    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), ForeignKey("tasks.id", ondelete="CASCADE"), index=True, nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    harness: Mapped[str] = mapped_column(String(32), nullable=False, default="claude")
    mode: Mapped[str] = mapped_column(String(32), nullable=False, default="execute")
    instruction: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), index=True, nullable=False, default="queued")

    run_handle: Mapped[dict[str, Any] | None] = mapped_column(JSON_TYPE, nullable=True)
    harness_session_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    sandbox_resource_name: Mapped[str | None] = mapped_column(String(256), nullable=True)

    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    lease_owner: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)
    lease_expires_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), index=True, nullable=True)

    started_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Relationships
    task: Mapped[TaskRecord] = relationship("TaskRecord", back_populates="runs")
    events: Mapped[list[TaskEventRecord]] = relationship("TaskEventRecord", back_populates="run")
    artifacts: Mapped[list[TaskArtifactRecord]] = relationship("TaskArtifactRecord", back_populates="run")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "seq": self.seq,
            "harness": self.harness,
            "mode": self.mode,
            "instruction": self.instruction,
            "status": self.status,
            "run_handle": self.run_handle,
            "harness_session_id": self.harness_session_id,
            "sandbox_resource_name": self.sandbox_resource_name,
            "exit_code": self.exit_code,
            "summary": self.summary,
            "error": self.error,
            "lease_owner": self.lease_owner,
            "lease_expires_at": self.lease_expires_at.isoformat() if self.lease_expires_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
        }


class TaskEventRecord(Base):
    """Monotonic event log for task execution, UI updates, and turn-zero voice briefings."""

    __tablename__ = "task_events"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(64), ForeignKey("tasks.id", ondelete="CASCADE"), index=True, nullable=False)
    run_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("runs.id", ondelete="SET NULL"), nullable=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)  # progress, question, approval_needed, completed, error
    message: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSON_TYPE, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    task: Mapped[TaskRecord] = relationship("TaskRecord", back_populates="events")
    run: Mapped[RunRecord | None] = relationship("RunRecord", back_populates="events")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "kind": self.kind,
            "message": self.message,
            "metadata": self.metadata_json or {},
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class TaskArtifactRecord(Base):
    """Pointers to larger task artifacts stored in Cloud Storage (GCS) or local workspace."""

    __tablename__ = "task_artifacts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), ForeignKey("tasks.id", ondelete="CASCADE"), index=True, nullable=False)
    run_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("runs.id", ondelete="SET NULL"), nullable=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)  # plan, raw_diff, stdout, progress
    gcs_uri: Mapped[str] = mapped_column(String(512), nullable=False)
    bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    task: Mapped[TaskRecord] = relationship("TaskRecord", back_populates="artifacts")
    run: Mapped[RunRecord | None] = relationship("RunRecord", back_populates="artifacts")


class CursorRecord(Base):
    """Per-principal watermark over monotonic task_events for briefing synthesis."""

    __tablename__ = "consumer_cursors"

    principal_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    last_seen_event_id: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
