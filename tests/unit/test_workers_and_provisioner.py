"""Comprehensive unit tests for LocalWorker, AntigravityHarness, SandboxProvisioner, and HarnessRegistry."""

from __future__ import annotations

import asyncio
import gc
import json
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
from tests.fakes import antigravity_stream_json, exec_transport


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
    """Test LocalWorker with a fake CLI script emitting stream-json output, missing binaries, and task cancellation."""
    monkeypatch.setenv("ORCHESTRATOR_WORKSPACE_ROOT", str(tmp_path))
    worker = LocalWorker(worktrees_dir=tmp_path / "wt")

    # This test covers subprocess execution and cancellation, not provisioning.
    # LocalWorker builds its own SandboxContext (no transport seam to inject), so
    # neutralize only the preflight gate and let the real subprocess path run.
    prov = get_sandbox_provisioner()
    claude = get_harness_registry().get("claude")

    async def _skip_preflight(harness, context=None, **_kwargs):
        state = prov.get_status(harness.name, (context or SandboxContext()).sandbox_name)
        state.status = "ready"
        return state

    monkeypatch.setattr(prov, "ensure_provisioned", _skip_preflight)
    assert claude.execution_mode == "sandbox_exec"  # so LocalWorker spawns a binary

    # 1. When the harness binary is missing, returns exit_code=1 with a helpful message
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

    # Finalize the terminated subprocess transport while this event loop is still
    # open; otherwise its __del__ runs after loop teardown and raises an
    # unraisable "Event loop is closed" warning against an unrelated later test.
    del run_task, cancel_res
    await asyncio.sleep(0.05)
    gc.collect()


