"""Unit and integration tests for configurable sandbox coding harnesses, worktree isolation, and CUJ 1-3 flows."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import httpx
import pytest

from app.agent import (
    approve_task,
    dispatch_task,
    get_task_result,
    set_coding_harness,
)
from app.tasks import get_task_registry, reset_task_registry
from app.workers import (
    HorizonA2AHarness,
    SandboxContext,
    SandboxWorker,
    get_harness_registry,
    get_sandbox_provisioner,
    reset_harness_registry,
    reset_sandbox_provisioner,
    reset_worker_backend,
)
from tests.fakes import (
    exec_transport,
    install_worker,
    register_fake_harnesses,
    sandbox_connection,
    sandbox_transport,
)


@pytest.fixture(autouse=True)
def _clean_singletons():
    reset_task_registry()
    reset_harness_registry()
    reset_sandbox_provisioner()
    reset_worker_backend()
    yield
    reset_task_registry()
    reset_harness_registry()
    reset_sandbox_provisioner()
    reset_worker_backend()


def test_harness_registry_and_commands():
    reg = get_harness_registry()
    assert reg.default_harness.name == "horizon"

    assert reg.get("claude-code").name == "claude"
    assert reg.get("long-horizon").name == "horizon"
    assert reg.get("adk-horizon").name == "horizon"
    assert reg.get("agy").name == "antigravity"
    assert reg.get("gemini").name == "antigravity"

    claude_cmd = reg.get("claude").build_command(
        goal="Fix bug", repo="my-repo", task_id="task-1"
    )
    assert claude_cmd[:5] == [
        "claude",
        "-p",
        "Fix bug",
        "--output-format",
        "stream-json",
    ]

    agy_cmd = reg.get("antigravity").build_command(
        goal="Add tests", repo="my-repo", task_id="task-2"
    )
    assert agy_cmd == [
        "agy",
        "-p",
        "Add tests",
        "--yolo",
        "--output-format",
        "stream-json",
    ]

    horizon_cmd = reg.get("horizon").build_command(
        goal="Refactor", repo="my-repo", task_id="task-3"
    )
    assert horizon_cmd == [
        "adk",
        "api_server",
        "--a2a",
        "--port",
        "8081",
        "horizon",
    ]


@pytest.mark.asyncio
async def test_horizon_remote_a2a_agent_inside_sandbox():
    """Verify HorizonA2AHarness drives ADK's RemoteA2aAgent into sandbox port 8081 with routing headers."""
    captured_requests: list[httpx.Request] = []

    async def custom_a2a_handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        assert request.headers.get("X-Sandbox-Port") == "8081"
        assert request.headers.get("X-Sandbox-Routing-Token") == "sandbox-route-xyz"
        assert request.headers.get("Authorization") == "Bearer sandbox-jwt-token"

        if request.url.path.endswith("agent-card.json"):
            return httpx.Response(
                200,
                json={
                    "name": "horizon_sandbox_agent",
                    "description": "In-sandbox Horizon A2A server",
                    "url": "https://sandbox.aiplatform.googleapis.com/a2a/horizon",
                    "version": "1.0.0",
                    "capabilities": {},
                    "defaultInputModes": ["text/plain"],
                    "defaultOutputModes": ["application/json"],
                    "skills": [
                        {
                            "id": "coding",
                            "name": "Coding",
                            "description": "Sandbox coding",
                            "tags": ["code"],
                        }
                    ],
                },
            )

        body = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body.get("id", "1"),
                "result": {
                    "kind": "task",
                    "id": "task-99",
                    "contextId": "ctx-99",
                    "status": {
                        "state": "completed",
                        "message": {
                            "kind": "message",
                            "messageId": "msg-99",
                            "role": "agent",
                            "parts": [
                                {
                                    "kind": "text",
                                    "text": "Horizon finished migrating database schema inside sandbox.",
                                }
                            ],
                        },
                    },
                },
            },
        )

    harness = HorizonA2AHarness(
        http_transport=httpx.MockTransport(custom_a2a_handler),
    )
    ctx = SandboxContext(
        user_id="alice",
        sandbox_name="voice-worker-alice",
        lb_host="sandbox.aiplatform.googleapis.com",
        routing_token="sandbox-route-xyz",
        sandbox_token="sandbox-jwt-token",
        a2a_port=8081,
    )

    streamed_events = []
    result = await harness.execute_in_sandbox(
        goal="Migrate DB schema",
        repo="backend-service",
        task_id="task-99",
        context=ctx,
        on_event=lambda ev: streamed_events.append(ev),
    )

    assert result.exit_code == 0
    assert result.harness == "horizon"
    assert "Horizon finished migrating database schema inside sandbox." in result.summary
    assert len(captured_requests) >= 2
    assert any("Horizon finished migrating" in e.message for e in streamed_events)


