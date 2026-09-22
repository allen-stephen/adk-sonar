"""Task management, per-task worktree tracking, and HITL approval gates."""

from __future__ import annotations

from app.tasks.handle import (
    TaskHandle,
    _apply_result_to_handle,
    apply_result_to_handle,
)
from app.tasks.persistence import (
    _is_store_active,
    _persist_handle,
    is_store_active,
    persist_handle,
)
from app.tasks.registry import (
    TaskRegistry,
    get_task_registry,
    reset_task_registry,
)

__all__ = [
    "TaskHandle",
    "TaskRegistry",
    "_apply_result_to_handle",
    "_is_store_active",
    "_persist_handle",
    "apply_result_to_handle",
    "get_task_registry",
    "is_store_active",
    "persist_handle",
    "reset_task_registry",
]
