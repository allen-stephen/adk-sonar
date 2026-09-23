"""Claude Code CLI coding harness running inside the Vertex Sandbox (Port 8080 /exec)."""

from __future__ import annotations

import json
import logging
import os
import shlex
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.integrations import get_integration_registry
from app.workers.base import WorkerExecutionResult
from app.workers.harnesses.base import (
    HarnessEvent,
    SandboxContext,
    collect_worktree_changes,
    exec_in_sandbox,
    parse_stream_json_line,
)
from app.workers.harnesses.prompts import (
    PLAN_OUTPUT_SCHEMA_JSON,
    build_harness_system_prompt,
    extract_plan_from_json_output,
)

logger = logging.getLogger(__name__)


@dataclass
class ClaudeCodeHarness:
    """Headless Claude Code CLI (`claude -p`) running inside the Vertex Sandbox (Port 8080 /exec)."""

    name: str = "claude"
    display_name: str = "Claude Code"
    execution_mode: str = "sandbox_exec"
    description: str = (
        "Headless Claude Code CLI (`claude -p`) running inside the Vertex Sandbox via /exec "
        "with stream-json output and MCP server integration."
    )
    binary_name: str = "claude"
    fallback_binaries: tuple[str, ...] = ("claude",)
    live_timeout_s: float = 600.0

    def build_command(
        self,
        *,
        goal: str,
        repo: str,
        task_id: str,
        mode: str = "execute",
        binary: str | None = None,
    ) -> list[str]:
        cmd = [
            binary or self.binary_name,
            "-p",
            goal,
            "--output-format",
            "stream-json",
        ]
        if mode == "plan":
            cmd.extend(["--permission-mode", "plan"])
        mcp_cfg = get_integration_registry().build_claude_mcp_config()
        if mcp_cfg.get("mcpServers"):
            cmd.extend(["--mcp-config", json.dumps(mcp_cfg)])
        return cmd

    def build_headless_invocation(
        self,
        *,
        goal: str,
        repo: str,
        task_id: str,
        mode: str = "execute",
        session_id: str | None = None,
        worktree_path: str = "/workspace",
        branch: str = "main",
        binary: str | None = None,
    ) -> list[str]:
        """Build the full 2-phase headless Claude Code invocation (`plan` with JSON schema vs `execute` with `--resume`)."""
        sys_prompt = build_harness_system_prompt(
            mode=mode,
            repo=repo,
            worktree_path=worktree_path,
            branch=branch,
        )
        cmd = [
            binary or self.binary_name,
            "--bare",
            "-p",
            goal,
            "--append-system-prompt",
            sys_prompt,
            "--permission-prompts",
            "none",
        ]
        if session_id:
            cmd.extend(["--resume", session_id])

        if mode == "plan":
            cmd.extend(
                [
                    "--permission-mode",
                    "plan",
                    "--output-format",
                    "json",
                    "--json-schema",
                    PLAN_OUTPUT_SCHEMA_JSON,
                ]
            )
        else:
            cmd.extend(
                [
                    "--permission-mode",
                    "acceptEdits",
                    "--allowedTools",
                    "Bash,Read,Edit,Write",
                    "--output-format",
                    "stream-json",
                    "--verbose",
                ]
            )

        mcp_cfg = get_integration_registry().build_claude_mcp_config()
        if mcp_cfg.get("mcpServers"):
            cmd.extend(["--mcp-config", json.dumps(mcp_cfg)])
        return cmd

    def format_sandbox_command(
        self,
        *,
        goal: str,
        repo: str,
        task_id: str,
        mode: str = "execute",
        session_id: str | None = None,
        worktree_path: str = "/workspace",
        branch: str = "main",
    ) -> str:
        return shlex.join(
            self.build_headless_invocation(
                goal=goal,
                repo=repo,
                task_id=task_id,
                mode=mode,
                session_id=session_id,
                worktree_path=worktree_path,
                branch=branch,
            )
        )

    def build_env(self) -> dict[str, str]:
        from app.auth import get_sandbox_gcp_env

        return {
            **get_sandbox_gcp_env(),
            "CLAUDE_CODE_USE_VERTEX": os.getenv("CLAUDE_CODE_USE_VERTEX", "1"),
            "ANTHROPIC_VERTEX_PROJECT_ID": os.getenv(
                "ANTHROPIC_VERTEX_PROJECT_ID",
                os.getenv("GOOGLE_CLOUD_PROJECT", ""),
            ),
        }

    def build_provision_commands(self, profile: Any | None = None) -> list[str]:
        from app.workers.harnesses.provisioner import get_default_provision_recipe

        return list(get_default_provision_recipe("claude", profile=profile)["commands"])

    def parse_stream_line(self, line: str, task_id: str) -> HarnessEvent | None:
        return parse_stream_json_line(line, task_id, self.name)

    async def execute_in_sandbox(
        self,
        *,
        goal: str,
        repo: str,
        task_id: str,
        context: SandboxContext,
        mode: str = "execute",
        require_approval: bool = False,
        session_id: str | None = None,
        on_event: Callable[[HarnessEvent], None] | None = None,
    ) -> WorkerExecutionResult:
        """Run headless Claude Code inside the provisioned Vertex Sandbox via `/exec`."""
        sess_id = session_id or f"claude-sess-{task_id}"
        branch = context.branch or f"agent/{task_id}"
        wt_path = str(context.worktree_dir or f"/workspace/.worktrees/{task_id}")
        cmd = self.format_sandbox_command(
            goal=goal,
            repo=repo,
            task_id=task_id,
            mode=mode,
            session_id=session_id,
            worktree_path=wt_path,
            branch=branch,
        )
        logger.info(
            "Sandbox /exec [%s] (port=%s, mode=%s, session=%s, worktree=%s): %s",
            self.name,
            context.exec_port,
            mode,
            sess_id,
            wt_path,
            cmd,
        )

        def _fail(message: str, exit_code: int) -> WorkerExecutionResult:
            logger.error("[%s] task %s failed: %s", self.name, task_id, message)
            if on_event:
                on_event(
                    HarnessEvent(
                        task_id=task_id,
                        harness=self.name,
                        kind="error",
                        message=message,
                    )
                )
            return WorkerExecutionResult(
                exit_code=exit_code or 1,
                summary=message,
                response_text=message,
                error=message,
                claude_session_id=sess_id,
                branch=branch,
                worktree_path=wt_path,
                harness=self.name,
                events=[message],
            )

        exec_result = await exec_in_sandbox(
            context,
            f"mkdir -p {shlex.quote(wt_path)} && cd {shlex.quote(wt_path)} && {cmd}",
            env=self.build_env(),
            timeout_s=self.live_timeout_s,
        )

        if exec_result.error:
            return _fail(
                f"{self.display_name} could not reach the sandbox. {exec_result.error}",
                exec_result.exit_code,
            )

        if mode == "plan":
            if exec_result.exit_code != 0:
                return _fail(
                    f"{self.display_name} planning failed with exit code "
                    f"{exec_result.exit_code}. {exec_result.stderr.strip()[:300]}",
                    exec_result.exit_code,
                )
            from app.workers.harnesses.prompts import extract_plan_steps_from_json_output

            parsed_sess, plan_summary, questions, _files = extract_plan_from_json_output(
                exec_result.stdout,
                fallback_session_id=sess_id,
                goal=goal,
                repo=repo,
                display_name=self.display_name,
            )
            plan_steps = extract_plan_steps_from_json_output(exec_result.stdout)
            full_plan_text = (
                f"{plan_summary} Steps: {'; '.join(plan_steps)}"
                if plan_steps
                else plan_summary
            )
            if on_event:
                on_event(
                    HarnessEvent(
                        task_id=task_id,
                        harness=self.name,
                        kind="approval_needed",
                        message=full_plan_text,
                    )
                )
            return WorkerExecutionResult(
                exit_code=0,
                summary=plan_summary,
                response_text=full_plan_text,
                claude_session_id=parsed_sess,
                questions=questions,
                awaiting_input=True,
                branch=branch,
                worktree_path=wt_path,
                harness=self.name,
                events=[plan_summary, *plan_steps],
            )

        events_log: list[str] = []
        for raw_line in exec_result.stdout.splitlines():
            ev = self.parse_stream_line(raw_line, task_id)
            if ev:
                events_log.append(ev.message)
                if on_event:
                    on_event(ev)

        if exec_result.exit_code != 0:
            detail = exec_result.stderr.strip() or (events_log[-1] if events_log else "")
            return _fail(
                f"{self.display_name} exited with code {exec_result.exit_code}. {detail[:300]}",
                exec_result.exit_code,
            )

        changed, diff_sum, raw_diff = await collect_worktree_changes(wt_path, context=context)
        summary = (
            events_log[-1]
            if events_log
            else (
                f"Task {task_id} completed by {self.display_name} in sandbox "
                f"{context.sandbox_name.split('/')[-1]}."
            )
        )
        return WorkerExecutionResult(
            exit_code=0,
            summary=summary,
            response_text=summary,
            claude_session_id=sess_id,
            files_changed=changed,
            awaiting_approval=require_approval,
            diff_summary=diff_sum,
            raw_diff=raw_diff,
            pending_action=(
                f"git commit and push branch {branch} and open PR"
                if require_approval
                else None
            ),
            branch=branch,
            worktree_path=wt_path,
            harness=self.name,
            events=events_log,
        )