@pytest.mark.asyncio
async def test_non_blocking_multi_harness_and_worktree_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Validate 1-to-many concurrent tasks (including two Claude instances + one Horizon) on the SAME repo use isolated git worktrees."""
    monkeypatch.setenv("ORCHESTRATOR_WORKSPACE_ROOT", str(tmp_path))

    # Stand in for the harnesses themselves; this test is about concurrency,
    # worktree isolation, and the approval gate, not harness internals.
    register_fake_harnesses(
        "claude",
        "horizon",
        "antigravity",
        summary="Fake harness finished the task.",
        writes={"generated_change.py": "# written by the fake harness\n"},
    )
    install_worker(
        monkeypatch,
        SandboxWorker(
            connection=sandbox_connection(),
            http_transport=sandbox_transport(exec_exit_code=0),
        ),
    )

    # Dispatch 3 concurrent agents on the SAME repository ('shared-api'):
    # two Claude Code instances + one ADK Long Horizon instance
    t0 = time.monotonic()
    r1 = await dispatch_task(goal="Implement OAuth", repo="shared-api", harness="claude", task_id="task-1")
    r2 = await dispatch_task(goal="Add rate limiter", repo="shared-api", harness="claude", task_id="task-2")
    r3 = await dispatch_task(
        goal="Optimize SQL queries",
        repo="shared-api",
        harness="horizon",
        require_approval=True,
        task_id="task-3",
    )
    dispatch_elapsed = time.monotonic() - t0

    assert dispatch_elapsed < 0.1
    assert "task-1" in r1 and "branch agent/task-1" in r1
    assert "task-2" in r2 and "branch agent/task-2" in r2
    assert "task-3" in r3 and "branch agent/task-3" in r3

    # Wait for all 3 background tasks to reach completion / approval gate
    task_reg = get_task_registry()
    h1 = await task_reg.get("task-1")
    h2 = await task_reg.get("task-2")
    h3 = await task_reg.get("task-3")
    assert h1 and h2 and h3
    await asyncio.gather(h1.async_task, h2.async_task, h3.async_task)

    # Verify each task ran in its own isolated git worktree directory and branch
    assert h1.branch == "agent/task-1" and h1.worktree_path and Path(h1.worktree_path).exists()
    assert h2.branch == "agent/task-2" and h2.worktree_path and Path(h2.worktree_path).exists()
    assert h3.branch == "agent/task-3" and h3.worktree_path and Path(h3.worktree_path).exists()
    assert h1.worktree_path != h2.worktree_path != h3.worktree_path

    # Each worktree received its own real copy of the harness's edit
    for handle in (h1, h2, h3):
        assert (Path(handle.worktree_path) / "generated_change.py").exists()

    # Verify task-3 paused in CUJ 3 'awaiting_approval' with a real change summary
    assert h3.status == "awaiting_approval"
    assert h3.diff_summary and "1 file(s) changed" in h3.diff_summary
    assert h3.pending_action and "agent/task-3" in h3.pending_action
    res3 = await get_task_result("task-3")
    assert "HITL Approval Gate" in res3

    # Approve task-3 via voice tool (CUJ 3 sign-off)
    approval_msg = await approve_task("task-3")
    assert "Approved task-3" in approval_msg
    assert h3.status == "completed"
    assert h3.approved_at is not None


@pytest.mark.asyncio
async def test_sandbox_provisioner_github_upgrade_and_preflight_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Verify SandboxProvisioner pulls latest Horizon from GitHub, warms up on switch, and blocks tasks if provisioning fails."""
    monkeypatch.setenv("ORCHESTRATOR_WORKSPACE_ROOT", str(tmp_path))
    provisioner = get_sandbox_provisioner()

    # 1. Initially unprovisioned
    h_status = provisioner.get_status("horizon")
    assert h_status.status == "unprovisioned"
    assert h_status.version == "git:main@latest"

    # 2. Provisioning runs the latest GitHub package spec and starts the A2A daemon.
    #    `sandbox_transport` answers both the /exec install commands and the
    #    agent-card health probe Horizon uses (execution_mode == "sandbox_a2a").
    horizon = get_harness_registry().get("horizon")
    ready_ctx = SandboxContext(http_transport=sandbox_transport(exec_exit_code=0))
    state = await provisioner.ensure_provisioned(horizon, ready_ctx)

    assert state.status == "ready"
    assert any(
        "git+https://github.com/google/adk-samples.git#subdirectory=core/python/long-horizon-harness"
        in cmd
        for cmd in state.commands_executed
    )
    assert any(
        "LHA_ENVIRONMENT_BACKEND=local" in cmd and "horizon.fast_api_app:app" in cmd
        for cmd in state.commands_executed
    )

    # 3. Switching the default harness still triggers an eager background warm-up
    switch_msg = await set_coding_harness("horizon")
    assert "ADK Long Horizon" in switch_msg

    # 4. A failing provisioning command must block task execution at the preflight gate
    reset_sandbox_provisioner()
    provisioner = get_sandbox_provisioner()

    worker = SandboxWorker(
        connection=sandbox_connection(),
        http_transport=sandbox_transport(
            exec_exit_code=1,
            exec_stderr="GitHub repository unreachable (HTTP 503)",
        ),
    )
    blocked_res = await worker.execute_task(
        goal="Refactor auth service",
        repo="auth-svc",
        task_id="task-blocked",
        harness="horizon",
    )
    assert blocked_res.exit_code == 1
    assert blocked_res.error is not None
    assert "GitHub repository unreachable" in blocked_res.error
    assert provisioner.get_status("horizon", "voice-worker-test").status == "error"

    # 5. An unreachable sandbox (502 / "No healthy upstream") must also block, not pass
    reset_sandbox_provisioner()
    dead_worker = SandboxWorker(
        connection=sandbox_connection(),
        http_transport=exec_transport(status_code=502),
    )
    dead_res = await dead_worker.execute_task(
        goal="Refactor auth service",
        repo="auth-svc",
        task_id="task-dead",
        harness="claude",
    )
    assert dead_res.exit_code != 0
    assert dead_res.error is not None


