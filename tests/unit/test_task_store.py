"""Unit tests for the durable TaskStore repository and dual-write engine."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.store.models import Base
from app.store.task_store import TaskStore
from app.tasks import TaskRegistry, reset_task_registry
from app.workers.base import WorkerExecutionResult


@pytest_asyncio.fixture(loop_scope="function")
async def memory_store(tmp_path: Path):
    """Provides an isolated SQLite TaskStore for testing."""
    db_file = tmp_path / "test_store.db"
    db_url = f"sqlite+aiosqlite:///{db_file}"
    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    store = TaskStore(session_factory=session_factory)
    store._initialized = True
    yield store
    await engine.dispose()


@pytest.mark.asyncio
async def test_task_store_crud(memory_store: TaskStore):
    # 1. Create task
    task = await memory_store.create_task(
        goal="Add rate limiting",
        repo="gateway-svc",
        short_id="task-42",
        status="running",
    )
    assert task.short_id == "task-42"
    assert task.goal == "Add rate limiting"
    assert task.status == "running"

    # 2. Retrieve by short_id and id
    by_short = await memory_store.get_task("task-42")
    assert by_short is not None
    assert by_short.id == task.id

    by_id = await memory_store.get_task(task.id)
    assert by_id is not None
    assert by_id.short_id == "task-42"

    # 3. Create run
    run = await memory_store.create_run(
        task_id=task.id,
        harness="claude",
        mode="plan",
        status="running",
    )
    assert run.task_id == task.id
    assert run.seq == 1
    assert run.harness == "claude"

    # 4. Update run
    await memory_store.update_run(
        run.id,
        status="succeeded",
        exit_code=0,
        summary="Plan completed with 2 options.",
        ended=True,
    )

    # 5. Record event
    event = await memory_store.record_event(
        task_id=task.id,
        run_id=run.id,
        kind="progress",
        message="Created PLAN.md with options",
    )
    assert event.task_id == task.id
    assert event.message == "Created PLAN.md with options"

    # 6. Update task status
    updated = await memory_store.update_task_status(task.id, status="awaiting_approval")
    assert updated is not None
    assert updated.status == "awaiting_approval"

    # 7. List tasks
    all_tasks = await memory_store.list_tasks()
    assert len(all_tasks) == 1
    assert all_tasks[0].short_id == "task-42"


@pytest.mark.asyncio
async def test_task_store_cursor_and_unseen_events(memory_store: TaskStore):
    principal = "user-alice"
    task = await memory_store.create_task(
        goal="Update telemetry",
        repo="analytics-svc",
        short_id="task-101",
        principal_id=principal,
    )

    # Record completed event
    ev1 = await memory_store.record_event(
        task_id=task.id,
        kind="completed",
        message="Task 101 completed successfully",
    )

    # Initially unseen
    unseen = await memory_store.get_unseen_events(principal)
    assert len(unseen) == 1
    assert unseen[0].id == ev1.id

    # Advance cursor
    await memory_store.advance_cursor(principal, ev1.id)

    # Now no unseen events
    after_cursor = await memory_store.get_unseen_events(principal)
    assert len(after_cursor) == 0


@pytest.mark.asyncio
async def test_task_registry_dual_writes_to_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_file = tmp_path / "dual_write.db"
    monkeypatch.setenv("TASK_DB_URL", f"sqlite+aiosqlite:///{db_file}")

    from app.store.engine import close_db, init_db
    from app.store.task_store import get_task_store, reset_task_store

    reset_task_store()
    reset_task_registry()
    await init_db()

    registry = TaskRegistry()

    async def coro():
        return WorkerExecutionResult(
            exit_code=0,
            summary="Refactor done.",
            files_changed=["main.py"],
        )

    task_id, handle = await registry.register(
        goal="Refactor logger",
        repo="core-svc",
        task_coro_fn=coro,
    )
    await handle.async_task
    # Let async dual-write task finish
    await asyncio.sleep(0.1)

    store = get_task_store()
    persisted = await store.get_task(task_id)
    assert persisted is not None
    assert persisted.short_id == task_id
    assert persisted.status == "completed"

    await close_db()
    reset_task_store()
    reset_task_registry()


@pytest.mark.asyncio
async def test_turn_zero_prompt_seeding(monkeypatch: pytest.MonkeyPatch):
    """Verify active/recent tasks are dynamically seeded into agent instruction."""
    from app.agent import build_orchestrator_instruction
    from app.tasks import TaskHandle, get_task_registry, reset_task_registry

    reset_task_registry()
    reg = get_task_registry()

    handle = TaskHandle(
        task_id="task-7",
        goal="Migrate auth",
        repo="auth-svc",
        started_at=100.0,
        harness="claude",
        branch="agent/task-7",
        _status="awaiting_approval",
    )
    reg._tasks["task-7"] = handle

    class _DummyContext:
        pass

    instruction = build_orchestrator_instruction(_DummyContext())  # type: ignore[arg-type]
    assert "ACTIVE & RECENT BACKGROUND CODING TASKS" in instruction
    assert "task-7 on auth-svc" in instruction
    assert "awaiting_approval" in instruction or "changes ready for review" in instruction

    reset_task_registry()


@pytest.mark.asyncio
async def test_read_path_fallback_to_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Verify TaskRegistry.get, TaskRegistry.list_all, get_task_result, and list_tasks hydrate full TaskHandles after a restart."""
    db_file = tmp_path / "fallback.db"
    monkeypatch.setenv("TASK_DB_URL", f"sqlite+aiosqlite:///{db_file}")

    from app.agent import get_task_result, list_tasks
    from app.store.engine import close_db, init_db
    from app.store.task_store import reset_task_store
    from app.tasks import get_task_registry, reset_task_registry

    reset_task_store()
    reset_task_registry()
    await init_db()

    reg = get_task_registry()

    async def _plan_coro():
        return WorkerExecutionResult(
            exit_code=0,
            summary="Drafted rate limiting plan in PLAN.md.",
            response_text="Created PLAN.md with token bucket strategy.",
            files_changed=["PLAN.md", "src/limiter.py"],
            awaiting_approval=True,
            pending_action="Apply rate limiter changes to src/limiter.py",
        )

    task_id, handle = await reg.register(
        goal="Add rate limiting",
        repo="billing-svc",
        mode="plan",
        harness="claude",
        task_coro_fn=_plan_coro,
    )
    await handle.async_task

    # Simulate a complete server restart by wiping the in-memory TaskRegistry
    reset_task_registry()
    fresh_reg = get_task_registry()
    assert len(fresh_reg._tasks) == 0

    # Hydrate via get_task_result and list_tasks across restart
    task_res = await get_task_result(task_id)
    assert f"{task_id} (harness: claude, branch: agent/{task_id}) status is awaiting_approval." in task_res
    assert "Created PLAN.md with token bucket strategy." in task_res
    assert "PLAN.md, src/limiter.py" in task_res
    assert "Apply rate limiter changes to src/limiter.py" in task_res

    tasks_list = await list_tasks()
    assert f"Awaiting your approval: {task_id} (claude) ready for diff approval on branch agent/{task_id}" in tasks_list

    await close_db()
    reset_task_store()
    reset_task_registry()


