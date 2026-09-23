"""Antigravity / Gemini CLI coding harness running inside the Vertex Sandbox (Port 8080 /exec)."""

from __future__ import annotations

import logging
import os
import shlex
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

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
    build_harness_user_prompt,
    extract_plan_from_json_output,
)

logger = logging.getLogger(__name__)


@dataclass
class AntigravityHarness:
    """Headless Antigravity / Gemini CLI (`agy -p`) running inside the Vertex Sandbox (Port 8080 /exec)."""

    name: str = "antigravity"
    display_name: str = "Antigravity"
    execution_mode: str = "sandbox_exec"
    description: str = (
        "Headless Antigravity / Gemini coding CLI (`agy -p`) running inside the Vertex Sandbox "
        "with GEMINI.md rules, skills, and stream-json output."
    )
    binary_name: str = "agy"
    fallback_binaries: tuple[str, ...] = ("agy", "antigravity", "gemini")
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
        return [
            binary or self.binary_name,
            "-p",
            goal,
            "--yolo",
            "--output-format",
            "stream-json",
        ]

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
        """Build the official 2-phase Antigravity headless invocation (`plan` with `--json-schema` vs `execute` with `--conversation` & `--dangerously-skip-permissions`)."""
        prompt_with_instructions = build_harness_user_prompt(
            goal=goal,
            repo=repo,
            mode=mode,
            worktree_path=worktree_path,
            branch=branch,
            include_system_preamble=True,
        )
        cmd = [
            binary or self.binary_name,
            "-p",
            prompt_with_instructions,
        ]
        if session_id:
            cmd.extend(["--conversation", session_id])

        if mode == "plan":
            cmd.extend(
                [
                    "--output-format",
                    "json",
                    "--json-schema",
                    PLAN_OUTPUT_SCHEMA_JSON,
                ]
            )
        else:
            cmd.extend(
                [
                    "--dangerously-skip-permissions",
                    "--output-format",
                    "stream-json",
                ]
            )
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
            "GOOGLE_GENAI_USE_VERTEXAI": os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "TRUE"),
            "GOOGLE_CLOUD_PROJECT": os.getenv("GOOGLE_CLOUD_PROJECT", ""),
            "GOOGLE_CLOUD_LOCATION": os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1"),
        }

    def build_provision_commands(self, profile: Any | None = None) -> list[str]:
        from app.workers.harnesses.provisioner import get_default_provision_recipe

        return list(
            get_default_provision_recipe("antigravity", profile=profile)["commands"]
        )

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
        """Run headless Antigravity inside the provisioned Vertex Sandbox via `/exec`."""
        sess_id = session_id or f"agy-conv-{task_id}"
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
            "Sandbox /exec [%s] (port=%s, mode=%s, conversation=%s, worktree=%s): %s",
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
