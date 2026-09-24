"""Hermetic multi-step CUJ sandbox runner and trace generator for `agents-cli eval grade`.

Executes multi-turn CUJ trajectories (`plan` -> `steer(mode='plan')` -> `steer(mode='execute')`
and cross-harness handoffs) inside the Vertex AI Agent Engine Sandbox with strict hermeticity:
1. Sets `ORCHESTRATOR_HERMETIC_SANDBOX=1` so no branch is ever pushed to GitHub.
2. Installs an in-sandbox `agents-cli` audit wrapper shim in `/workspace/.sonar/audit_bin/agents-cli`
   that logs every `agents-cli` invocation (proving whether `agents-cli` was used on greenfield
   agent creation) AND blocks any non-hermetic subcommands (`deploy`, `publish`, `infra`).
3. Inspects the resulting worktree for ADK agent structure (`root_agent` / `pyproject.toml`),
   created files, and `pytest` status, then cleans up the temporary worktree.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex
import sys
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parents[3]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

load_dotenv(ROOT_DIR / ".env")
# Enforce hermetic sandbox execution: never push branches or mutate external state.
os.environ["ORCHESTRATOR_HERMETIC_SANDBOX"] = "1"

from app.workers.base import WorkerExecutionResult
from app.workers.harnesses.base import SandboxContext, exec_in_sandbox
from app.workers.harnesses.prompts import build_cross_harness_handoff
from app.workers.sandbox import SandboxWorker


async def _get_sandbox_context(worker: SandboxWorker) -> SandboxContext:
    sandbox_info = await worker._ensure_sandbox()
    return SandboxContext(
        user_id="default_user",
        sandbox_name=sandbox_info["sandbox_name"],
        lb_host=sandbox_info["lb_host"],
        routing_token=sandbox_info["routing_token"],
        sandbox_token=sandbox_info["sandbox_token"],
    )


async def _install_hermetic_agents_cli_shim(
    worker: SandboxWorker,
    *,
    task_id: str,
    dry_run: bool,
) -> None:
    """Install a hermetic audit wrapper around `agents-cli` inside the sandbox container.

    - Records every `agents-cli` invocation to `/workspace/.sonar/runs/<task_id>/agents_cli_audit.log`
      so evaluations can deterministically verify whether the harness invoked `agents-cli`.
    - Blocks mutating cloud subcommands (`deploy`, `publish`, `infra`) so eval runs have
      zero external consequences even if a model attempts them.
    """
    if dry_run:
        return
    try:
        context = await _get_sandbox_context(worker)
    except Exception:
        return

    audit_log = f"/workspace/.sonar/runs/{task_id}/agents_cli_audit.log"
    setup_script = (
        f"mkdir -p /workspace/.sonar/audit_bin /workspace/.sonar/runs/{shlex.quote(task_id)} && "
        f"rm -f {shlex.quote(audit_log)} && "
        "REAL_BIN=$(PATH=\"/workspace/.sonar-venv/bin:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin\" which agents-cli 2>/dev/null || echo '/workspace/.sonar-venv/bin/agents-cli'); "
        "cat << 'EOF' > /workspace/.sonar/audit_bin/agents-cli\n"
        "#!/bin/sh\n"
        "LOG_FILE=\"${SONAR_AGENTS_CLI_AUDIT_LOG:-/workspace/.sonar/agents_cli_audit.log}\"\n"
        "mkdir -p \"$(dirname \"$LOG_FILE\")\"\n"
        "printf '%s\\n' \"$*\" >> \"$LOG_FILE\"\n"
        "case \" $* \" in\n"
        "  *\" deploy \"*|*\" publish \"*|*\" infra \"*)\n"
        "    echo \"[HERMETIC SANDBOX GUARD] Blocked external cloud mutation: agents-cli $*\" >&2\n"
        "    exit 0\n"
        "    ;;\n"
        "esac\n"
        "for candidate in /workspace/.sonar-venv/bin/agents-cli \"$HOME/.local/bin/agents-cli\" /usr/local/bin/agents-cli; do\n"
        "  if [ -x \"$candidate\" ]; then\n"
        "    exec \"$candidate\" \"$@\"\n"
        "  fi\n"
        "done\n"
        "echo \"agents-cli binary not found\" >&2\n"
        "exit 127\n"
        "EOF\n"
        "chmod +x /workspace/.sonar/audit_bin/agents-cli && "
        "ln -sf /workspace/.sonar/audit_bin/agents-cli \"$HOME/.local/bin/agents-cli\" 2>/dev/null || true"
    )
    await exec_in_sandbox(context, setup_script, timeout_s=20.0)


async def _inspect_and_cleanup_worktree(
    worker: SandboxWorker,
    *,
    task_id: str,
    worktree_path: str,
    dry_run: bool,
    keep_worktree: bool = False,
) -> dict[str, Any]:
    """Verify worktree artifacts (`pytest`, ADK `root_agent`, `agents-cli` audit log) and clean up."""
    if dry_run:
        return {
            "pytest_exit_code": 0,
            "pytest_stdout": "2 passed in 0.10s (dry-run)",
            "agents_cli_invocations": ["scaffold create release-triage --skip-git"],
            "adk_agent_detected": True,
            "workspace_files": ["README.md", "release_triage/agent.py", "deliverable.md"],
        }
    try:
        context = await _get_sandbox_context(worker)
    except Exception as exc:
        return {
            "pytest_exit_code": -1,
            "pytest_stdout": f"Sandbox unavailable: {exc}",
            "agents_cli_invocations": [],
            "adk_agent_detected": False,
            "workspace_files": [],
        }

    audit_log = f"/workspace/.sonar/runs/{task_id}/agents_cli_audit.log"
    probe_cmd = (
        'export PATH="/workspace/.sonar/audit_bin:/workspace/.sonar-venv/bin:$HOME/.local/bin:$PATH"; '
        'PYTEST_BIN="/workspace/.sonar-venv/bin/pytest"; [ -x "$PYTEST_BIN" ] || PYTEST_BIN="pytest"; '
        f"cd {shlex.quote(worktree_path)} 2>/dev/null || exit 0; "
        "git remote remove origin 2>/dev/null || true; "
        "echo '---FILES---'; "
        "find . -maxdepth 3 -not -path '*/.*' -type f | sort; "
        "echo '---AUDIT---'; "
        f"cat {shlex.quote(audit_log)} 2>/dev/null || cat /workspace/.sonar/agents_cli_audit.log 2>/dev/null || true; "
        "echo '---ADK_DETECT---'; "
        "grep -rnE '(root_agent|from google\\.adk|google-adk|agents-cli)' . 2>/dev/null | head -20 || true; "
        "echo '---PYTEST---'; "
        "SUBPROJ=$(find . -maxdepth 2 -mindepth 2 -name pyproject.toml -exec dirname {} \\; | head -1); "
        'if [ -n "$SUBPROJ" ]; then '
        '  (cd "$SUBPROJ" && PYTHONPATH=. "$PYTEST_BIN" -q tests/unit 2>&1 || PYTHONPATH=. "$PYTEST_BIN" -q 2>&1); '
        "else "
        '  PYTHONPATH=. "$PYTEST_BIN" -q 2>&1; '
        "fi; "
        'echo "__PYTEST_EC__=$?"'
    )
    res = await exec_in_sandbox(context, probe_cmd, timeout_s=60.0)
    raw = res.stdout or ""

    def _section(text: str, start_marker: str, end_marker: str | None) -> str:
        if start_marker not in text:
            return ""
        sub = text.split(start_marker, 1)[1]
        if end_marker and end_marker in sub:
            sub = sub.split(end_marker, 1)[0]
        return sub.strip()

    files_block = _section(raw, "---FILES---", "---AUDIT---")
    audit_block = _section(raw, "---AUDIT---", "---ADK_DETECT---")
    adk_block = _section(raw, "---ADK_DETECT---", "---PYTEST---")
    pytest_block = _section(raw, "---PYTEST---", None)

    pytest_ec = 5
    pytest_lines: list[str] = []
    for ln in pytest_block.splitlines():
        if ln.startswith("__PYTEST_EC__="):
            try:
                pytest_ec = int(ln.split("=", 1)[1].strip())
            except ValueError:
                pytest_ec = 1
        else:
            pytest_lines.append(ln)

    workspace_files = [
        f.lstrip("./") for f in files_block.splitlines() if f.strip() and f.strip() != "."
    ]
    invocations = [ln.strip() for ln in audit_block.splitlines() if ln.strip()]
    adk_detected = bool(adk_block.strip()) or any(
        "agent.py" in f or "eval_config.yaml" in f for f in workspace_files
    )

    if not keep_worktree:
        cleanup_cmd = (
            f"rm -rf {shlex.quote(worktree_path)} "
            f"/workspace/.sonar/runs/{shlex.quote(task_id)} "
            "/workspace/.sonar/agents_cli_audit.log 2>/dev/null || true"
        )
        await exec_in_sandbox(context, cleanup_cmd, timeout_s=15.0)

    return {
        "pytest_exit_code": pytest_ec,
        "pytest_stdout": "\n".join(pytest_lines).strip(),
        "agents_cli_invocations": invocations,
        "adk_agent_detected": adk_detected,
        "adk_evidence": adk_block[:600],
        "workspace_files": workspace_files,
    }


def _build_dry_run_step_result(
    case: dict[str, Any],
    step: dict[str, Any],
    step_idx: int,
    harness: str,
    task_id: str,
) -> WorkerExecutionResult:
    mode = step.get("mode", "execute")
    repo = case.get("repo", "default-repo")
    wt_path = f"/workspace/.worktrees/{task_id}"
    branch = f"agent/{task_id}"
    if mode == "plan":
        return WorkerExecutionResult(
            exit_code=0,
            summary=(
                f"Prepared read-only plan (step {step_idx + 1}) for {case['eval_case_id']} in {repo}."
            ),
            response_text=(
                f"Plan step {step_idx + 1}: 1. Inspect workspace and constraints; "
                "2. Outline options and trade-offs for user confirmation."
            ),
            claude_session_id=f"dry-sess-{task_id}",
            questions=["Would you like to proceed with these defaults?"],
            awaiting_input=True,
            files_changed=[],
            branch=branch,
            worktree_path=wt_path,
            harness=harness,
            events=["Running agents-cli --help", "Completed read-only plan."],
        )
    files = (
        ["release_triage/agent.py", "pyproject.toml"]
        if case.get("require_agents_cli")
        else (
            ["menu_and_shopping_list.md"]
            if case.get("persona") == "generalist"
            else ["src/module.py", "tests/test_module.py"]
        )
    )
    return WorkerExecutionResult(
        exit_code=0,
        summary=f"Completed execution for {case['eval_case_id']} and verified deliverables: {', '.join(files)}.",
        response_text=f"Created {', '.join(files)} fulfilling the user request.",
        claude_session_id=f"dry-sess-{task_id}",
        files_changed=files,
        diff_summary=f"{len(files)} file(s) changed (+35 -0) in {task_id}.",
        raw_diff=f"diff --git a/{files[0]} b/{files[0]}\n+# Deliverable for {case['eval_case_id']}\n",
        branch=branch,
        worktree_path=wt_path,
        harness=harness,
        events=["Running agents-cli scaffold create", "Completed execution."],
    )


async def run_for_harness(
    *,
    harness: str,
    cases: list[dict[str, Any]],
    output_dir: Path,
    fixtures_dir: Path,
    dry_run: bool = False,
) -> Path:
    """Execute multi-step CUJ trajectories for `harness` and emit traces + fixtures."""
    worker = SandboxWorker()
    eval_cases_out: list[dict[str, Any]] = []
    harness_fixture_dir = fixtures_dir / harness
    harness_fixture_dir.mkdir(parents=True, exist_ok=True)
    harness_trace_dir = output_dir / harness
    harness_trace_dir.mkdir(parents=True, exist_ok=True)

    for idx, case in enumerate(cases, start=1):
        case_id = case["eval_case_id"]
        repo = case.get("repo", "default-repo")
        steps = list(case.get("steps") or [])
        task_id = f"eval-{harness}-{case_id}-{int(time.time())}"

        print(
            f"[{harness}] ({idx}/{len(cases)}) Running CUJ {case_id} "
            f"(repo={repo}, steps={len(steps)})..."
        )
        await _install_hermetic_agents_cli_shim(worker, task_id=task_id, dry_run=dry_run)

        step_records: list[dict[str, Any]] = []
        session_id: str | None = None
        active_harness = harness
        prior_summary: str | None = None
        prior_files: list[str] = []
        wt_path = f"/workspace/.worktrees/{task_id}"
        branch = f"agent/{task_id}"
        t0_total = time.monotonic()

        for s_idx, step in enumerate(steps):
            step_mode = step.get("mode", "execute")
            step_goal = step["goal"]
            handoff_target = step.get("handoff_harness")
            if handoff_target and handoff_target != active_harness:
                handoff_block = build_cross_harness_handoff(
                    previous_harness=active_harness,
                    new_harness=handoff_target,
                    previous_summary=prior_summary,
                    files_changed=prior_files,
                    worktree_path=wt_path,
                    branch=branch,
                )
                effective_goal = f"{handoff_block}\n\nFollow-up instruction: {step_goal}"
                active_harness = handoff_target
                session_id = None
            elif s_idx > 0:
                effective_goal = (
                    f"Prior step summary: {prior_summary or 'Plan reviewed.'}\n"
                    f"Follow-up instruction: {step_goal}"
                )
            else:
                effective_goal = step_goal

            t_step = time.monotonic()
            if dry_run:
                res = _build_dry_run_step_result(case, step, s_idx, active_harness, task_id)
            else:
                res = await worker.execute_task(
                    goal=effective_goal,
                    repo=repo,
                    task_id=task_id,
                    mode=step_mode,
                    require_approval=False,
                    harness=active_harness,
                    session_id=session_id,
                )
            step_elapsed = round(time.monotonic() - t_step, 2)
            session_id = res.claude_session_id or session_id
            prior_summary = res.summary
            prior_files = list(res.files_changed or prior_files)
            wt_path = res.worktree_path or wt_path
            branch = res.branch or branch

            step_rec = {
                "step_index": s_idx,
                "harness": active_harness,
                "mode": step_mode,
                "goal": step_goal,
                "wall_clock_s": step_elapsed,
                "exit_code": res.exit_code,
                "summary": res.summary,
                "response_text": res.response_text,
                "error": res.error,
                "claude_session_id": res.claude_session_id,
                "files_changed": res.files_changed,
                "questions": res.questions,
                "diff_summary": res.diff_summary,
                "raw_diff": res.raw_diff,
                "events": res.events,
            }
            step_records.append(step_rec)
            print(
                f"  -> step {s_idx + 1}/{len(steps)} ({active_harness}, mode={step_mode}): "
                f"exit={res.exit_code}, files={res.files_changed}, time={step_elapsed}s"
            )

        total_wall_s = round(time.monotonic() - t0_total, 2)
        verification = await _inspect_and_cleanup_worktree(
            worker,
            task_id=task_id,
            worktree_path=wt_path,
            dry_run=dry_run,
        )

        # Combine stream-level mentions of agents-cli with audit shim invocations
        all_events_str = " ".join(
            " ".join(sr.get("events") or []) + " " + (sr.get("response_text") or "")
            for sr in step_records
        )
        used_agents_cli = bool(verification["agents_cli_invocations"]) or (
            "agents-cli" in all_events_str
        )

        last_step = step_records[-1]
        sandbox_meta = {
            "eval_case_id": case_id,
            "persona": case.get("persona", "developer"),
            "harness": harness,
            "repo": repo,
            "require_agents_cli": bool(case.get("require_agents_cli", False)),
            "used_agents_cli": used_agents_cli,
            "agents_cli_invocations": verification["agents_cli_invocations"],
            "adk_agent_detected": verification["adk_agent_detected"],
            "adk_evidence": verification.get("adk_evidence", ""),
            "workspace_files": verification["workspace_files"],
            "wall_clock_s": total_wall_s,
            "steps": step_records,
            "exit_code": max((sr["exit_code"] for sr in step_records), default=1),
            "summary": last_step["summary"],
            "response_text": last_step["response_text"],
            "error": next((sr["error"] for sr in step_records if sr.get("error")), None),
            "files_changed": last_step["files_changed"] or verification["workspace_files"],
            "questions": step_records[0]["questions"] if step_records else [],
            "diff_summary": last_step["diff_summary"],
            "raw_diff": last_step["raw_diff"],
            "pytest_exit_code": verification["pytest_exit_code"],
            "pytest_stdout": verification["pytest_stdout"],
            "reference": str(case.get("reference", "")),
        }

        # 1. Save fixture for Phase 3 (L3 voice replay)
        fixture_file = harness_fixture_dir / f"{case_id}.json"
        fixture_file.write_text(json.dumps(sandbox_meta, indent=2) + "\n", encoding="utf-8")

        # 2. Build EvaluationDataset trace for `agents-cli eval grade`
        trajectory_summary_lines = [
            f"CUJ: {case_id} (persona={sandbox_meta['persona']}, repo={repo}, total_time={total_wall_s}s)"
        ]
        for sr in step_records:
            trajectory_summary_lines.append(
                f"\n--- Step {sr['step_index'] + 1} [{sr['harness']} | mode={sr['mode']} | exit={sr['exit_code']}] ---\n"
                f"Goal: {sr['goal']}\n"
                f"Summary: {sr['summary']}\n"
                f"Questions: {'; '.join(sr['questions']) if sr['questions'] else '(none)'}\n"
                f"Files Changed: {', '.join(sr['files_changed']) if sr['files_changed'] else '(none)'}"
            )
        trajectory_summary_lines.append(
            f"\n--- Worktree Verification ---\n"
            f"Workspace Files: {', '.join(verification['workspace_files']) or '(none)'}\n"
            f"agents-cli Used: {used_agents_cli} (invocations={verification['agents_cli_invocations']})\n"
            f"ADK Agent Detected: {verification['adk_agent_detected']}\n"
            f"Pytest Exit Code: {verification['pytest_exit_code']} ({verification['pytest_stdout'][:200]})"
        )
        final_text = "\n".join(trajectory_summary_lines)
        first_goal = steps[0]["goal"] if steps else case_id
        ref_text = str(case.get("reference", ""))

        trace_case = {
            "eval_case_id": case_id,
            "prompt": {
                "role": "user",
                "parts": [{"text": first_goal}],
            },
            "responses": [
                {
                    "response": {
                        "role": "model",
                        "parts": [{"text": final_text}],
                    }
                }
            ],
            "reference": {
                "response": {
                    "role": "model",
                    "parts": [{"text": ref_text}],
                }
            },
            "agent_data": {
                "agents": {
                    f"sandbox_{harness}": {
                        "agent_id": f"sandbox_{harness}",
                        "agent_type": "CodingHarness",
                        "instruction": f"Hermetic {harness} coding harness inside Vertex Sandbox.",
                    }
                },
                "turns": [
                    {
                        "turn_index": 0,
                        "events": [
                            {
                                "author": "user",
                                "content": {
                                    "role": "user",
                                    "parts": [{"text": first_goal}],
                                },
                            },
                            {
                                "author": f"sandbox_{harness}",
                                "content": {
                                    "role": "model",
                                    "parts": [
                                        {
                                            "function_call": {
                                                "name": "execute_cuj_trajectory",
                                                "args": {
                                                    "eval_case_id": case_id,
                                                    "repo": repo,
                                                    "harness": harness,
                                                },
                                            }
                                        }
                                    ],
                                },
                            },
                            {
                                "author": "tool",
                                "content": {
                                    "role": "tool",
                                    "parts": [
                                        {
                                            "function_response": {
                                                "name": "execute_cuj_trajectory",
                                                "response": sandbox_meta,
                                            }
                                        }
                                    ],
                                },
                            },
                            {
                                "author": f"sandbox_{harness}",
                                "content": {
                                    "role": "model",
                                    "parts": [{"text": final_text}],
                                },
                            },
                        ],
                    }
                ],
            },
        }
        eval_cases_out.append(trace_case)

    trace_path = harness_trace_dir / "traces.json"
    trace_payload = {"eval_cases": eval_cases_out}
    trace_path.write_text(json.dumps(trace_payload, indent=2) + "\n", encoding="utf-8")
    print(f"[{harness}] Wrote {len(eval_cases_out)} CUJ trace(s) to {trace_path}")
    return trace_path


async def _async_main() -> int:
    parser = argparse.ArgumentParser(
        description="Run hermetic multi-step CUJ sandbox evaluations and emit traces for agents-cli eval grade."
    )
    parser.add_argument(
        "--harness",
        default="claude",
        choices=["claude", "antigravity", "horizon", "all"],
        help="Which coding harness(es) to evaluate in the sandbox.",
    )
    parser.add_argument(
        "--dataset",
        default="tests/eval/sandbox/sandbox_dataset.json",
        help="Path to sandbox evaluation dataset JSON.",
    )
    parser.add_argument(
        "--case",
        default="",
        help="Optional comma-separated eval_case_id filter.",
    )
    parser.add_argument(
        "--output",
        default="artifacts/sandbox_traces",
        help="Output directory for generated trace JSON files.",
    )
    parser.add_argument(
        "--fixtures-dir",
        default="tests/eval/fixtures",
        help="Directory where recorded sandbox outputs are saved for L3 voice replay.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Emit synthetic traces without calling the remote Vertex Sandbox (for pipeline smoke testing).",
    )
    args = parser.parse_args()

    dataset_path = Path(args.dataset)
    raw_data = json.loads(dataset_path.read_text(encoding="utf-8"))
    cases = list(raw_data.get("eval_cases", []))
    if args.case.strip():
        wanted = {c.strip() for c in args.case.split(",") if c.strip()}
        cases = [c for c in cases if c.get("eval_case_id") in wanted]
    if not cases:
        print("No matching eval_cases found.", file=sys.stderr)
        return 1

    harnesses = (
        ["claude", "antigravity", "horizon"]
        if args.harness == "all"
        else [args.harness]
    )
    for h in harnesses:
        await run_for_harness(
            harness=h,
            cases=cases,
            output_dir=Path(args.output),
            fixtures_dir=Path(args.fixtures_dir),
            dry_run=args.dry_run,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_async_main()))
