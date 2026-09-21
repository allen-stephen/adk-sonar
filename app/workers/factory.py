"""Factory for instantiating the active worker backend."""

from __future__ import annotations

import os

from app.workers.base import WorkerBackend
from app.workers.local import LocalWorker
from app.workers.sandbox import SandboxWorker

_active_worker: WorkerBackend | None = None


def get_worker_backend() -> WorkerBackend:
    """Returns the singleton worker backend based on WORKER_BACKEND env var.

    Options:
        - 'sandbox' (default): Vertex Agent Platform Sandbox
        - 'local': Local host git worktree + subprocess
    """
    global _active_worker
    if _active_worker is None:
        backend_choice = os.getenv("WORKER_BACKEND", "sandbox").lower()
        if backend_choice == "local":
            _active_worker = LocalWorker()
        else:
            _active_worker = SandboxWorker()
    return _active_worker


def reset_worker_backend() -> None:
    global _active_worker
    _active_worker = None
