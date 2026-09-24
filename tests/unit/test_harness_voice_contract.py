"""L2 contract fidelity tests between coding harnesses, TaskRegistry, and voice task tools.

Covers the F1-F9 correctness invariants from the sandbox evaluation plan:
- F1: Unparseable or empty plan output returns None (never fabricated questions) and fails closed.
- F2: approve_task delegates to WorkerBackend.commit_and_push_task and reports actual commit/push state.
- F3: get_task_result bounds raw_diff for the voice model while preserving handle.raw_diff for A2UI.
- F6: require_approval=True persists across dispatch_task(mode='plan') -> steer_task(mode='execute').
- F7: ANTIGRAVITY_MODEL and LHA_ROOT_MODEL pin models on Antigravity and Horizon harnesses.
- F8: Unrecognized mode values fail closed in dispatch_task and steer_task instead of coercing to 'execute'.
- F9: Non-zero git exit in _collect_sandbox_worktree_changes surfaces an error in diff_summary.
"""

from __future__ import annotations

import json
import pytest

from app.tasks import get_task_registry
from app.tools.task_tools import (
    _VOICE_DIFF_CHAR_BUDGET,
    approve_task,
    dispatch_task,
    get_task_result,
    steer_task,
)
from app.workers.base import ApprovalCommitResult, WorkerExecutionResult
from app.workers.harnesses.antigravity import AntigravityHarness
from app.workers.harnesses.base import (
    SandboxContext,
    _collect_sandbox_worktree_changes,
)
from app.workers.harnesses.claude import ClaudeCodeHarness
from app.workers.harnesses.horizon import HorizonA2AHarness
from app.workers.harnesses.prompts import extract_plan_from_json_output
from tests.fakes import exec_transport


@pytest.mark.asyncio
async def test_f1_plan_extractor_and_harnesses_never_fabricate_on_unparseable_stdout() -> None:
    """F1: Empty or non-JSON stdout must return None and fail the harness run (exit_code=1)."""
    assert extract_plan_from_json_output("", fallback_session_id="s1") is None
    assert extract_plan_from_json_output("plain text banner", fallback_session_id="s1") is None

    ctx = SandboxContext(
        http_transport=exec_transport(stdout="not valid json", stderr="", exit_code=0)
    )
    claude_res = await ClaudeCodeHarness().execute_in_sandbox(
        goal="Refactor token store",
        repo="auth-svc",
        task_id="task-f1-claude",
        context=ctx,
        mode="plan",
    )
    assert claude_res.exit_code == 1
    assert claude_res.error is not None
    assert "no parseable plan" in claude_res.error.lower()
    assert claude_res.questions == []

    agy_res = await AntigravityHarness().execute_in_sandbox(
        goal="Refactor token store",
        repo="auth-svc",
        task_id="task-f1-agy",
        context=ctx,
        mode="plan",
    )
    assert agy_res.exit_code == 1
    assert agy_res.error is not None
    assert "no parseable plan" in agy_res.error.lower()
    assert agy_res.questions == []


@pytest.mark.asyncio
async def test_f2_approve_task_reports_honest_commit_and_push_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """F2: approve_task must report what WorkerBackend.commit_and_push_task actually did."""
    import app.workers.factory as factory_mod

    registry = get_task_registry()
    registry._tasks.clear()

    class _CommitOnlyBackend:
        async def execute_task(self, **kwargs):
            return WorkerExecutionResult(
                exit_code=0,
                summary="Implemented feature.",
                awaiting_approval=True,
                pending_action="git commit and push",
                branch="agent/task-1",
                worktree_path="/workspace/.worktrees/task-1",
            )

        async def cancel_task(self, task_id: str) -> bool:
            return True

        async def commit_and_push_task(self, **kwargs) -> ApprovalCommitResult:
            return ApprovalCommitResult(
                committed=True,
                pushed=False,
                detail="No remote origin configured.",
            )

    monkeypatch.setattr(factory_mod, "_active_worker", _CommitOnlyBackend())

    await dispatch_task(
        goal="Add feature",
        repo="auth-svc",
        mode="execute",
        harness="claude",
        require_approval=True,
        task_id="task-1",
    )
    handle = await registry.get("task-1")
    assert handle is not None
    assert handle.async_task is not None
    await handle.async_task

    spoken = await approve_task("task-1")
    assert "committed branch" in spoken.lower()
    assert "push did not succeed" in spoken.lower()
    assert "No remote origin configured." in spoken


@pytest.mark.asyncio
async def test_f3_get_task_result_bounds_raw_diff_for_voice_context() -> None:
    """F3: get_task_result must truncate huge diffs for the voice model while keeping handle.raw_diff intact."""
    registry = get_task_registry()
    registry._tasks.clear()

    huge_diff = "diff --git a/src/auth/jwt.py b/src/auth/jwt.py\n" + ("+line = 1\n" * 500)

    async def _coro(tid: str, on_ev):
        return WorkerExecutionResult(
            exit_code=0,
            summary="Updated JWT module.",
            files_changed=["src/auth/jwt.py"],
            diff_summary="1 file changed (+500 -0).",
            raw_diff=huge_diff,
        )

    task_id, handle = await registry.register(
        goal="Large refactor",
        repo="auth-svc",
        task_coro_fn=_coro,
    )
    assert handle.async_task is not None
    await handle.async_task

    # Full raw_diff stays intact on the handle for the screen card
    assert len(handle.raw_diff or "") == len(huge_diff)

    # Voice payload is bounded
    voice_text = await get_task_result(task_id)
    assert "diff truncated for voice context" in voice_text
    assert len(voice_text) < _VOICE_DIFF_CHAR_BUDGET + 600