def test_headless_2_phase_invocations_and_stream_json_parsing():
    """Verify separate harness prompts, --json-schema plan invocations, --resume/--conversation execute invocations, and dual NDJSON parsing."""
    from app.workers.harnesses import (
        PLAN_OUTPUT_SCHEMA_JSON,
        extract_plan_from_json_output,
        parse_stream_json_line,
    )

    reg = get_harness_registry()
    claude = reg.get("claude")
    agy = reg.get("antigravity")

    # 1. Phase 1 (mode='plan') enforces --json-schema and read-only permission flags
    claude_plan_cmd = claude.build_headless_invocation(
        goal="Add Redis token store",
        repo="auth-svc",
        task_id="task-10",
        mode="plan",
        worktree_path="/workspace/.worktrees/task-10",
        branch="agent/task-10",
    )
    assert "--bare" in claude_plan_cmd
    assert "--permission-mode" in claude_plan_cmd
    assert "plan" in claude_plan_cmd
    assert "--json-schema" in claude_plan_cmd
    assert PLAN_OUTPUT_SCHEMA_JSON in claude_plan_cmd

    agy_plan_cmd = agy.build_headless_invocation(
        goal="Add Redis token store",
        repo="auth-svc",
        task_id="task-10",
        mode="plan",
        worktree_path="/workspace/.worktrees/task-10",
        branch="agent/task-10",
    )
    assert "--mode" in agy_plan_cmd
    assert "plan" in agy_plan_cmd
    assert "--dangerously-skip-permissions" in agy_plan_cmd
    assert "--output-format" in agy_plan_cmd
    assert "json" in agy_plan_cmd
    assert "--json-schema" in agy_plan_cmd

    # 2. Phase 2 (mode='execute') resumes captured session/conversation ID and enables unattended permissions
    claude_exec_cmd = claude.build_headless_invocation(
        goal="Proceed with Redis TTL",
        repo="auth-svc",
        task_id="task-10",
        mode="execute",
        session_id="claude-sess-xyz",
    )
    assert "--resume" in claude_exec_cmd
    assert "claude-sess-xyz" in claude_exec_cmd
    assert "acceptEdits" in claude_exec_cmd
    assert "--permission-prompts" in claude_exec_cmd

    agy_exec_cmd = agy.build_headless_invocation(
        goal="Proceed with Redis TTL",
        repo="auth-svc",
        task_id="task-10",
        mode="execute",
        session_id="055a398f-db14-4c5f-abbb-1bf03f8120a7",
    )
    assert "--conversation" in agy_exec_cmd
    assert "055a398f-db14-4c5f-abbb-1bf03f8120a7" in agy_exec_cmd
    assert "--dangerously-skip-permissions" in agy_exec_cmd
    assert "stream-json" in agy_exec_cmd

    # 3. Extract structured_output and conversation_id from Antigravity JSON schema envelope
    agy_json_envelope = json.dumps(
        {
            "conversation_id": "4e502687-290c-4030-b908-5ed6c68fa5dc",
            "status": "SUCCESS",
            "structured_output": {
                "plan_summary": "Introduce RedisTokenStore with rotating JTIs.",
                "steps": ["Add RedisTokenStore", "Add unit tests"],
                "questions": ["Should TTL default to 15 minutes or 1 hour?"],
                "files_to_modify": ["src/auth/jwt.py"],
            },
        }
    )
    parsed = extract_plan_from_json_output(
        agy_json_envelope,
        fallback_session_id="fallback-id",
    )
    assert parsed is not None
    sess_id, summary, questions, files = parsed
    assert sess_id == "4e502687-290c-4030-b908-5ed6c68fa5dc"
    assert summary == "Introduce RedisTokenStore with rotating JTIs."
    assert questions == ["Should TTL default to 15 minutes or 1 hour?"]
    assert files == ["src/auth/jwt.py"]

    # 3b. Unusable harness output must report failure, never invent a plan.
    for unusable in ("", "   ", "not json at all", "Traceback (most recent call last):"):
        assert (
            extract_plan_from_json_output(unusable, fallback_session_id="fallback-id")
            is None
        ), f"expected None for unusable stdout {unusable!r}"

    # A valid envelope with no questions must report zero questions, not defaults.
    no_questions = json.dumps(
        {
            "session_id": "sess-1",
            "structured_output": {
                "plan_summary": "Rename the module.",
                "steps": ["Rename"],
            },
        }
    )
    parsed_nq = extract_plan_from_json_output(
        no_questions, fallback_session_id="fallback-id"
    )
    assert parsed_nq is not None
    assert parsed_nq[2] == []

    # 4. Parse real Antigravity (`event`) and Claude (`type`) stream-json lines
    agy_tool_line = json.dumps(
        {
            "event": "step_update",
            "step_update": {
                "conversation_id": "edb1c8c1",
                "step_index": 4,
                "state": "DONE",
                "step_type": "tool",
                "tool_name": "run_command",
            },
        }
    )
    ev_tool = parse_stream_json_line(agy_tool_line, "task-10", "antigravity")
    assert ev_tool is not None
    assert ev_tool.kind == "progress"
    assert "Running run_command" in ev_tool.message
    assert ev_tool.metadata.get("session_id") == "edb1c8c1"

    agy_res_line = json.dumps(
        {
            "event": "result",
            "result": {
                "conversation_id": "edb1c8c1",
                "status": "SUCCESS",
                "response": "All tests passed.",
            },
        }
    )
    ev_res = parse_stream_json_line(agy_res_line, "task-10", "antigravity")
    assert ev_res is not None
    assert ev_res.kind == "completed"
    assert ev_res.message == "All tests passed."


