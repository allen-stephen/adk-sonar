"""Google ADK Long Horizon coding harness running inside the Vertex Sandbox (Port 8081 RemoteA2aAgent)."""

from __future__ import annotations

import asyncio
import json
import os
import warnings
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx
from google.adk.agents.remote_a2a_agent import (
    AGENT_CARD_WELL_KNOWN_PATH,
    RemoteA2aAgent,
)
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from app.workers.base import WorkerExecutionResult
from app.workers.harnesses.base import (
    HarnessEvent,
    SandboxContext,
    materialize_workspace_output,
    parse_stream_json_line,
)
from app.workers.harnesses.prompts import build_harness_user_prompt


@dataclass
class HorizonA2AHarness:
    """Google ADK Long Horizon harness running inside the Vertex Sandbox (`adk api_server --a2a`).

    Consumed via ADK's `RemoteA2aAgent` routed into the sandbox container's A2A port
    (`X-Sandbox-Port: 8081` + `X-Sandbox-Routing-Token`), so `horizon` runs inside the
    same persistent container as Claude Code and Antigravity while speaking native A2A.
    """

    name: str = "horizon"
    display_name: str = "ADK Long Horizon"
    execution_mode: str = "sandbox_a2a"
    description: str = (
        "Google ADK Long Horizon harness (`adk api_server --a2a --port 8081`) running inside "
        "the Vertex Sandbox and consumed via ADK's RemoteA2aAgent."
    )
    binary_name: str = "adk"
    fallback_binaries: tuple[str, ...] = ("adk", "uv")
    a2a_url_override: str | None = field(
        default_factory=lambda: os.getenv("HORIZON_A2A_URL")
    )
    http_transport: httpx.AsyncBaseTransport | None = None
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
            "api_server",
            "--a2a",
            "--port",
            "8081",
            "horizon",
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
        """Return the A2A daemon invocation paired with the session metadata."""
        return self.build_command(
            goal=goal,
            repo=repo,
            task_id=task_id,
            mode=mode,
            binary=binary,
        )

    def get_sandbox_a2a_url(self, context: SandboxContext) -> str:
        if self.a2a_url_override:
            return self.a2a_url_override.rstrip("/")
        return f"https://{context.lb_host}/a2a/horizon"

    def format_sandbox_command(
        self,
        *,
        goal: str,
        repo: str,
        task_id: str,
        mode: str = "execute",
        session_id: str | None = None,
    ) -> str:
        base = self.a2a_url_override or "https://<sandbox_lb_host>/a2a/horizon"
        sess_tag = f", session_id={session_id}" if session_id else ""
        return f"RemoteA2aAgent({base}{AGENT_CARD_WELL_KNOWN_PATH}, X-Sandbox-Port=8081, mode={mode}{sess_tag})"

    def build_env(self) -> dict[str, str]:
        return {
            "GOOGLE_CLOUD_PROJECT": os.getenv(
                "GOOGLE_CLOUD_PROJECT", "agents-cli-test-dev-qi5zi1"
            ),
            "GOOGLE_CLOUD_LOCATION": os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1"),
            "LHA_ENVIRONMENT": os.getenv("LHA_ENVIRONMENT", "local"),
            "LHA_ENVIRONMENT_BACKEND": os.getenv("LHA_ENVIRONMENT_BACKEND", "local"),
            "USE_IN_MEMORY_SESSION": os.getenv("USE_IN_MEMORY_SESSION", "true"),
            "USE_IN_MEMORY_TASK_STORE": os.getenv("USE_IN_MEMORY_TASK_STORE", "true"),
        }

    def build_provision_commands(self, a2a_port: int = 8081) -> list[str]:
        """Commands executed via sandbox /exec (port 8080) to install latest GitHub/fork Horizon and start A2A server."""
        from app.workers.harnesses.provisioner import get_default_provision_recipe

        return list(get_default_provision_recipe("horizon", a2a_port)["commands"])

    def parse_stream_line(self, line: str, task_id: str) -> HarnessEvent | None:
        return parse_stream_json_line(line, task_id, self.name)

    def _build_mock_a2a_transport(
        self,
        *,
        goal: str,
        repo: str,
        task_id: str,
        context: SandboxContext,
        base_url: str,
    ) -> httpx.MockTransport:
        """Creates an in-sandbox A2A MockTransport serving `agent-card.json` and A2A task responses."""

        async def _handler(request: httpx.Request) -> httpx.Response:
            assert request.headers.get("X-Sandbox-Port") == str(context.a2a_port)
            assert (
                request.headers.get("X-Sandbox-Routing-Token")
                == context.routing_token
            )

            if request.url.path.endswith("agent-card.json") or request.url.path.endswith(
                "agent.json"
            ):
                return httpx.Response(
                    200,
                    json={
                        "name": "horizon_sandbox_agent",
                        "description": self.description,
                        "url": base_url,
                        "version": "1.0.0",
                        "capabilities": {},
                        "defaultInputModes": ["text/plain"],
                        "defaultOutputModes": ["application/json"],
                        "skills": [
                            {
                                "id": "long_horizon_coding",
                                "name": "Long Horizon Sandbox Coding",
                                "description": "Executes coding tasks inside the Vertex Sandbox",
                                "tags": ["coding", "sandbox", "adk"],
                            }
                        ],
                    },
                )

            await asyncio.sleep(self.mock_step_delay_s)
            body = json.loads(request.content.decode("utf-8"))
            rpc_id = body.get("id", task_id)
            branch = context.branch or f"agent/{task_id}"
            summary_text = (
                f"Task {task_id} completed by ADK Long Horizon inside sandbox "
                f"{context.sandbox_name} (port {context.a2a_port}) on {repo}. "
                f"Branch {branch} updated."
            )
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "result": {
                        "kind": "task",
                        "id": task_id,
                        "contextId": f"ctx-{task_id}",
                        "status": {
                            "state": "completed",
                            "message": {
                                "kind": "message",
                                "messageId": f"msg-{task_id}",
                                "role": "agent",
                                "parts": [{"kind": "text", "text": summary_text}],
                            },
                        },
                    },
                },
            )

        return httpx.MockTransport(_handler)

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
        sess_id = session_id or f"horizon-sess-{task_id}"
        branch = context.branch or f"agent/{task_id}"
        wt_path = str(context.worktree_dir or f"/workspace/.worktrees/{task_id}")

        if mode == "plan":
            await asyncio.sleep(self.mock_step_delay_s)
            questions = [
                f"Should ADK Long Horizon persist architectural notes for '{goal}' in {repo} to Memory Bank?",
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

        base_url = self.get_sandbox_a2a_url(context)
        agent_card_url = f"{base_url}{AGENT_CARD_WELL_KNOWN_PATH}"
        sandbox_headers = context.build_sandbox_headers(port=context.a2a_port)

        transport = self.http_transport
        if transport is None and context.mock_mode:
            transport = self._build_mock_a2a_transport(
                goal=goal,
                repo=repo,
                task_id=task_id,
                context=context,
                base_url=base_url,
            )

        events_log: list[str] = []
        progress_ev = HarnessEvent(
            task_id=task_id,
            harness=self.name,
            kind="progress",
            message=(
                f"Connecting RemoteA2aAgent to ADK Long Horizon inside sandbox "
                f"{context.sandbox_name} (X-Sandbox-Port: {context.a2a_port}, worktree={wt_path})"
            ),
        )
        events_log.append(progress_ev.message)
        if on_event:
            on_event(progress_ev)

        async with httpx.AsyncClient(
            transport=transport,
            headers=sandbox_headers,
            timeout=httpx.Timeout(600.0, connect=15.0),
        ) as client:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                remote_agent = RemoteA2aAgent(
                    name="horizon_sandbox_agent",
                    description=self.description,
                    agent_card=agent_card_url,
                    httpx_client=client,
                    a2a_request_meta_provider=lambda _ctx, _msg: {
                        "task_id": task_id,
                        "repo": repo,
                        "worktree_path": wt_path,
                        "branch": branch,
                        "sandbox_name": context.sandbox_name,
                        "user_id": context.user_id,
                    },
                )

                session_service = InMemorySessionService()
                # Task-scoped session ID so multiple Horizon agents run concurrently without collision
                session = await session_service.create_session(
                    app_name="voice_orchestrator_a2a",
                    user_id=context.user_id,
                    session_id=sess_id,
                )
                runner = Runner(
                    agent=remote_agent,
                    session_service=session_service,
                    app_name="voice_orchestrator_a2a",
                )

                final_texts: list[str] = []
                prompt_text = build_harness_user_prompt(
                    goal=f"[Repo: {repo}] [Worktree: {wt_path}] [Branch: {branch}] [Task: {task_id}] {goal}",
                    repo=repo,
                    mode=mode,
                    worktree_path=wt_path,
                    branch=branch,
                    include_system_preamble=True,
                )
                async for adk_event in runner.run_async(
                    user_id=context.user_id,
                    session_id=session.id,
                    new_message=types.Content(
                        role="user",
                        parts=[types.Part(text=prompt_text)],
                    ),
                ):
                    if adk_event.content and adk_event.content.parts:
                        chunk = " ".join(
                            p.text for p in adk_event.content.parts if p.text
                        ).strip()
                        if chunk:
                            final_texts.append(chunk)
                            ev = HarnessEvent(
                                task_id=task_id,
                                harness=self.name,
                                kind="completed"
                                if adk_event.is_final_response()
                                else "progress",
                                message=chunk,
                            )
                            events_log.append(chunk)
                            if on_event:
                                on_event(ev)

        summary = (
            final_texts[-1]
            if final_texts
            else f"Task {task_id} completed by ADK Long Horizon in sandbox {context.sandbox_name}."
        )
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
            summary=summary,
            response_text=summary,
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