@pytest.mark.asyncio
async def test_f6_require_approval_survives_plan_to_execute_steer(monkeypatch: pytest.MonkeyPatch) -> None:
    """F6: A task dispatched with require_approval=True in plan mode keeps require_approval=True on steer_task."""
    import app.workers.factory as factory_mod

    registry = get_task_registry()
    registry._tasks.clear()
    registry._next_id = 1
    captured_calls: list[dict] = []

    class _RecordingBackend:
        async def execute_task(self, **kwargs):
            captured_calls.append(dict(kwargs))
            if kwargs.get("mode") == "plan":
                return WorkerExecutionResult(
                    exit_code=0,
                    summary="Plan ready.",
                    questions=["Use Redis or memory?"],
                    awaiting_input=True,
                )
            return WorkerExecutionResult(
                exit_code=0,
                summary="Executed with approval gate.",
                awaiting_approval=bool(kwargs.get("require_approval")),
                pending_action="git commit and push" if kwargs.get("require_approval") else None,
            )

        async def cancel_task(self, task_id: str) -> bool:
            return True

        async def commit_and_push_task(self, **kwargs) -> ApprovalCommitResult:
            return ApprovalCommitResult(committed=True, pushed=True)

    monkeypatch.setattr(factory_mod, "_active_worker", _RecordingBackend())

    await dispatch_task(
        goal="Add rotating JTI",
        repo="auth-svc",
        mode="plan",
        harness="claude",
        require_approval=True,
        task_id="task-1",
    )
    h1 = await registry.get("task-1")
    assert h1 is not None and h1.async_task is not None
    await h1.async_task
    assert h1.require_approval is True

    await steer_task(
        task_id="task-1",
        instruction="Use Redis and proceed to implementation.",
        mode="execute",
    )
    h2 = await registry.get("task-1")
    assert h2 is not None and h2.async_task is not None
    await h2.async_task

    assert len(captured_calls) == 2
    assert captured_calls[1]["mode"] == "execute"
    assert captured_calls[1]["require_approval"] is True
    assert h2.status == "awaiting_approval"


@pytest.mark.asyncio
async def test_f4_f10_execute_summary_ignores_trailing_tool_use_and_captures_session_id() -> None:
    """F4/F10: Execute mode must pick the completed result message (not trailing 'Running Bash') and real session_id."""
    stream_lines = "\n".join(
        [
            json.dumps({"type": "system", "subtype": "init", "session_id": "real-claude-sess-42"}),
            json.dumps({"type": "result", "result": "Added issue_rotating_token and verified pytest."}),
            json.dumps({"type": "tool_use", "name": "Bash"}),
        ]
    )
    ctx = SandboxContext(
        http_transport=exec_transport(stdout=stream_lines, stderr="", exit_code=0)
    )
    res = await ClaudeCodeHarness().execute_in_sandbox(
        goal="Implement rotating token",
        repo="auth-svc",
        task_id="task-f4",
        context=ctx,
        mode="execute",
    )
    assert res.exit_code == 0
    assert res.summary == "Added issue_rotating_token and verified pytest."
    assert res.claude_session_id == "real-claude-sess-42"


def test_f7_model_pinning_on_antigravity_and_horizon(monkeypatch: pytest.MonkeyPatch) -> None:
    """F7: ANTIGRAVITY_MODEL passes --model to agy, and LHA_ROOT_MODEL is propagated to Horizon."""
    monkeypatch.setenv("ANTIGRAVITY_MODEL", "Gemini 3.1 Pro (High)")
    agy_cmd = AntigravityHarness().build_headless_invocation(
        goal="Check auth",
        repo="auth-svc",
        task_id="task-f7",
        mode="plan",
    )
    assert "--model" in agy_cmd
    idx = agy_cmd.index("--model")
    assert agy_cmd[idx + 1] == "Gemini 3.1 Pro (High)"

    monkeypatch.setenv("LHA_ROOT_MODEL", "gemini-3.1-pro-preview")
    horizon_env = HorizonA2AHarness().build_env()
    assert horizon_env.get("LHA_ROOT_MODEL") == "gemini-3.1-pro-preview"


@pytest.mark.asyncio
async def test_f8_invalid_mode_fails_closed() -> None:
    """F8: Unknown mode strings must be rejected rather than silently coercing to 'execute'."""
    res_dispatch = await dispatch_task(
        goal="Inspect files",
        repo="auth-svc",
        mode="invalid_mode_name",
    )
    assert "not a valid mode" in res_dispatch

    res_steer = await steer_task(
        task_id="task-1",
        instruction="Continue",
        mode="invalid_mode_name",
    )
    assert "not a valid mode" in res_steer


@pytest.mark.asyncio
async def test_f9_collect_sandbox_worktree_changes_reports_git_failure() -> None:
    """F9: A broken git command in the sandbox must surface in diff_summary rather than returning empty."""
    ctx = SandboxContext(
        http_transport=exec_transport(
            stdout="",
            stderr="fatal: not a git repository",
            exit_code=128,
        )
    )
    files, summary, raw_diff = await _collect_sandbox_worktree_changes(
        ctx, "/workspace/.worktrees/task-f9"
    )
    assert files == []
    assert raw_diff == ""
    assert "git exited 128" in summary