@pytest.mark.asyncio
async def test_declarative_sandbox_seeding_and_cross_harness_worktree_transition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Verify `provision_shared_sandbox` seeds repos/context and switching harnesses mid-task preserves the exact same worktree and files."""
    from app.agent import steer_task
    from scripts.provision_sandbox import provision_shared_sandbox

    monkeypatch.setenv("ORCHESTRATOR_WORKSPACE_ROOT", str(tmp_path))

    fakes = register_fake_harnesses(
        "claude",
        "antigravity",
        questions=["Redis Token Store or in-process cache?"],
        summary="Applied the approved plan.",
        writes={"task_1_output.py": "# applied by the fake harness\n"},
    )
    fakes["claude"].display_name = "Claude Code"
    fakes["antigravity"].display_name = "Antigravity"
    install_worker(
        monkeypatch,
        SandboxWorker(
            connection=sandbox_connection(),
            http_transport=sandbox_transport(exec_exit_code=0),
        ),
    )

    # 1. Provision shared sandbox and seed declarative repositories
    summary = await provision_shared_sandbox(
        user_id="dev_test",
        workspace_root=tmp_path,
        http_transport=sandbox_transport(exec_exit_code=0),
    )
    assert "auth-svc" in summary["seeded_repositories"]
    assert "analytics-svc" in summary["seeded_repositories"]
    assert (tmp_path / "auth-svc" / "src" / "auth" / "jwt.py").exists()
    assert (tmp_path / "auth-svc" / "AGENTS.md").exists()
    assert (tmp_path / "auth-svc" / "CLAUDE.md").exists()
    assert (tmp_path / "auth-svc" / "GEMINI.md").exists()

    # 2. Start task-1 in Claude Code (`mode="plan"`) on `auth-svc`
    await dispatch_task(
        goal="Add rotating refresh tokens",
        repo="auth-svc",
        mode="plan",
        harness="claude",
        task_id="task-1",
    )
    task_reg = get_task_registry()
    h1 = await task_reg.get("task-1")
    assert h1 is not None
    await h1.async_task

    assert h1.status == "awaiting_input"
    assert h1.harness == "claude"
    claude_worktree = h1.worktree_path
    claude_branch = h1.branch
    assert claude_worktree is not None and Path(claude_worktree).exists()
    assert (Path(claude_worktree) / "src" / "auth" / "jwt.py").exists()
    assert (Path(claude_worktree) / "GEMINI.md").exists()

    # 3. Switch from Claude Code to Antigravity on the SAME task (`task-1`) and execute
    steer_msg = await steer_task(
        task_id="task-1",
        instruction="Proceed with Redis Token Store and 15m TTL",
        mode="execute",
        harness="antigravity",
    )
    assert "Switched task-1 from claude to Antigravity" in steer_msg
    await h1.async_task

    # Verify Antigravity executed on the exact same git worktree and branch as Claude Code
    assert h1.status == "completed"
    assert h1.harness == "antigravity"
    assert h1.worktree_path == claude_worktree
    assert h1.branch == claude_branch
    assert (Path(h1.worktree_path) / "task_1_output.py").exists()


def test_onboarding_wizard_and_local_fork_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Verify agent/human onboarding wizard writes `workspaces.local.yaml`, rewrites forks + upstream URLs, and updates Horizon spec."""
    from app.workers.harnesses.base import load_workspaces_manifest
    from app.workers.harnesses.provisioner import get_default_provision_recipe
    from scripts.provision_sandbox import run_onboarding_wizard

    local_yaml = tmp_path / "workspaces.local.yaml"
    monkeypatch.setenv("ORCHESTRATOR_WORKSPACES_LOCAL_YAML", str(local_yaml))

    res = run_onboarding_wizard(
        interactive=False,
        github_user="dev-alice",
        prefer_forks=True,
        default_harness="horizon",
        add_repos=["adk-python=https://github.com/dev-alice/adk-python.git@feat/live"],
        local_override_path=local_yaml,
    )
    assert local_yaml.exists()
    assert res["github_user"] == "dev-alice"
    assert res["prefer_forks"] is True
    assert get_harness_registry().default_harness.name == "horizon"

    merged = load_workspaces_manifest(local_override_path=local_yaml)
    repos_by_name = {r["name"]: r for r in merged["repositories"]}

    # 1. Canonical google/adk-samples was rewritten to dev-alice's fork as origin + google as upstream
    assert repos_by_name["adk-samples"]["git_url"] == "https://github.com/dev-alice/adk-samples.git"
    assert repos_by_name["adk-samples"]["upstream_url"] == "https://github.com/google/adk-samples.git"

    # 2. Extra repo was appended cleanly
    assert repos_by_name["adk-python"]["git_url"] == "https://github.com/dev-alice/adk-python.git"
    assert repos_by_name["adk-python"]["branch"] == "feat/live"

    # 3. Horizon sandbox recipe pulls directly from dev-alice's adk-samples fork
    recipe = get_default_provision_recipe("horizon")
    assert "github.com/dev-alice/adk-samples.git" in recipe["source_url"]


