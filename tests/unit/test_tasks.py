import asyncio
from pathlib import Path

import pytest

from app.agent import (
    create_or_clone_repository,
    dispatch_task,
    get_task_result,
    inspect_repository_files,
    list_integrations,
    list_repositories,
    read_workspace_file,
    steer_task,
)
from app.integrations import IntegrationRegistry, reset_integration_registry
from app.tasks import TaskRegistry, reset_task_registry
from app.workers import SandboxWorker, WorkerExecutionResult


@pytest.mark.asyncio
async def test_task_registry_lifecycle():
    registry = TaskRegistry()

    async def coro():
        return WorkerExecutionResult(
            exit_code=0,
            summary="Finished successfully.",
            response_text="Created prime generator module.",
            claude_session_id="sess-123",
            files_changed=["primes.py"],
        )

    task_id, handle = await registry.register(
        goal="Refactor auth",
        repo="my-repo",
        task_coro_fn=coro,
    )
    assert task_id == "task-1"
    assert handle.goal == "Refactor auth"

    await handle.async_task
    assert handle.status == "completed"
    assert handle.response_text == "Created prime generator module."
    assert handle.files_changed == ["primes.py"]
    assert handle.claude_session_id == "sess-123"

    finished = await registry.wait_next_unconsumed(timeout_s=1.0)
    assert finished is not None
    assert finished.task_id == "task-1"
    assert finished.consumed is True

    again = await registry.wait_next_unconsumed(timeout_s=0.1)
    assert again is None


@pytest.mark.asyncio
async def test_plan_and_steer_lifecycle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ORCHESTRATOR_WORKSPACE_ROOT", str(tmp_path))
    reset_task_registry()

    # 1. Dispatch in plan mode
    dispatch_msg = await dispatch_task(
        goal="Write prime number generator",
        repo="test-a",
        mode="plan",
    )
    assert "task-1" in dispatch_msg
    assert "plan mode" in dispatch_msg

    registry = get_task_registry() if False else None  # use agent tools directly
    from app.tasks import get_task_registry as _get_reg

    reg = _get_reg()
    handle = await reg.get("task-1")
    assert handle is not None
    await handle.async_task
    assert handle.status == "awaiting_input"
    assert len(handle.questions) > 0

    result_msg = await get_task_result("task-1")
    assert "awaiting_input" in result_msg
    assert "Questions for you:" in result_msg

    # 2. Steer task to execute mode
    steer_msg = await steer_task(
        task_id="task-1",
        instruction="Use a library function and include pytest benchmarks.",
        mode="execute",
    )
    assert "Resumed task-1 in execute mode" in steer_msg
    await handle.async_task
    assert handle.status == "completed"
    assert len(handle.files_changed) == 1

    # 3. Inspect workspace files created by the worker
    repos_out = await list_repositories()
    assert "test-a" in repos_out
    files_out = await inspect_repository_files("test-a")
    assert "task_1_output.py" in files_out


@pytest.mark.asyncio
async def test_workspace_tools_honesty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Verify workspace tools operate strictly on real directories without fake seeded repos."""
    monkeypatch.setenv("ORCHESTRATOR_WORKSPACE_ROOT", str(tmp_path))

    # Empty workspace initially
    empty_msg = await list_repositories()
    assert "currently has no repositories" in empty_msg

    # Create a real git repository
    create_msg = await create_or_clone_repository("test-a")
    assert "Initialized new git repository test-a" in create_msg

    # Verify it now appears with real git status
    list_msg = await list_repositories()
    assert "test-a on branch main" in list_msg

    read_msg = await read_workspace_file("test-a", "README.md")
    assert "# test-a" in read_msg


@pytest.mark.asyncio
async def test_integrations_mcp_toolset_builder(monkeypatch: pytest.MonkeyPatch):
    """Verify ADK McpToolset construction with StreamableHTTPConnectionParams + header_provider."""
    for env_key in (
        "GOOGLE_WORKSPACE_ACCESS_TOKEN",
        "GOOGLE_WORKSPACE_REFRESH_TOKEN",
        "GITHUB_PERSONAL_ACCESS_TOKEN",
        "SPOTIFY_ACCESS_TOKEN",
        "SPOTIFY_REFRESH_TOKEN",
        "SLACK_BOT_TOKEN",
    ):
        monkeypatch.delenv(env_key, raising=False)

    reset_integration_registry()
    reg = IntegrationRegistry()

    # Without credentials in env, only_with_credentials=True skips unauthenticated toolsets
    assert reg.build_adk_mcp_toolsets(only_with_credentials=True) == []

    # When GOOGLE_WORKSPACE_ACCESS_TOKEN is provided, Google Workspace McpToolsets mount
    monkeypatch.setenv("GOOGLE_WORKSPACE_ACCESS_TOKEN", "test-oauth-token")
    toolsets = reg.build_adk_mcp_toolsets(only_with_credentials=True)
    assert len(toolsets) >= 3  # Calendar, Gmail, Drive, Universal Search

    # Verify Claude .mcp.json config generation
    claude_cfg = reg.build_claude_mcp_config()
    assert "google_calendar" in claude_cfg["mcpServers"]
    assert claude_cfg["mcpServers"]["google_calendar"]["url"] == "https://calendarmcp.googleapis.com/mcp/v1"

    status_str = await list_integrations()
    assert "Google Calendar MCP (ready)" in status_str