@pytest.mark.asyncio
async def test_antigravity_harness_modes_and_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Test AntigravityHarness plan mode, execute mode with approval gate, env vars, stream parsing, and sandbox failure surfacing."""
    monkeypatch.setenv("ORCHESTRATOR_WORKSPACE_ROOT", str(tmp_path))
    agy = AntigravityHarness()

    env = agy.build_env()
    assert "GOOGLE_GENAI_USE_VERTEXAI" in env
    assert "agy -p" in agy.format_sandbox_command(
        goal="Build UI", repo="ui-repo", task_id="task-a1"
    )

    # 1. Plan mode: `agy -p ... --output-format json` returns a plan envelope.
    plan_stdout = json.dumps(
        {
            "conversation_id": "agy-conv-a1",
            "structured_output": {
                "plan_summary": "Add a reusable Button component.",
                "steps": ["Inspect ui-repo", "Add component", "Add tests"],
                "questions": ["Should the component support a loading state?"],
                "files_to_modify": ["src/Button.tsx"],
            },
        }
    )
    plan_ctx = SandboxContext(
        user_id="dev",
        sandbox_name="voice-worker-dev",
        http_transport=exec_transport(stdout=plan_stdout, exit_code=0),
    )
    plan_res = await agy.execute_in_sandbox(
        goal="Design component",
        repo="ui-repo",
        task_id="task-a1",
        context=plan_ctx,
        mode="plan",
    )
    assert plan_res.awaiting_input is True
    assert len(plan_res.questions) >= 1
    assert plan_res.questions == ["Should the component support a loading state?"]
    assert plan_res.claude_session_id == "agy-conv-a1"

    # 2. Execute mode behind an approval gate. The diff summary is derived from
    # the real worktree contents, so seed an actual file change there.
    worktree = tmp_path / "wt-task-a2"
    worktree.mkdir()
    (worktree / "Button.tsx").write_text("export const Button = () => null;\n")

    exec_events = []
    exec_ctx = SandboxContext(
        user_id="dev",
        sandbox_name="voice-worker-dev",
        worktree_dir=worktree,
        branch="agent/task-a2",
        http_transport=exec_transport(
            stdout=antigravity_stream_json(
                response_text="Implemented the Button component."
            ),
            exit_code=0,
        ),
    )
    exec_res = await agy.execute_in_sandbox(
        goal="Implement component",
        repo="ui-repo",
        task_id="task-a2",
        context=exec_ctx,
        mode="execute",
        require_approval=True,
        on_event=lambda e: exec_events.append(e),
    )
    assert exec_res.exit_code == 0
    assert exec_res.awaiting_approval is True
    assert exec_res.pending_action is not None
    assert exec_res.files_changed == ["Button.tsx"]
    assert exec_res.diff_summary is not None
    assert "1 file(s) changed" in exec_res.diff_summary
    assert exec_res.summary == "Implemented the Button component."
    assert len(exec_events) >= 2

    # 3. An unreachable sandbox must surface as a hard failure. The harness has a
    # single real HTTP path now, so a 502 can no longer be swallowed into exit 0.
    dead_ctx = SandboxContext(http_transport=exec_transport(status_code=502))
    dead_res = await agy.execute_in_sandbox(
        goal="Live run", repo="ui-repo", task_id="task-a3", context=dead_ctx
    )
    assert dead_res.exit_code == 502
    assert dead_res.error is not None
    assert "could not reach the sandbox" in dead_res.error
    assert dead_res.files_changed == []


@pytest.mark.asyncio
async def test_sandbox_provisioner_and_registry():
    """Test SandboxProvisioner recipes, preflight checks, background warming, and real provisioning failures."""
    for name in ("horizon", "claude", "antigravity", "custom"):
        recipe = get_default_provision_recipe(name)
        assert "health_probe" in recipe

    prov = get_sandbox_provisioner()
    reg = get_harness_registry()
    claude = reg.get("claude")
    agy = reg.get("antigravity")

    # A sandbox where every provisioning command and health probe succeeds.
    ctx = SandboxContext(
        sandbox_name="voice-worker-test",
        http_transport=exec_transport(exit_code=0),
    )

    status = await prov.ensure_provisioned(claude, ctx, force_refresh=True)
    assert status.status == "ready"
    assert status.to_dict()["harness"] == "claude"
    assert status.commands_executed  # the install command really went over /exec

    # Second call when already ready hits cached health check
    cached_status = await prov.ensure_provisioned(claude, ctx, force_refresh=False)
    assert cached_status.status == "ready"

    # Background warming
    warm_task = prov.warm_up_in_background(agy, ctx)
    if warm_task:
        await warm_task
    assert prov.get_status("antigravity", ctx.sandbox_name).status == "ready"

    # A sandbox whose `/exec` reports a failed install raises HarnessNotProvisionedError
    failing_ctx = SandboxContext(
        sandbox_name="voice-worker-test",
        http_transport=exec_transport(exit_code=1, stderr="npm registry timeout"),
    )
    with pytest.raises(HarnessNotProvisionedError, match="npm registry timeout"):
        await prov.ensure_provisioned(claude, failing_ctx, force_refresh=True)
    assert prov.get_status("claude", ctx.sandbox_name).status == "error"

    # Once the sandbox recovers, provisioning reaches "ready" again
    recovered = await prov.ensure_provisioned(claude, ctx, force_refresh=True)
    assert recovered.status == "ready"
    assert recovered.error is None

    # Custom harness registration & error handling in HarnessRegistry
    reg.register_harness(ClaudeCodeHarness(name="custom_bot"), aliases=["cbot"])
    assert reg.get("cbot").name == "custom_bot"
    with pytest.raises(ValueError, match="Unknown coding harness"):
        reg.get("nonexistent_harness")


# Probe output shapes for the container varieties the provisioner must handle.
_UNPRIVILEGED_PROBE = (
    "uid=1000\n"
    "home=/home/sandbox\n"
    "virtual_env=\n"
    "npm_prefix=/usr/local\n"
    "npm_prefix_writable=0\n"
    "writable=/workspace\n"
    "writable=/home/sandbox\n"
    "binary=uv\n"
    "binary=npm\n"
    "binary=git\n"
)
_ROOT_PROBE = (
    "uid=0\n"
    "home=/root\n"
    "virtual_env=\n"
    "npm_prefix=/usr/local\n"
    "npm_prefix_writable=1\n"
    "writable=/workspace\n"
    "writable=/root\n"
    "writable=/usr/local\n"
    "binary=uv\n"
)
_PREBUILT_IMAGE_PROBE = (
    "uid=1000\n"
    "home=/home/sandbox\n"
    "virtual_env=/opt/sonar-venv\n"
    "npm_prefix=/home/sandbox/.local\n"
    "npm_prefix_writable=1\n"
    "writable=/workspace\n"
    "binary=claude\n"
    "binary=agy\n"
    "binary=uv\n"
)


def test_sandbox_runtime_profile_adapts_to_container(monkeypatch: pytest.MonkeyPatch):
    """The resolved layout must follow the container, not a hardcoded assumption."""
    from app.workers.harnesses.provisioner import (
        parse_sandbox_probe,
        resolve_sandbox_runtime_profile,
    )

    monkeypatch.delenv("SANDBOX_VENV_PATH", raising=False)
    monkeypatch.delenv("SANDBOX_NPM_PREFIX", raising=False)

    # 1. Managed shell sandbox: unprivileged, no venv, unwritable npm prefix.
    unprivileged = resolve_sandbox_runtime_profile(parse_sandbox_probe(_UNPRIVILEGED_PROBE))
    assert unprivileged.venv_path == "/workspace/.sonar-venv"
    assert unprivileged.npm_prefix == "/home/sandbox/.local"
    assert unprivileged.is_root is False

    # 2. Root container: its own npm prefix is writable, so don't redirect it.
    root = resolve_sandbox_runtime_profile(parse_sandbox_probe(_ROOT_PROBE))
    assert root.is_root is True
    assert root.npm_prefix is None
    assert root.npm_global_install("pkg") == "npm install -g pkg"

    # 3. Prebuilt image: adopt the baked environment instead of shadowing it.
    prebuilt = resolve_sandbox_runtime_profile(parse_sandbox_probe(_PREBUILT_IMAGE_PROBE))
    assert prebuilt.venv_path == "/opt/sonar-venv"
    assert prebuilt.reuses_existing_venv is True
    assert prebuilt.has_binary("claude") and prebuilt.has_binary("agy")

    # 4. Explicit configuration outranks anything the probe discovered.
    monkeypatch.setenv("SANDBOX_VENV_PATH", "/srv/pinned-venv")
    pinned = resolve_sandbox_runtime_profile(parse_sandbox_probe(_PREBUILT_IMAGE_PROBE))
    assert pinned.venv_path == "/srv/pinned-venv"
    assert pinned.reuses_existing_venv is False

    # 5. No probe at all (probe failed) still yields a usable profile.
    monkeypatch.delenv("SANDBOX_VENV_PATH", raising=False)
    blind = resolve_sandbox_runtime_profile()
    assert blind.probed is False
    assert blind.venv_path.endswith(".sonar-venv")


def test_install_commands_quote_version_specifiers(monkeypatch: pytest.MonkeyPatch):
    """Unquoted `pkg>=1.0` is a shell redirect: it writes a file named `=1.0` and installs nothing."""
    from app.workers.harnesses.provisioner import (
        parse_sandbox_probe,
        resolve_sandbox_runtime_profile,
    )

    monkeypatch.delenv("SANDBOX_VENV_PATH", raising=False)
    monkeypatch.delenv("SANDBOX_NPM_PREFIX", raising=False)
    profile = resolve_sandbox_runtime_profile(parse_sandbox_probe(_UNPRIVILEGED_PROBE))

    cmd = profile.uv_pip_install(["fastapi>=0.115.0", "uvicorn[standard]>=0.34.0"])
    assert "'fastapi>=0.115.0'" in cmd
    assert "'uvicorn[standard]>=0.34.0'" in cmd
    # No bare specifier may reach the shell unquoted.
    assert " fastapi>=" not in cmd
    # Installs must target the resolved venv, or uv reports "No virtual environment found".
    assert cmd.startswith('VIRTUAL_ENV="/workspace/.sonar-venv"')


def test_provision_recipes_target_resolved_environment(monkeypatch: pytest.MonkeyPatch):
    """Recipes must create the venv, install into it, and launch daemons from its interpreter."""
    from app.workers.harnesses.provisioner import (
        get_default_provision_recipe,
        parse_sandbox_probe,
        resolve_sandbox_runtime_profile,
    )

    monkeypatch.delenv("SANDBOX_VENV_PATH", raising=False)
    monkeypatch.delenv("SANDBOX_NPM_PREFIX", raising=False)
    profile = resolve_sandbox_runtime_profile(parse_sandbox_probe(_UNPRIVILEGED_PROBE))

    horizon = get_default_provision_recipe("horizon", 8081, profile)
    bootstrap, install, daemon = horizon["commands"]
    assert "uv venv" in bootstrap
    assert install.startswith('VIRTUAL_ENV="/workspace/.sonar-venv"')
    # `uv run` builds a throwaway environment where `horizon` is not importable.
    assert "uv run" not in daemon
    assert "/workspace/.sonar-venv/bin/python" in daemon
    assert "uvicorn" in daemon
    assert "horizon.fast_api_app:app" in daemon

    # Claude uses npm prefix redirection; Antigravity uses the official installer.
    claude_recipe = get_default_provision_recipe("claude", 8081, profile)
    assert 'npm config set prefix "/home/sandbox/.local"' in claude_recipe["commands"][0]
    assert "/home/sandbox/.local/bin" in claude_recipe["health_probe"]

    agy_recipe = get_default_provision_recipe("antigravity", 8081, profile)
    assert "https://antigravity.google/cli/install.sh" in agy_recipe["commands"][0]
    assert "/home/sandbox/.local/bin" in agy_recipe["health_probe"]
    assert "agy" in agy_recipe["health_probe"]


@pytest.mark.asyncio
async def test_sandbox_remote_git_sync_and_parallel_repo_locks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Verify that SandboxWorker synchronizes remote git refs, serializes per-repo fetches via locks, and derives sandbox diffs."""
    from app.workers.sandbox import SandboxWorker
    from app.workers.harnesses.base import SandboxContext, collect_worktree_changes

    worker = SandboxWorker()
    # 1. Distinct repos get distinct locks; same repo shares identical lock
    lock_a1 = worker._get_repo_lock("adk-samples")
    lock_a2 = worker._get_repo_lock("adk-samples")
    lock_b = worker._get_repo_lock("agents-cli")
    assert lock_a1 is lock_a2
    assert lock_a1 is not lock_b

    # 2. Mock transport capturing /exec commands
    recorded_commands: list[str] = []

    def _transport_handler(request):
        import json
        body = json.loads(request.content.decode("utf-8"))
        cmd = body.get("command", "")
        recorded_commands.append(cmd)
        if "diff --numstat" in cmd:
            return httpx.Response(
                200,
                json={
                    "exit_code": 0,
                    "stdout": " M src/payment.py\n---NUMSTAT---\n5\t1\tsrc/payment.py\n---DIFF---\n+new payment code\n",
                    "stderr": "",
                },
            )
        return httpx.Response(200, json={"exit_code": 0, "stdout": "", "stderr": ""})

    import httpx
    mock_transport = httpx.MockTransport(_transport_handler)
    ctx = SandboxContext(
        sandbox_name="voice-worker-test",
        lb_host="mock-lb.aiplatform.googleapis.com",
        http_transport=mock_transport,
    )

    # 3. Running sync executes git fetch & worktree creation
    await worker._sync_remote_repository_in_sandbox(
        ctx,
        repo="adk-samples",
        task_id="task-parallel-1",
        branch_name="agent/task-parallel-1",
    )
    assert any("worktree" in cmd for cmd in recorded_commands)

    # 4. collect_worktree_changes derives sandbox diffs via /exec
    files, summary, raw_diff = await collect_worktree_changes(
        "/workspace/.worktrees/task-parallel-1",
        context=ctx,
    )
    assert files == ["src/payment.py"]
    assert "1 file(s) changed (+5 -1)" in summary
    assert "+new payment code" in raw_diff