@pytest.mark.asyncio
async def test_sandbox_skills_gcp_auth_and_iterative_plan_steering(
    monkeypatch: pytest.MonkeyPatch,
):
    """Verify sandbox skills manifest, GCP credential injection, plan steps extraction, and multi-turn mode='plan' steering."""
    from app.agent import steer_task
    from app.auth import get_sandbox_gcp_env
    from app.workers.harnesses.base import (
        SandboxContext,
        launch_detached_command,
        load_workspaces_manifest,
    )
    from app.workers.harnesses.prompts import extract_plan_steps_from_json_output
    from app.workers.harnesses.provisioner import SandboxRuntimeProfile

    # 1. Verify sandbox skills in workspaces.yaml and SandboxRuntimeProfile helpers
    manifest = load_workspaces_manifest()
    skills = manifest.get("sandbox", {}).get("skills", [])
    assert any("vercel-labs/skills --skill find-skills" in s for s in skills)
    assert any("obra/superpowers" in s for s in skills)
    assert any("mattpocock/skills" in s for s in skills)

    prof = SandboxRuntimeProfile()
    assert "agents-cli setup --skip-auth --agent all" in prof.agents_cli_setup_command()
    assert (
        "npx -y skills add vercel-labs/skills --skill find-skills -g -a '*' -y"
        in prof.skills_install("vercel-labs/skills --skill find-skills")
    )

    # 2. Verify GCP auth bridge injects project and token into harness env and detached commands
    monkeypatch.setenv("SANDBOX_GCP_PROJECT", "sonar-prod-123")
    monkeypatch.setenv("SANDBOX_GCP_ACCESS_TOKEN", "ya29.test-sandbox-token")
    gcp_env = get_sandbox_gcp_env()
    assert gcp_env["GOOGLE_CLOUD_PROJECT"] == "sonar-prod-123"
    assert gcp_env["CLOUDSDK_CORE_PROJECT"] == "sonar-prod-123"
    assert gcp_env["CLOUDSDK_AUTH_ACCESS_TOKEN"] == "ya29.test-sandbox-token"

    captured_reqs: list[httpx.Request] = []
    ctx = SandboxContext(
        http_transport=exec_transport(exit_code=0, requests=captured_reqs)
    )
    await launch_detached_command(
        ctx,
        "agents-cli deploy",
        run_id="run-gcp-1",
        worktree_path="/workspace/auth-svc",
    )
    assert len(captured_reqs) == 1
    sent_payload = json.loads(captured_reqs[0].content)
    assert sent_payload["env"]["GOOGLE_CLOUD_PROJECT"] == "sonar-prod-123"
    assert sent_payload["env"]["CLOUDSDK_AUTH_ACCESS_TOKEN"] == "ya29.test-sandbox-token"
    assert "/workspace/.sonar/gcp_adc.json" in sent_payload["command"]

    # 3. Verify plan steps extraction and iterative mode='plan' steering before mode='execute'
    raw_plan = json.dumps(
        {
            "session_id": "sess-iter-1",
            "structured_output": {
                "plan_summary": "Compare Redis vs Postgres for session state.",
                "steps": ["Inspect current session store", "Benchmark Redis TTL"],
                "questions": ["Should we keep Postgres as a fallback?"],
                "files_to_modify": ["app/store.py"],
            },
        }
    )
    assert extract_plan_steps_from_json_output(raw_plan) == [
        "Inspect current session store",
        "Benchmark Redis TTL",
    ]

    register_fake_harnesses(
        "claude",
        questions=["What TTL should we use for the revised Redis plan?"],
        summary="Revised Plan v2 with Redis primary and Postgres fallback.",
    )
    install_worker(
        monkeypatch,
        SandboxWorker(
            connection=sandbox_connection(),
            http_transport=sandbox_transport(exec_exit_code=0),
        ),
    )
    await dispatch_task(goal="Design session store", repo="auth-svc", mode="plan", harness="claude", task_id="task-1")
    h = await get_task_registry().get("task-1")
    assert h is not None
    await h.async_task
    assert h.status == "awaiting_input"

    # Iterate on the plan in mode='plan' (Plan v1 -> Plan v2)
    steer_plan_msg = await steer_task(
        task_id="task-1",
        instruction="Keep Postgres as fallback and revise the plan steps",
        mode="plan",
    )
    assert "in plan mode" in steer_plan_msg
    await h.async_task
    assert h.status == "awaiting_input"
    assert h.mode == "plan"