@pytest.mark.asyncio
async def test_detached_command_lifecycle():
    import httpx

    from app.workers.harnesses.base import (
        SandboxContext,
        cancel_detached_command,
        launch_detached_command,
        poll_detached_command,
    )

    def _handler(request: httpx.Request) -> httpx.Response:
        body = request.read().decode()
        if "tail" in body:
            return httpx.Response(
                200,
                json={
                    "stdout": '{"state":"completed","exit_code":0}\n---STATUS_DELIMITER---\n{"type":"message","text":"all tests passed"}\n',
                    "exit_code": 0,
                },
            )
        return httpx.Response(
            200,
            json={"stdout": '{"pid":1234,"run_dir":"/workspace/.sonar/runs/r1"}', "exit_code": 0},
        )

    context = SandboxContext(
        user_id="u1",
        sandbox_name="sb1",
        lb_host="localhost",
        routing_token="rt",
        sandbox_token="st",
        http_transport=httpx.MockTransport(_handler),
    )

    handle = await launch_detached_command(
        context,
        "claude -p 'test'",
        run_id="run-1",
        worktree_path="/workspace/.worktrees/task-1",
    )
    assert handle["run_id"] == "run-1"
    assert "/workspace/.sonar/runs/run-1" in handle["run_dir"]

    status_data, new_stdout, new_offset = await poll_detached_command(context, handle, offset=0)
    assert status_data["state"] == "completed"
    assert "all tests passed" in new_stdout
    assert new_offset > 0

    cancelled = await cancel_detached_command(context, handle)
    assert cancelled is True


@pytest.mark.asyncio
async def test_reconciler_claims_and_updates_run(memory_store: TaskStore, monkeypatch: pytest.MonkeyPatch):
    from app.store.reconciler import reconcile_once
    from app.workers.harnesses.base import SandboxExecResult

    async def _mock_exec(_ctx, _cmd, **_kw):
        return SandboxExecResult(
            exit_code=0,
            stdout='{"state":"completed","exit_code":0}\n---STATUS_DELIMITER---\nTask finished successfully\n',
        )

    monkeypatch.setattr("app.store.reconciler.poll_detached_command", lambda ctx, handle, offset=0: _mock_poll_return())

    async def _mock_poll_return():
        return {"state": "completed", "exit_code": 0}, "Task finished successfully\n", 30

    monkeypatch.setattr(
        "app.store.reconciler.poll_detached_command",
        lambda *args, **kwargs: _mock_poll_return(),
    )

    task = await memory_store.create_task(
        goal="Refactor DB",
        repo="data-svc",
        short_id="task-500",
        status="running",
    )
    run = await memory_store.create_run(
        task_id=task.id,
        harness="claude",
        mode="execute",
        status="running",
    )
    await memory_store.update_run(
        run.id,
        run_handle={"run_id": run.id, "run_dir": f"/workspace/.sonar/runs/{run.id}"},
    )

    reconciled = await reconcile_once(replica_id="replica-test", store=memory_store)
    assert reconciled >= 1

    updated_task = await memory_store.get_task("task-500")
    assert updated_task is not None
    assert updated_task.status == "completed"


@pytest.mark.asyncio
async def test_non_blocking_memory_capture_zero_latency():
    import time

    from app.agent import non_blocking_memory_capture

    call_completed = asyncio.Event()

    class _MockContext:
        async def add_session_to_memory(self):
            await asyncio.sleep(0.3)  # Simulate slow remote Vertex call
            call_completed.set()

    ctx = _MockContext()
    t0 = time.monotonic()
    await non_blocking_memory_capture(ctx)
    elapsed = time.monotonic() - t0

    # Verify callback returned in < 30ms (zero user-perceptible latency)
    assert elapsed < 0.03
    # Verify background task actually executes
    await asyncio.wait_for(call_completed.wait(), timeout=1.0)



