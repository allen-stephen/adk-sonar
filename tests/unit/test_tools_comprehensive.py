"""Comprehensive unit tests for task_tools, grounding_tools, workspace_tools, integrations, and api_routes."""

from __future__ import annotations

import asyncio
from pathlib import Path
from fastapi.testclient import TestClient
import pytest

from app.agent import (
    approve_task,
    cancel_task,
    configure_integration,
    create_or_clone_repository,
    dispatch_task,
    get_task_result,
    inspect_repository_files,
    list_harnesses,
    list_integrations,
    list_repositories,
    list_tasks,
    read_workspace_file,
    search_maps_grounded,
    search_web_grounded,
    set_coding_harness,
    steer_task,
    stop_streaming,
    watch_tasks,
)
from app.fast_api_app import app
from app.integrations import (
    get_integration_registry,
    make_auth_header_provider,
    reset_integration_registry,
)
from app.tasks import get_task_registry, reset_task_registry
from app.workers import get_harness_registry, reset_harness_registry

client = TestClient(app)


@pytest.fixture(autouse=True)
def _clean_all():
    reset_task_registry()
    reset_harness_registry()
    reset_integration_registry()
    yield
    reset_task_registry()
    reset_harness_registry()
    reset_integration_registry()


@pytest.mark.asyncio
async def test_task_tools_all_branches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Cover all tool branches in app/tools/task_tools.py."""
    monkeypatch.setenv("ORCHESTRATOR_WORKSPACE_ROOT", str(tmp_path))

    # Empty list_tasks
    assert "No tasks are currently running" in await list_tasks()

    # Invalid harness on dispatch_task and set_coding_harness
    bad_dispatch = await dispatch_task(
        goal="Test", repo="r1", harness="unknown_harness"
    )
    assert "Cannot dispatch" in bad_dispatch
    bad_set = await set_coding_harness("unknown_harness")
    assert "Unknown coding harness" in bad_set

    # Missing task on get_task_result, steer_task, approve_task, cancel_task
    assert "No task found" in await get_task_result("task-999")
    assert "was not found" in await steer_task("task-999", "do x")
    assert "was not found" in await approve_task("task-999")
    assert "Could not cancel" in await cancel_task("task-999")

    # Dispatch plan task + approval-required task
    reg = get_harness_registry()
    reg.get("claude").mock_step_delay_s = 0.02  # type: ignore[attr-defined]
    await dispatch_task(goal="Plan feature", repo="r1", mode="plan", harness="claude")
    await dispatch_task(
        goal="Code feature",
        repo="r1",
        mode="execute",
        harness="claude",
        require_approval=True,
    )

    task_reg = get_task_registry()
    h1 = await task_reg.get("task-1")
    h2 = await task_reg.get("task-2")
    assert h1 and h2
    await asyncio.gather(h1.async_task, h2.async_task)

    # list_tasks with awaiting_input and awaiting_approval
    tasks_summary = await list_tasks()
    assert "Awaiting your input" in tasks_summary
    assert "Awaiting your approval" in tasks_summary

    # watch_tasks yielding awaiting_input and awaiting_approval notifications
    watcher = watch_tasks()
    msg1 = await asyncio.wait_for(watcher.__anext__(), timeout=2.0)
    msg2 = await asyncio.wait_for(watcher.__anext__(), timeout=2.0)
    await watcher.aclose()
    combined = f"{msg1} | {msg2}"
    assert "has questions" in combined
    assert "ready for your approval" in combined.lower()

    # stop_streaming
    assert "Stopped streaming" in await stop_streaming("watch_tasks")


@pytest.mark.asyncio
async def test_grounding_and_integration_tools(monkeypatch: pytest.MonkeyPatch):
    """Cover search_web_grounded, search_maps_grounded, configure_integration, and auth header provider."""
    from google.genai import types
    from app.agent import root_agent
    from app.tools.grounding_tools import GROUNDING_MODEL

    assert GROUNDING_MODEL == "gemini-3.8-flash"

    # Verify specialist & harness tools on root_agent have FunctionResponseScheduling.WHEN_IDLE (Behavior.NON_BLOCKING)
    tool_map = {getattr(t, "name", getattr(t, "__name__", "")): t for t in root_agent.tools}
    for nb_name in ("search_web_grounded", "search_maps_grounded", "dispatch_task", "steer_task", "approve_task"):
        assert tool_map[nb_name].response_scheduling == types.FunctionResponseScheduling.WHEN_IDLE

    # Toggle google_search off -> returns disabled message
    await configure_integration("google_search", False)
    assert "disabled" in await search_web_grounded("latest python release")

    await configure_integration("google_maps", False)
    assert "disabled" in await search_maps_grounded("coffee shops")

    # Unknown integration
    assert "Unknown integration" in await configure_integration("nonexistent_mcp", True)

    # Re-enable and stub _run_specialist to test enabled path + exception handling
    await configure_integration("google_search", True)
    await configure_integration("google_maps", True)

    async def _fake_specialist(_agent, prompt: str) -> str:
        return f"Grounded answer for: {prompt}"

    monkeypatch.setattr("app.tools.grounding_tools._run_specialist", _fake_specialist)
    web_ans = await search_web_grounded("ADK documentation")
    assert "Grounded answer for: ADK documentation" in web_ans
    maps_ans = await search_maps_grounded("library", "Mountain View")
    assert "Grounded answer for: library near Mountain View" in maps_ans

    # Test auth header provider with session state and env fallback
    class _DummyCtx:
        state = {"MY_TOKEN": "session-secret-123"}

    provider = make_auth_header_provider("MY_TOKEN")
    assert provider(_DummyCtx()) == {"Authorization": "Bearer session-secret-123"}  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_workspace_tools_edge_cases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Cover edge cases in workspace_tools.py (missing repos, subdirectories, >30 files, existing repos)."""
    monkeypatch.setenv("ORCHESTRATOR_WORKSPACE_ROOT", str(tmp_path))

    # Inspect missing repository
    assert "was not found" in await inspect_repository_files("missing-repo")
    assert "was not found" in await read_workspace_file("missing-repo", "file.py")

    # Create repository and re-create (already exists branch)
    await create_or_clone_repository("demo-repo")
    already_msg = await create_or_clone_repository("demo-repo")
    assert "already exists" in already_msg

    # Non-existent subdirectory
    assert "does not exist" in await inspect_repository_files("demo-repo", "no-such-dir")

    # Create 35 files to test >30 truncation
    repo_dir = tmp_path / "demo-repo"
    for i in range(35):
        (repo_dir / f"mod_{i:02d}.py").write_text(f"x = {i}\n")
    files_out = await inspect_repository_files("demo-repo")
    assert "more files" in files_out