def test_github_token_propagated_across_all_three_harnesses(
    monkeypatch: pytest.MonkeyPatch,
):
    """Verify GH_TOKEN and GITHUB_PERSONAL_ACCESS_TOKEN are propagated across Claude, Antigravity, and Horizon harnesses."""
    from app.tools.integration_tools import _resolve_github_owner_and_repo
    from app.workers.harnesses import (
        AntigravityHarness,
        ClaudeCodeHarness,
        HorizonA2AHarness,
    )

    monkeypatch.setenv("GITHUB_PERSONAL_ACCESS_TOKEN", "ghp_test_token_all_3")
    monkeypatch.delenv("GH_TOKEN", raising=False)

    for harness in (ClaudeCodeHarness(), AntigravityHarness(), HorizonA2AHarness()):
        env = harness.build_env()
        assert env.get("GH_TOKEN") == "ghp_test_token_all_3"
        assert env.get("GITHUB_PERSONAL_ACCESS_TOKEN") == "ghp_test_token_all_3"

    short_name, fork_slug, upstream_slug = _resolve_github_owner_and_repo(
        "agents-cli", "demo-user"
    )
    assert short_name == "agents-cli"
    assert fork_slug == "demo-user/agents-cli"
    assert upstream_slug == "google/agents-cli"





