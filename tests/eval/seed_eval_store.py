"""Seeds the local TaskStore SQLite database (~/.sonar/tasks.db) before running `agents-cli eval run`.

This keeps `app/` completely free of evaluation fixtures while testing the real
cross-restart database hydration path (`TaskStore` -> `TaskRecord.to_task_handle()`)
when `agents-cli eval run --mode adk_live` boots a fresh server process.
"""

from __future__ import annotations

import asyncio
import time

from app.store.engine import close_db, get_async_engine, init_db
from app.store.models import Base
from app.store.task_store import get_task_store
from app.tasks import TaskHandle


async def seed_overnight_tasks() -> None:
    engine = get_async_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await init_db()
    store = get_task_store()
    now = time.monotonic()

    handle_901 = TaskHandle(
        task_id="task-901",
        goal="Add token bucket rate limiter to webhook endpoint",
        repo="billing-svc",
        started_at=now - 3600.0,
        harness="claude",
        mode="execute",
        branch="agent/task-901",
        _status="awaiting_approval",
        awaiting_approval=True,
        pending_action="Commit and push rate limiter changes on agent/task-901",
        summary="Implemented token bucket rate limiter in billing-svc and paused at approval gate.",
        response_text="Implemented Redis-backed token bucket limiter (100 req/min) in src/webhooks/limiter.py with unit tests.",
        diff_summary="2 files changed, 48 insertions(+), 4 deletions(-)",
        raw_diff=(
            "diff --git a/src/webhooks/limiter.py b/src/webhooks/limiter.py\n"
            "+++ b/src/webhooks/limiter.py\n"
            "@@ -12,4 +12,9 @@ def check_rate_limit(client_id: str) -> bool:\n"
            "+    bucket = TokenBucket(rate=100, capacity=100)\n"
            "+    return bucket.consume(client_id, 1)\n"
        ),
        files_changed=["src/webhooks/limiter.py", "tests/test_limiter.py"],
    )

    handle_902 = TaskHandle(
        task_id="task-902",
        goal="Migrate telemetry ingestion schema to v2",
        repo="analytics-svc",
        started_at=now - 7200.0,
        harness="antigravity",
        mode="execute",
        branch="agent/task-902",
        _status="failed",
        exit_code=1,
        summary="Migration test suite failed due to missing DATABASE_URL environment variable in sandbox.",
        error=(
            "Traceback (most recent call last):\n"
            '  File "/workspace/.venv/lib/python3.12/site-packages/sqlalchemy/engine/create.py", line 64, in create_engine\n'
            "    raise KeyError('DATABASE_URL environment variable is not set in sandbox container')\n"
            "KeyError: 'DATABASE_URL environment variable is not set in sandbox container'"
        ),
        files_changed=["src/telemetry/schema_v2.py"],
    )

    await store.sync_from_handle(handle_901, ended=True)
    await store.sync_from_handle(handle_902, ended=True)
    await close_db()
    print("Seeded task-901 (awaiting_approval) and task-902 (failed) into TaskStore.")


if __name__ == "__main__":
    asyncio.run(seed_overnight_tasks())
