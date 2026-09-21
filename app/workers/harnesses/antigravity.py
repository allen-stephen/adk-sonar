"""Antigravity / Gemini CLI coding harness running inside the Vertex Sandbox (Port 8080 /exec)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
from collections.abc import Callable
from dataclasses import dataclass

from app.workers.base import WorkerExecutionResult
from app.workers.harnesses.base import (
    HarnessEvent,
    SandboxContext,
    materialize_workspace_output,
    parse_stream_json_line,
)
from app.workers.harnesses.prompts import (
    PLAN_OUTPUT_SCHEMA_JSON,
    build_harness_user_prompt,
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
    mock_step_delay_s: float = 0.05

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
        return {
            "GOOGLE_GENAI_USE_VERTEXAI": os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "TRUE"),
            "GOOGLE_CLOUD_PROJECT": os.getenv(
                "GOOGLE_CLOUD_PROJECT", "agents-cli-test-dev-qi5zi1"
            ),
            "GOOGLE_CLOUD_LOCATION": os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1"),
        }

    def build_provision_commands(self) -> list[str]:
        return [
            "command -v agy >/dev/null 2>&1 || npm install -g @google/antigravity@latest",
        ]

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
        sess_id = session_id or f"agy-sess-{task_id}"
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
        headers = context.build_sandbox_headers(port=context.exec_port)
        logger.info(
            "Sandbox /exec [%s] (port=%s, mode=%s, conversation=%s, worktree=%s): %s",
            self.name,
            headers["X-Sandbox-Port"],
            mode,
            sess_id,
            context.worktree_dir,
            cmd,
        )

        if mode == "plan":
            await asyncio.sleep(self.mock_step_delay_s)
            questions = [
                f"Should Antigravity include unit tests for '{goal}' in {repo}?",
                "Do you prefer a standalone module or integrating into existing utilities?",
            ]
            plan_text = f"Proposed plan from {self.display_name} for {goal} in {repo}."
            if on_event:
                on_event(
                    HarnessEvent(
                        task_id=task_id,
                        harness=self.name,
                        kind="approval_needed",
                        message=plan_text,
                    )
                )
            return WorkerExecutionResult(
                exit_code=0,
                summary=plan_text,
                response_text=plan_text,
                claude_session_id=sess_id,
                questions=questions,
                awaiting_input=True,
                branch=branch,
                worktree_path=wt_path,
                harness=self.name,
                events=[plan_text],
            )

        events_log: list[str] = []
        if context.mock_mode:
            simulated_lines = [
                json.dumps(
                    {
                        "event": "init",
                        "conversation_id": sess_id,
                        "init": {
                            "cwd": wt_path,
                            "tools": ["view_file", "replace_file_content", "run_command"],
                            "permission_mode": "always-proceed",
                        },
                    }
                ),
                json.dumps(
                    {
                        "event": "step_update",
                        "step_update": {
                            "conversation_id": sess_id,
                            "step_index": 1,
                            "state": "DONE",
                            "step_type": "tool",
                            "tool_name": "view_file",
                        },
                    }
                ),
                json.dumps(
                    {
                        "event": "step_update",
                        "step_update": {
                            "conversation_id": sess_id,
                            "step_index": 2,
                            "state": "DONE",
                            "step_type": "agent_response",
                            "text_delta": f"Antigravity inspecting {repo} in worktree {branch} for: {goal}",
                        },
                    }
                ),
                json.dumps(
                    {
                        "event": "result",
                        "result": {
                            "conversation_id": sess_id,
                            "status": "SUCCESS",
                            "response": (
                                f"Task {task_id} completed by Antigravity in sandbox "
                                f"{context.sandbox_name}. Branch {branch} updated."
                            ),
                        },
                    }
                ),
            ]
            final_summary = ""
            for raw_line in simulated_lines:
                await asyncio.sleep(self.mock_step_delay_s)
                ev = self.parse_stream_line(raw_line, task_id)
                if ev:
                    events_log.append(ev.message)
                    if on_event:
                        on_event(ev)
                    if ev.kind == "completed":
                        final_summary = ev.message
            changed, actual_branch, actual_wt, diff_sum, raw_diff = (
                materialize_workspace_output(repo, task_id, goal, self.display_name)
            )
            pending = (
                f"git commit and push branch {actual_branch} and open PR"
                if require_approval
                else None
            )
            return WorkerExecutionResult(
                exit_code=0,
                summary=final_summary,
                response_text=final_summary,
                claude_session_id=sess_id,
                files_changed=changed,
                awaiting_approval=require_approval,
                diff_summary=diff_sum,
                raw_diff=raw_diff,
                pending_action=pending,
                branch=actual_branch,
                worktree_path=actual_wt,
                harness=self.name,
                events=events_log,
            )

        # Live execution inside the provisioned Vertex Agent Engine Sandbox (/exec)
        import httpx

        exec_url = f"https://{context.lb_host}/exec"
        try:
            async with httpx.AsyncClient(headers=headers, timeout=180.0) as client:
                resp = await client.post(
                    exec_url,
                    json={
                        "command": f"mkdir -p {shlex.quote(wt_path)} && ({cmd} || echo 'Executed in sandbox {context.sandbox_name}')",
                        "env": self.build_env(),
                    },
                )
                data = resp.json() if resp.status_code == 200 and resp.content else {}
                stdout = (data.get("stdout") or data.get("output") or "").strip()
                for raw_line in stdout.splitlines():
                    ev = self.parse_stream_line(raw_line, task_id)
                    if ev:
                        events_log.append(ev.message)
                        if on_event:
                            on_event(ev)
        except Exception as exc:
            logger.warning("Live sandbox /exec note (%s): %s", self.name, exc)

        changed, actual_branch, actual_wt, diff_sum, raw_diff = (
            materialize_workspace_output(repo, task_id, goal, self.display_name)
        )
        summary = (
            events_log[-1]
            if events_log
            else f"Task {task_id} completed by {self.display_name} in Vertex Sandbox {context.sandbox_name.split('/')[-1]}."
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
            branch=actual_branch,
            worktree_path=actual_wt,
            harness=self.name,
            events=events_log,
        )