def test_api_routes_control_plane_endpoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Cover control-plane endpoints in app/api_routes.py."""
    monkeypatch.setenv("ORCHESTRATOR_WORKSPACE_ROOT", str(tmp_path))

    # 1. Switch default harness and trigger provisioning via API
    res_h = client.post("/api/v1/harnesses/default", json={"harness": "horizon"})
    assert res_h.status_code == 200
    assert res_h.json()["default_harness"]["name"] == "horizon"

    res_prov = client.post("/api/v1/harnesses/claude/provision")
    assert res_prov.status_code == 200

    # 2. Create task, steer task, cancel task via REST API
    create_res = client.post(
        "/api/v1/tasks",
        json={"goal": "Add caching", "repo": "api-svc", "mode": "plan", "harness": "claude"},
    )
    assert create_res.status_code == 200
    assert create_res.json()["ok"] is True
    tid = "task-1"

    steer_res = client.post(
        f"/api/v1/tasks/{tid}/steer",
        json={"instruction": "Use Redis", "mode": "execute"},
    )
    assert steer_res.status_code == 200

    cancel_res = client.post(f"/api/v1/tasks/{tid}/cancel")
    assert cancel_res.status_code == 200

    # 3. Workspace auth connection, surface dismiss, & secret put/delete
    ws_res = client.post(
        "/api/v1/integrations/workspace",
        json={"token": "ya29.test-oauth-token", "surfaces": {"gmail": True}},
    )
    assert ws_res.status_code == 200
    assert ws_res.json()["ok"] is True

    dismiss_res = client.post("/api/v1/a2ui/dismiss", json={"surface_id": "a2ui-plan-task-1"})
    assert dismiss_res.status_code == 200

    sec_put = client.put(
        "/api/v1/secrets",
        json={"name": "GITHUB_PERSONAL_ACCESS_TOKEN", "value": "ghp_secret123456"},
    )
    assert sec_put.status_code == 200
    sec_del = client.delete("/api/v1/secrets/GITHUB_PERSONAL_ACCESS_TOKEN")
    assert sec_del.status_code == 200
