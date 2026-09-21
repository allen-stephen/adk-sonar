"""Comprehensive unit tests for LocalWorker, AntigravityHarness, SandboxProvisioner, and HarnessRegistry."""

from __future__ import annotations

import asyncio
from pathlib import Path
import pytest

from app.workers import (
    AntigravityHarness,
    ClaudeCodeHarness,
    HarnessNotProvisionedError,
    LocalWorker,
    SandboxContext,
    get_default_provision_recipe,
    get_harness_registry,
    get_sandbox_provisioner,
    get_worker_backend,
    reset_harness_registry,
    reset_sandbox_provisioner,
    reset_worker_backend,
)


@pytest.fixture(autouse=True)
def _reset_state():
    reset_harness_registry()
    reset_worker_backend()
    reset_sandbox_provisioner()
    yield
    reset_harness_registry()
    reset_worker_backend()
    reset_sandbox_provisioner()


def test_worker_factory_env_selection(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("WORKER_BACKEND", "local")
    reset_worker_backend()
    w_local = get_worker_backend()
    assert isinstance(w_local, LocalWorker)

    monkeypatch.setenv("WORKER_BACKEND", "sandbox")
    reset_worker_backend()
    w_sandbox = get_worker_backend()
    assert not isinstance(w_sandbox, LocalWorker)


@pytest.mark.asyncio
async def test_local_worker_execute_and_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Test LocalWorker with a mock CLI script emitting stream-json output, missing binaries, and task cancellation."""
    monkeypatch.setenv("ORCHESTRATOR_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("MOCK_REMOTE_RUNNER", "false")
    worker = LocalWorker(worktrees_dir=tmp_path / "wt")

    # Pre-mark claude as provisioned in local-worktree so LocalWorker exercises subprocess execution
    prov = get_sandbox_provisioner()
    claude = get_harness_registry().get("claude")
    await prov.ensure_provisioned(
        claude,
        SandboxContext(sandbox_name="local-worktree", mock_mode=True),
    )

    # 1. When binary is missing and MOCK_REMOTE_RUNNER=false, returns exit_code=1 with helpful message
    monkeypatch.setattr("shutil.which", lambda _name: None)
    missing_res = await worker.execute_task(
        goal="Fix bug", repo="test-repo", task_id="task-missing", harness="claude"
    )
    assert missing_res.exit_code == 1
    assert "not found on local PATH" in missing_res.summary

    # 2. Create a fake CLI executable script that outputs stream-json lines
    fake_cli = tmp_path / "fake_claude.sh"
    fake_cli.write_text(
        "#!/bin/sh\n"
        'echo \'{"type": "tool_use", "name": "Edit"}\'\n'
        'echo \'{"type": "assistant", "text": "Edited file"}\'\n'
        'echo \'{"type": "result", "is_error": false, "result": "Local CLI task finished cleanly."}\'\n'
    )
    fake_cli.chmod(0o755)
    monkeypatch.setattr("shutil.which", lambda _name: str(fake_cli))

    captured_events = []
    ok_res = await worker.execute_task(
        goal="Update README",
        repo="test-repo",
        task_id="task-ok",
        harness="claude",
        on_event=lambda ev: captured_events.append(ev),
    )
    assert ok_res.exit_code == 0
    assert "Local CLI task finished cleanly." in ok_res.summary
    assert len(captured_events) >= 3

    # 3. Test LocalWorker cancellation of a long-running subprocess
    slow_cli = tmp_path / "slow_claude.sh"
    slow_cli.write_text("#!/bin/sh\nsleep 10\n")
    slow_cli.chmod(0o755)
    monkeypatch.setattr("shutil.which", lambda _name: str(slow_cli))

    run_task = asyncio.create_task(
        worker.execute_task(
            goal="Long job", repo="test-repo", task_id="task-slow", harness="claude"
        )
    )
    await asyncio.sleep(0.15)
    cancelled = await worker.cancel_task("task-slow")
    assert cancelled is True
    run_task.cancel()
    cancel_res = await run_task
    assert cancel_res.exit_code in (-1, -15, 143)


@pytest.mark.asyncio
async def test_antigravity_harness_modes_and_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Test AntigravityHarness plan mode, execute mode with approval gate, env vars, and stream parsing."""
    monkeypatch.setenv("ORCHESTRATOR_WORKSPACE_ROOT", str(tmp_path))
    agy = AntigravityHarness(mock_step_delay_s=0.01)

    env = agy.build_env()
    assert "GOOGLE_GENAI_USE_VERTEXAI" in env
    assert "agy -p" in agy.format_sandbox_command(
        goal="Build UI", repo="ui-repo", task_id="task-a1"
    )

    ctx = SandboxContext(
        user_id="dev",
        sandbox_name="voice-worker-dev",
        mock_mode=True,
    )

    plan_res = await agy.execute_in_sandbox(
        goal="Design component",
        repo="ui-repo",
        task_id="task-a1",
        context=ctx,
        mode="plan",
    )
    assert plan_res.awaiting_input is True
    assert len(plan_res.questions) >= 1

    exec_events = []
    exec_res = await agy.execute_in_sandbox(
        goal="Implement component",
        repo="ui-repo",
        task_id="task-a2",
        context=ctx,
        mode="execute",
        require_approval=True,
        on_event=lambda e: exec_events.append(e),
    )
    assert exec_res.exit_code == 0
    assert exec_res.awaiting_approval is True
    assert exec_res.diff_summary is not None
    assert len(exec_events) >= 2

    ctx_live = SandboxContext(mock_mode=False)
    live_res = await agy.execute_in_sandbox(
        goal="Live run", repo="ui-repo", task_id="task-a3", context=ctx_live
    )
    assert live_res.exit_code == 0


@pytest.mark.asyncio
async def test_sandbox_provisioner_and_registry():
    """Test SandboxProvisioner recipes, preflight checks, background warming, and simulated failures."""
    for name in ("horizon", "claude", "antigravity", "custom"):
        recipe = get_default_provision_recipe(name)
        assert "health_probe" in recipe

    prov = get_sandbox_provisioner()
    ctx = SandboxContext(sandbox_name="voice-worker-test", mock_mode=True)
    reg = get_harness_registry()
    claude = reg.get("claude")
    agy = reg.get("antigravity")

    status = await prov.ensure_provisioned(claude, ctx, force_refresh=True)
    assert status.status == "ready"
    assert status.to_dict()["harness"] == "claude"

    # Second call when already ready hits cached health check
    cached_status = await prov.ensure_provisioned(claude, ctx, force_refresh=False)
    assert cached_status.status == "ready"

    # Background warming
    warm_task = prov.warm_up_in_background(agy, ctx)
    if warm_task:
        await warm_task
    assert prov.get_status("antigravity", ctx.sandbox_name).status == "ready"

    # Simulated failure raises HarnessNotProvisionedError
    prov.simulate_failure("claude", "npm registry timeout")
    with pytest.raises(HarnessNotProvisionedError, match="npm registry timeout"):
        await prov.ensure_provisioned(claude, ctx, force_refresh=True)

    prov.simulate_failure("claude", None)
    recovered = await prov.ensure_provisioned(claude, ctx, force_refresh=True)
    assert recovered.status == "ready"

    # Custom harness registration & error handling in HarnessRegistry
    reg.register_harness(ClaudeCodeHarness(name="custom_bot"), aliases=["cbot"])
    assert reg.get("cbot").name == "custom_bot"
    with pytest.raises(ValueError, match="Unknown coding harness"):
        reg.get("nonexistent_harness")
