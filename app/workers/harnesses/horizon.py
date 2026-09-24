"""Google ADK Long Horizon coding harness running inside the Vertex Sandbox (Port 8081 RemoteA2aAgent)."""

from __future__ import annotations

import os
import warnings
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

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
    collect_worktree_changes,
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
        return f"https://{context.lb_host}"

    def format_sandbox_command(
        self,
        *,
        goal: str,
        repo: str,
        task_id: str,
        mode: str = "execute",
        session_id: str | None = None,
    ) -> str:
        base = self.a2a_url_override or "https://<sandbox_lb_host>"
        sess_tag = f", session_id={session_id}" if session_id else ""
        return f"RemoteA2aAgent({base}{AGENT_CARD_WELL_KNOWN_PATH}, X-Sandbox-Port=8081, mode={mode}{sess_tag})"

    def build_env(self) -> dict[str, str]:
        from app.auth import get_sandbox_gcp_env

        gcp_env = get_sandbox_gcp_env()
        project_id = (
            gcp_env.get("GOOGLE_CLOUD_PROJECT")
            or os.getenv("SANDBOX_GCP_PROJECT")
            or os.getenv("GOOGLE_CLOUD_PROJECT")
            or ""
        ).strip()
        env = {
            **gcp_env,
            "GOOGLE_CLOUD_PROJECT": project_id,
            "GOOGLE_CLOUD_LOCATION": os.getenv(
                "SANDBOX_GCP_LOCATION",
                os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1"),
            ),
            "LHA_ENVIRONMENT": os.getenv("LHA_ENVIRONMENT", "local"),
            "LHA_ENVIRONMENT_BACKEND": os.getenv("LHA_ENVIRONMENT_BACKEND", "local"),
            "USE_IN_MEMORY_SESSION": os.getenv("USE_IN_MEMORY_SESSION", "true"),
            "USE_IN_MEMORY_TASK_STORE": os.getenv("USE_IN_MEMORY_TASK_STORE", "true"),
        }
        # Declared here so the pinned model travels with the run record. It only
        # takes effect when the A2A server is (re)started with it set; see
        # `provisioner.py`.
        lha_model = os.getenv("LHA_ROOT_MODEL", "").strip()
        if lha_model:
            env["LHA_ROOT_MODEL"] = lha_model
        return env

    def build_provision_commands(
        self,
        a2a_port: int = 8081,
        profile: Any | None = None,
    ) -> list[str]:
        """Commands executed via sandbox /exec (port 8080) to install latest GitHub/fork Horizon and start A2A server."""
        from app.workers.harnesses.provisioner import get_default_provision_recipe

        return list(get_default_provision_recipe("horizon", a2a_port, profile)["commands"])

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
        sess_id = session_id or f"horizon-sess-{task_id}"
        branch = context.branch or f"agent/{task_id}"
        wt_path = str(context.worktree_dir or f"/workspace/.worktrees/{task_id}")

        base_url = self.get_sandbox_a2a_url(context)
        agent_card_url = f"{base_url}{AGENT_CARD_WELL_KNOWN_PATH}"
        sandbox_headers = context.build_sandbox_headers(port=context.a2a_port)

        transport = self.http_transport or context.http_transport

        events_log: list[str] = []
        progress_ev = HarnessEvent(
            task_id=task_id,
            harness=self.name,
            kind="progress",
            message="Starting ADK Long Horizon session...",
        )
        events_log.append(progress_ev.message)
        if on_event:
            on_event(progress_ev)

        async def _rewrite_agent_card_origin(response: httpx.Response) -> None:
            """Ensure agent-card.json RPC URLs match the sandbox LB origin (`base_url`) fetched by RemoteA2aAgent."""
            if response.status_code == 200 and response.request.url.path.endswith("agent-card.json"):
                raw_bytes = await response.aread()
                try:
                    import json
                    from urllib.parse import urlparse

                    card_data = json.loads(raw_bytes.decode("utf-8"))
                    if isinstance(card_data, dict):
                        origin = base_url.rstrip("/")

                        def _swap_origin(u: str) -> str:
                            parsed = urlparse(u)
                            path = parsed.path or "/a2a"
                            return f"{origin}{path}"

                        if isinstance(card_data.get("url"), str):
                            card_data["url"] = _swap_origin(card_data["url"])
                        if isinstance(card_data.get("supportedInterfaces"), list):
                            for iface in card_data["supportedInterfaces"]:
                                if isinstance(iface, dict) and isinstance(iface.get("url"), str):
                                    iface["url"] = _swap_origin(iface["url"])
                        response._content = json.dumps(card_data).encode("utf-8")
                        if hasattr(response, "_text"):
                            delattr(response, "_text")
                except Exception:
                    pass

        from app.auth import get_local_adc_json_for_sandbox
        from app.workers.harnesses.base import exec_in_sandbox

        adc_json = get_local_adc_json_for_sandbox()
        if adc_json and transport is None:
            import shlex

            run_env = self.build_env()
            proj_id = run_env.get("GOOGLE_CLOUD_PROJECT", "").strip()
            gh_tok = run_env.get("GH_TOKEN", "").strip()
            gh_env_str = (
                f"GH_TOKEN={shlex.quote(gh_tok)} GITHUB_PERSONAL_ACCESS_TOKEN={shlex.quote(gh_tok)} "
                if gh_tok
                else ""
            )
            lha_model = os.getenv("LHA_ROOT_MODEL", "").strip()
            lha_model_env = f"LHA_ROOT_MODEL={shlex.quote(lha_model)} " if lha_model else ""
            spawn_py = (
                "import subprocess, signal; "
                "signal.signal(signal.SIGHUP, signal.SIG_IGN); "
                "p = subprocess.Popen(['/workspace/.sonar-venv/bin/python', '-m', 'uvicorn', "
                f"'horizon.fast_api_app:app', '--host', '0.0.0.0', '--port', '{context.a2a_port}'], "
                "stdin=subprocess.DEVNULL, stdout=open('/tmp/horizon-a2a.log', 'w'), "
                "stderr=subprocess.STDOUT, start_new_session=True, close_fds=True); "
                "open('/tmp/horizon.pid', 'w').write(str(p.pid))"
            )
            horizon_loc = os.getenv("HORIZON_GCP_LOCATION", "global").strip()
            daemon_key = f"{proj_id}:{horizon_loc}:{lha_model}:{'gh' if gh_tok else 'nogh'}"
            sync_daemon_cmd = (
                f"mkdir -p /workspace/.sonar && "
                f"printf %s {shlex.quote(adc_json)} > /workspace/.sonar/application_default_credentials.json && "
                f"CUR_KEY=$(cat /tmp/horizon.project 2>/dev/null || echo ''); "
                f"if [ \"$CUR_KEY\" != {shlex.quote(daemon_key)} ] || ! curl -fsSL http://127.0.0.1:{context.a2a_port}/.well-known/agent-card.json >/dev/null 2>&1; then "
                f"  ([ -f /tmp/horizon.pid ] && kill -9 $(cat /tmp/horizon.pid) 2>/dev/null || true); "
                f"  printf %s {shlex.quote(daemon_key)} > /tmp/horizon.project; "
                f"  LHA_ENVIRONMENT_BACKEND=local USE_IN_MEMORY_SESSION=true USE_IN_MEMORY_TASK_STORE=true "
                f"  GOOGLE_GENAI_USE_VERTEXAI=TRUE {lha_model_env}{gh_env_str}"
                f"  GOOGLE_CLOUD_PROJECT={shlex.quote(proj_id)} GOOGLE_CLOUD_LOCATION={shlex.quote(horizon_loc)} "
                f"  GOOGLE_APPLICATION_CREDENTIALS=/workspace/.sonar/application_default_credentials.json "
                f"  APP_URL=http://127.0.0.1:{context.a2a_port} "
                f"  python3 -c {shlex.quote(spawn_py)}; "
                f"  for i in 1 2 3 4 5 6 7 8 9 10 11 12; do "
                f"    curl -fsSL http://127.0.0.1:{context.a2a_port}/.well-known/agent-card.json >/dev/null 2>&1 && break; "
                f"    sleep 1; "
                f"  done; "
                f"fi"
            )
            await exec_in_sandbox(context, sync_daemon_cmd, timeout_s=25.0)

        final_texts: list[str] = []
        a2a_error: str | None = None

        try:
            async with httpx.AsyncClient(
                transport=transport,
                headers=sandbox_headers,
                event_hooks={"response": [_rewrite_agent_card_origin]},
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

                    from app.workers.harnesses.prompts import (
                        PLAN_OUTPUT_SCHEMA_JSON,
                        extract_plan_from_json_output,
                        extract_plan_steps_from_json_output,
                    )

                    goal_with_meta = f"[Repo: {repo}] [Worktree: {wt_path}] [Branch: {branch}] [Task: {task_id}] {goal}"
                    if mode == "plan":
                        goal_with_meta += (
                            f"\n\nReturn your proposed plan as a JSON object matching this schema: {PLAN_OUTPUT_SCHEMA_JSON}"
                        )

                    prompt_text = build_harness_user_prompt(
                        goal=goal_with_meta,
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
                        if getattr(adk_event, "error_message", None):
                            a2a_error = str(adk_event.error_message)
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
        except Exception as exc:
            a2a_error = f"ADK Long Horizon A2A execution failed: {exc}"

        raw_combined_check = "\n".join(final_texts).strip()
        if not a2a_error and (
            raw_combined_check.startswith("403 Forbidden")
            or raw_combined_check.startswith("404 Not Found")
            or "PERMISSION_DENIED" in raw_combined_check
            or "aiplatform.endpoints.predict" in raw_combined_check
        ):
            a2a_error = f"Horizon upstream model error: {raw_combined_check[:300]}"

        if not a2a_error and not final_texts and mode != "plan":
            changed_probe, diff_sum_probe, _ = await collect_worktree_changes(
                wt_path, context=context
            )
            if changed_probe:
                final_texts.append(
                    f"Completed execution in {repo}: {diff_sum_probe or ', '.join(changed_probe[:5])}"
                )

        if a2a_error or not final_texts:
            err_msg = a2a_error or (
                f"ADK Long Horizon returned no response from {agent_card_url}. "
                "Verify the Horizon A2A server and agent card origin in the sandbox."
            )
            if on_event:
                on_event(
                    HarnessEvent(
                        task_id=task_id,
                        harness=self.name,
                        kind="error",
                        message=err_msg,
                    )
                )
            return WorkerExecutionResult(
                exit_code=1,
                summary=err_msg,
                response_text=err_msg,
                error=err_msg,
                claude_session_id=sess_id,
                branch=branch,
                worktree_path=wt_path,
                harness=self.name,
                events=[*events_log, err_msg],
            )

        # RemoteA2aAgent streams incremental text chunks; joining with "" preserves
        # token boundaries (whereas "\n".join split JSON strings across lines and
        # `final_texts[-1]` kept only the final 2-3 token tail).
        raw_combined = "".join(final_texts).strip()
        from app.workers.harnesses.base import (
            _humanize_artifact_title,
            collect_worktree_artifacts,
        )

        # Extract any files Horizon saved via `/lha/workspace/download?path=...`
        import re as _re
        import shlex as _shlex

        lha_paths = _re.findall(
            r"/lha/workspace/download\?path=([^&\)\s\"']+)",
            "\n".join(events_log) + "\n" + raw_combined,
        )
        lha_artifacts: list[dict[str, str]] = []
        if lha_paths and transport is None:
            for rel_p in dict.fromkeys(lha_paths):
                dl_url = f"http://127.0.0.1:{context.a2a_port}/lha/workspace/download?path={rel_p}&inline=1"
                dl_res = await exec_in_sandbox(
                    context,
                    f"curl -fsSL {_shlex.quote(dl_url)} 2>/dev/null | head -c 14000 || true",
                    timeout_s=12.0,
                )
                content_str = (dl_res.stdout or "").strip()
                if content_str:
                    lha_artifacts.append(
                        {
                            "name": rel_p,
                            "title": _humanize_artifact_title(rel_p, content_str),
                            "content": content_str,
                        }
                    )

        if mode == "plan":
            parsed = extract_plan_from_json_output(
                raw_combined,
                fallback_session_id=sess_id,
            )
            if parsed is None:
                if not raw_combined.strip():
                    empty_msg = (
                        f"{self.display_name} returned an empty plan from {agent_card_url}."
                    )
                    return WorkerExecutionResult(
                        exit_code=1,
                        summary=empty_msg,
                        response_text=empty_msg,
                        error=empty_msg,
                        claude_session_id=sess_id,
                        branch=branch,
                        worktree_path=wt_path,
                        harness=self.name,
                        events=[*events_log, empty_msg],
                    )
                parsed_sess, plan_summary, questions, _files = (
                    sess_id,
                    raw_combined.strip(),
                    [],
                    [],
                )
            else:
                parsed_sess, plan_summary, questions, _files = parsed
            plan_steps = extract_plan_steps_from_json_output(raw_combined)
            full_plan_text = (
                f"{plan_summary} Steps: {'; '.join(plan_steps)}"
                if plan_steps
                else plan_summary
            )
            if not lha_artifacts and len(raw_combined.strip()) > 260:
                lha_artifacts.append(
                    {
                        "name": "plan.md",
                        "title": _humanize_artifact_title(goal, raw_combined),
                        "content": raw_combined.strip(),
                    }
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
                artifacts=lha_artifacts,
                questions=questions,
                awaiting_input=True,
                branch=branch,
                worktree_path=wt_path,
                harness=self.name,
                events=[plan_summary[:180], *plan_steps[:3]],
            )

        summary = raw_combined
        changed, diff_sum, raw_diff = await collect_worktree_changes(wt_path, context=context)
        artifacts = lha_artifacts or await collect_worktree_artifacts(wt_path, changed, context=context)
        if not artifacts and len(raw_combined.strip()) > 260:
            artifacts = [
                {
                    "name": "output.md",
                    "title": _humanize_artifact_title(goal, raw_combined),
                    "content": raw_combined.strip(),
                }
            ]
        actual_branch, actual_wt = branch, wt_path
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
            artifacts=artifacts,
            awaiting_approval=require_approval,
            diff_summary=diff_sum,
            raw_diff=raw_diff,
            pending_action=pending,
            branch=actual_branch,
            worktree_path=actual_wt,
            harness=self.name,
            events=events_log,
        )
