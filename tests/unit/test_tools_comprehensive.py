"""Comprehensive unit tests for task_tools, grounding_tools, workspace_tools, integrations, and api_routes."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import ClassVar

import pytest
from fastapi.testclient import TestClient

from app.agent import (
    approve_task,
    cancel_task,
    configure_integration,
    create_or_clone_repository,
    dispatch_task,
    get_task_result,
    inspect_repository_files,
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
    make_auth_header_provider,
    reset_integration_registry,
)
from app.tasks import get_task_registry, reset_task_registry
from app.workers import (
    HarnessNotProvisionedError,
    SandboxContext,
    SandboxWorker,
    get_harness_registry,
    get_sandbox_provisioner,
    reset_harness_registry,
    reset_sandbox_provisioner,
)
from tests.fakes import FakeHarness, exec_transport, sandbox_connection

client = TestClient(app)


@pytest.fixture(autouse=True)
def _clean_all():
    reset_task_registry()
    reset_harness_registry()
    reset_sandbox_provisioner()
    reset_integration_registry()
    yield
    reset_task_registry()
    reset_harness_registry()
    reset_sandbox_provisioner()
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

    # Dispatch plan task + approval-required task.
    # Two scripted harnesses stand in for real CLIs: one returns clarifying
    # questions from plan mode, the other produces changes behind an approval gate.
    reg = get_harness_registry()
    planner = FakeHarness(
        name="fake_planner",
        display_name="Fake Planner",
        summary="Drafted a plan for the feature.",
        questions=["Should the feature be behind a flag?"],
    )
    builder = FakeHarness(
        name="fake_builder",
        display_name="Fake Builder",
        summary="Implemented the feature.",
        writes={"feature.py": "def feature() -> str:\n    return 'ok'\n"},
    )
    reg.register_harness(planner, aliases=["planner"])
    reg.register_harness(builder, aliases=["builder"])

    # The worker's real preflight gate still runs, so hand it a sandbox
    # connection plus a mock `/exec` transport instead of live Vertex calls.
    monkeypatch.setattr(
        "app.workers.factory._active_worker",
        SandboxWorker(
            connection=sandbox_connection(),
            http_transport=exec_transport(exit_code=0),
        ),
    )

    await dispatch_task(
        goal="Plan feature", repo="r1", mode="plan", harness="fake_planner"
    )
    await dispatch_task(
        goal="Code feature",
        repo="r1",
        mode="execute",
        harness="fake_builder",
        require_approval=True,
    )

    task_reg = get_task_registry()
    h1 = await task_reg.get("task-1")
    h2 = await task_reg.get("task-2")
    assert h1 and h2
    await asyncio.gather(h1.async_task, h2.async_task)

    assert h1.status == "awaiting_input"
    assert h2.status == "awaiting_approval"
    assert h2.pending_action is not None

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

    # Verify specialist failure fails closed without fabricating canned facts
    async def _failing_specialist(_agent, _prompt: str) -> str:
        raise RuntimeError("Upstream grounding quota exceeded")

    monkeypatch.setattr("app.tools.grounding_tools._run_specialist", _failing_specialist)
    err_web = await search_web_grounded("latest python release")
    assert "could not complete the request" in err_web
    assert "Python 3.13" not in err_web
    err_maps = await search_maps_grounded("coffee shops")
    assert "could not complete the request" in err_maps
    assert "Medici Roasting" not in err_maps

    # Test auth header provider with session state and env fallback
    class _DummyCtx:
        state: ClassVar[dict[str, str]] = {"MY_TOKEN": "session-secret-123"}

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
    monkeypatch.setattr(
        "app.workers.factory._active_worker",
        SandboxWorker(
            connection=sandbox_connection(),
            http_transport=exec_transport(exit_code=0),
        ),
    )

    # 1. Switch default harness and trigger provisioning via API
    res_h = client.post("/api/v1/harnesses/default", json={"harness": "horizon"})
    assert res_h.status_code == 200
    assert res_h.json()["default_harness"]["name"] == "horizon"

    # The provision route now really runs the preflight gate, so stub the
    # provisioner rather than letting it dial out to a sandbox.
    prov = get_sandbox_provisioner()

    async def _ready(harness, context=None, **_kwargs):
        state = prov.get_status(harness.name, (context or SandboxContext()).sandbox_name)
        state.status = "ready"
        state.error = None
        return state

    monkeypatch.setattr(prov, "ensure_provisioned", _ready)
    res_prov = client.post("/api/v1/harnesses/claude/provision")
    assert res_prov.status_code == 200
    assert res_prov.json()["harness"] == "claude"
    assert res_prov.json()["status"] == "ready"

    # An unknown harness is a client error, not a silent success.
    assert client.post("/api/v1/harnesses/not-a-harness/provision").status_code == 404

    # A provisioning failure surfaces as 502 with the underlying reason.
    async def _broken(harness, context=None, **_kwargs):
        raise HarnessNotProvisionedError("npm registry timeout")

    monkeypatch.setattr(prov, "ensure_provisioned", _broken)
    res_broken = client.post("/api/v1/harnesses/claude/provision")
    assert res_broken.status_code == 502
    assert "npm registry timeout" in res_broken.json()["detail"]

    monkeypatch.setattr(prov, "ensure_provisioned", _ready)

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
