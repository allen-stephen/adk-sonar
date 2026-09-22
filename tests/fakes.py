"""Test doubles for the coding-harness subsystem.

These live in the test tree on purpose. Production harnesses have exactly one
code path (real `/exec` or real A2A); anything that needs to stand in for a
sandbox belongs here.

Two flavours are provided:

1. :class:`FakeHarness` — implements the `CodingHarness` protocol directly.
   Use it when the harness itself is irrelevant and you are testing the layers
   above it (task registry, approval gate, tool responses, routing).

2. :func:`exec_transport` / :func:`a2a_transport` — `httpx.MockTransport`
   factories. Use these when you want to exercise the **real**
   `ClaudeCodeHarness` / `AntigravityHarness` / `HorizonA2AHarness` code and
   only fake the network boundary.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.workers.base import WorkerExecutionResult
from app.workers.harnesses.base import HarnessEvent, SandboxContext

# --------------------------------------------------------------------------
# 1. Protocol-level fake
# --------------------------------------------------------------------------


@dataclass
class FakeHarness:
    """A `CodingHarness` that returns scripted results without any I/O."""

    name: str = "fake"
    display_name: str = "Fake Harness"
    execution_mode: str = "sandbox_exec"
    description: str = "Deterministic in-memory harness used in tests."
    binary_name: str = "fake-harness"
    fallback_binaries: tuple[str, ...] = ("fake-harness",)

    # Scripted behavior
    exit_code: int = 0
    summary: str = "Fake harness completed the task."
    error: str | None = None
    questions: list[str] = field(default_factory=list)
    files_changed: list[str] = field(default_factory=list)
    diff_summary: str | None = None
    progress_events: list[str] = field(default_factory=list)
    # Files to actually write into the worktree, as {relative_path: contents}
    writes: dict[str, str] = field(default_factory=dict)

    # Recorded calls, for assertions
    calls: list[dict[str, Any]] = field(default_factory=list)

    def build_command(
        self,
        *,
        goal: str,
        repo: str,
        task_id: str,
        mode: str = "execute",
        binary: str | None = None,
    ) -> list[str]:
        return [binary or self.binary_name, "-p", goal, "--mode", mode]

    def format_sandbox_command(
        self,
        *,
        goal: str,
        repo: str,
        task_id: str,
        mode: str = "execute",
        **_kwargs: Any,
    ) -> str:
        return " ".join(
            self.build_command(goal=goal, repo=repo, task_id=task_id, mode=mode)
        )

    def build_env(self) -> dict[str, str]:
        return {"FAKE_HARNESS": "1"}

    def parse_stream_line(self, line: str, task_id: str) -> HarnessEvent | None:
        if not line.strip():
            return None
        return HarnessEvent(
            task_id=task_id,
            harness=self.name,
            kind="progress",
            message=line.strip(),
        )

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
        self.calls.append(
            {
                "goal": goal,
                "repo": repo,
                "task_id": task_id,
                "mode": mode,
                "require_approval": require_approval,
                "session_id": session_id,
            }
        )

        branch = context.branch or f"agent/{task_id}"
        wt_path = str(context.worktree_dir or f"/workspace/.worktrees/{task_id}")

        for message in self.progress_events:
            ev = HarnessEvent(
                task_id=task_id, harness=self.name, kind="progress", message=message
            )
            if on_event:
                on_event(ev)

        # Actually write requested files so downstream git/diff logic sees real state.
        if self.writes and context.worktree_dir is not None:
            for rel_path, contents in self.writes.items():
                target = context.worktree_dir / rel_path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(contents)

        if self.error or self.exit_code != 0:
            message = self.error or f"{self.display_name} exited with {self.exit_code}."
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
                exit_code=self.exit_code or 1,
                summary=message,
                response_text=message,
                error=message,
                branch=branch,
                worktree_path=wt_path,
                harness=self.name,
                events=[*self.progress_events, message],
            )

        if mode == "plan":
            questions = self.questions or [
                f"Should '{goal}' in {repo} include automated tests?"
            ]
            return WorkerExecutionResult(
                exit_code=0,
                summary=self.summary,
                response_text=self.summary,
                claude_session_id=session_id or f"fake-sess-{task_id}",
                questions=questions,
                awaiting_input=True,
                branch=branch,
                worktree_path=wt_path,
                harness=self.name,
                events=[*self.progress_events, self.summary],
            )

        return WorkerExecutionResult(
            exit_code=0,
            summary=self.summary,
            response_text=self.summary,
            claude_session_id=session_id or f"fake-sess-{task_id}",
            files_changed=list(self.files_changed) or list(self.writes.keys()),
            awaiting_approval=require_approval,
            diff_summary=self.diff_summary
            or (f"{len(self.writes)} file(s) changed." if self.writes else None),
            pending_action=(
                f"git commit and push branch {branch} and open PR"
                if require_approval
                else None
            ),
            branch=branch,
            worktree_path=wt_path,
            harness=self.name,
            events=[*self.progress_events, self.summary],
        )


# --------------------------------------------------------------------------
# 2. Network-boundary fakes (drive the REAL harness classes)
# --------------------------------------------------------------------------


def exec_transport(
    *,
    stdout: str = "",
    stderr: str = "",
    exit_code: int = 0,
    status_code: int = 200,
    body: Any | None = None,
    raise_exc: Exception | None = None,
    expect_port: str = "8080",
    requests: list[httpx.Request] | None = None,
) -> httpx.MockTransport:
    """Build a `MockTransport` standing in for the sandbox `/exec` endpoint.

    Args:
        stdout / stderr / exit_code: Shape of a normal `/exec` JSON response.
        status_code: Use >=400 to simulate 502 / 401 / "No healthy upstream".
        body: Override the response body entirely (e.g. to return invalid JSON).
        raise_exc: Raise instead of responding, to simulate a timeout or DNS failure.
        expect_port: Asserted value of the `X-Sandbox-Port` routing header.
        requests: Optional list that captures every request for later assertions.
    """

    async def _handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        assert request.headers.get("X-Sandbox-Port") == expect_port, (
            f"expected X-Sandbox-Port={expect_port}, "
            f"got {request.headers.get('X-Sandbox-Port')}"
        )
        assert request.headers.get("X-Sandbox-Routing-Token"), (
            "missing X-Sandbox-Routing-Token header"
        )
        assert request.headers.get("Authorization", "").startswith("Bearer "), (
            "missing bearer Authorization header"
        )

        if raise_exc is not None:
            raise raise_exc

        if status_code >= 400:
            return httpx.Response(status_code, text="No healthy upstream")

        if body is not None:
            if isinstance(body, str):
                return httpx.Response(status_code, text=body)
            return httpx.Response(status_code, json=body)

        return httpx.Response(
            status_code,
            json={"stdout": stdout, "stderr": stderr, "exit_code": exit_code},
        )

    return httpx.MockTransport(_handler)


def claude_stream_json(
    *,
    result_text: str,
    is_error: bool = False,
    assistant_text: str | None = None,
    session_id: str = "claude-sess-test",
) -> str:
    """Render a realistic Claude Code `stream-json` stdout payload."""
    lines = [
        json.dumps({"type": "system", "subtype": "init", "session_id": session_id}),
        json.dumps({"type": "tool_use", "name": "Bash"}),
    ]
    if assistant_text:
        lines.append(json.dumps({"type": "assistant", "text": assistant_text}))
    lines.append(
        json.dumps({"type": "result", "is_error": is_error, "result": result_text})
    )
    return "\n".join(lines)


def claude_plan_json(
    *,
    plan_summary: str,
    questions: list[str],
    files: list[str] | None = None,
    session_id: str = "claude-sess-plan",
) -> str:
    """Render a Claude Code plan-mode JSON payload matching `PLAN_OUTPUT_SCHEMA`."""
    return json.dumps(
        {
            "session_id": session_id,
            "structured_output": {
                "plan_summary": plan_summary,
                "steps": ["Inspect repository", "Apply change", "Run tests"],
                "questions": questions,
                "files_to_modify": files or [],
            },
        }
    )


def antigravity_stream_json(
    *,
    response_text: str,
    status: str = "SUCCESS",
    conversation_id: str = "agy-conv-test",
) -> str:
    """Render a realistic Antigravity `stream-json` stdout payload."""
    return "\n".join(
        [
            json.dumps({"event": "init", "conversation_id": conversation_id}),
            json.dumps(
                {
                    "event": "step_update",
                    "step_update": {
                        "conversation_id": conversation_id,
                        "step_type": "tool",
                        "tool_name": "view_file",
                    },
                }
            ),
            json.dumps(
                {
                    "event": "result",
                    "result": {
                        "conversation_id": conversation_id,
                        "status": status,
                        "response": response_text,
                    },
                }
            ),
        ]
    )


def a2a_transport(
    *,
    summary_text: str,
    task_id: str = "task-1",
    agent_name: str = "horizon_sandbox_agent",
    base_url: str = "https://mock-sandbox-lb.aiplatform.googleapis.com/a2a/horizon",
    expect_port: str = "8081",
    card_status: int = 200,
    requests: list[httpx.Request] | None = None,
) -> httpx.MockTransport:
    """Build a `MockTransport` serving an A2A agent-card plus a completed task."""

    async def _handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        assert request.headers.get("X-Sandbox-Port") == expect_port
        assert request.headers.get("X-Sandbox-Routing-Token")

        path = request.url.path
        if path.endswith("agent-card.json") or path.endswith("agent.json"):
            if card_status != 200:
                return httpx.Response(card_status, text="agent card unavailable")
            return httpx.Response(
                200,
                json={
                    "name": agent_name,
                    "description": "ADK Long Horizon harness (test double).",
                    "url": base_url,
                    "version": "1.0.0",
                    "capabilities": {"streaming": False},
                    "defaultInputModes": ["text/plain"],
                    "defaultOutputModes": ["text/plain"],
                    "skills": [],
                },
            )

        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": "1",
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


def sandbox_connection(
    *,
    sandbox_name: str = "voice-worker-test",
    lb_host: str = "mock-sandbox-lb.aiplatform.googleapis.com",
) -> dict[str, str]:
    """Connection info for `SandboxWorker(connection=...)`, bypassing Vertex resolution."""
    return {
        "sandbox_name": sandbox_name,
        "lb_host": lb_host,
        "routing_token": "test-routing-token",
        "sandbox_token": "test-sandbox-token",
    }


def sandbox_transport(
    *,
    exec_stdout: str = "",
    exec_stderr: str = "",
    exec_exit_code: int = 0,
    agent_card_status: int = 200,
    a2a_summary: str = "Horizon finished the task.",
    requests: list[httpx.Request] | None = None,
) -> httpx.MockTransport:
    """Transport serving BOTH the `/exec` endpoint and the A2A agent-card / task endpoints.

    Useful for `SandboxWorker(http_transport=...)`, where provisioning health
    probes and harness execution share one client.
    """

    async def _handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        path = request.url.path

        if path.endswith("agent-card.json") or path.endswith("agent.json"):
            if agent_card_status != 200:
                return httpx.Response(agent_card_status, text="agent card unavailable")
            return httpx.Response(
                200,
                json={
                    "name": "horizon_sandbox_agent",
                    "description": "ADK Long Horizon harness (test double).",
                    "url": str(request.url).replace("/.well-known/agent-card.json", ""),
                    "version": "1.0.0",
                    "capabilities": {"streaming": False},
                    "defaultInputModes": ["text/plain"],
                    "defaultOutputModes": ["text/plain"],
                    "skills": [],
                },
            )

        if path.endswith("/exec"):
            return httpx.Response(
                200,
                json={
                    "stdout": exec_stdout,
                    "stderr": exec_stderr,
                    "exit_code": exec_exit_code,
                },
            )

        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": "1",
                "result": {
                    "kind": "task",
                    "id": "task-a2a",
                    "contextId": "ctx-a2a",
                    "status": {
                        "state": "completed",
                        "message": {
                            "kind": "message",
                            "messageId": "msg-a2a",
                            "role": "agent",
                            "parts": [{"kind": "text", "text": a2a_summary}],
                        },
                    },
                },
            },
        )

    return httpx.MockTransport(_handler)


def install_worker(monkeypatch: Any, worker: Any) -> Any:
    """Install `worker` as the process-wide worker backend singleton."""
    import app.workers.factory as factory_mod

    monkeypatch.setattr(factory_mod, "_active_worker", worker)
    return worker


def register_fake_harnesses(*names: str, **kwargs: Any) -> dict[str, FakeHarness]:
    """Register a `FakeHarness` under each given canonical harness name.

    Overrides the real `claude` / `horizon` / `antigravity` entries so tests
    about task lifecycle, worktrees, and routing never touch harness internals.
    """
    from app.workers.harnesses.registry import get_harness_registry

    reg = get_harness_registry()
    created: dict[str, FakeHarness] = {}
    for name in names:
        fake = FakeHarness(name=name, display_name=kwargs.pop("display_name", None) or name.title(), **kwargs)
        reg.register_harness(fake)
        created[name] = fake
    return created


# --------------------------------------------------------------------------
# 3. Downstream REST Integration Transport Fake (Spotify, Workspace, Slack, GitHub)
# --------------------------------------------------------------------------


def integrations_transport(
    *,
    requests: list[httpx.Request] | None = None,
) -> httpx.MockTransport:
    """Build a `MockTransport` serving deterministic responses for Spotify, Google Workspace, Slack, and GitHub REST APIs.

    Allows `app/tools/integration_tools.py` and `app/auth.py` to execute 100% real HTTP code paths
    in unit tests and evaluations without embedding test branches or canned strings in `app/`.
    """

    async def _handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)

        host = request.url.host
        path = request.url.path
        method = request.method.upper()

        # 1. Spotify Web API
        if host == "api.spotify.com":
            if path == "/v1/me":
                return httpx.Response(
                    200,
                    json={"display_name": "Sonar Test User", "product": "premium"},
                )
            if path == "/v1/me/player/currently-playing":
                return httpx.Response(
                    200,
                    json={
                        "is_playing": True,
                        "item": {
                            "name": "Awake",
                            "artists": [{"name": "Tycho"}],
                        },
                    },
                )
            if path == "/v1/search":
                q = request.url.params.get("q", "Deep Focus Coding")
                return httpx.Response(
                    200,
                    json={
                        "tracks": {
                            "items": [
                                {
                                    "uri": "spotify:track:awake",
                                    "name": q,
                                    "artists": [{"name": "Tycho"}],
                                }
                            ]
                        },
                        "playlists": {
                            "items": [
                                {
                                    "uri": "spotify:playlist:focus",
                                    "name": f"{q} Playlist",
                                }
                            ]
                        },
                    },
                )
            return httpx.Response(204)

        # 2. Google OAuth2 Userinfo & Google Calendar / Drive
        if host == "www.googleapis.com":
            if path == "/oauth2/v2/userinfo":
                return httpx.Response(200, json={"email": "dev@sonar.local", "name": "Sonar Dev"})
            if path.endswith("/events/quickAdd") and method == "POST":
                text = request.url.params.get("text", "Architecture Sync")
                return httpx.Response(200, json={"summary": text})
            if path.endswith("/calendars/primary/events"):
                return httpx.Response(
                    200,
                    json={
                        "items": [
                            {
                                "summary": "Architecture Review",
                                "start": {"dateTime": "2026-09-22T11:00:00-05:00"},
                            },
                            {
                                "summary": "Sprint Triage",
                                "start": {"dateTime": "2026-09-22T14:00:00-05:00"},
                            },
                        ]
                    },
                )
            if path == "/drive/v3/files":
                q = request.url.params.get("q", "")
                doc_name = "Token Rotation Architecture Spec"
                if "contains '" in q:
                    doc_name = q.split("contains '", 1)[1].split("'", 1)[0] or doc_name
                return httpx.Response(
                    200,
                    json={
                        "files": [
                            {
                                "id": "doc-1",
                                "name": doc_name,
                                "mimeType": "application/vnd.google-apps.document",
                                "modifiedTime": "2026-09-22T10:00:00Z",
                                "description": "Specifies Redis-backed JWT rotation with a 15-minute grace window and zero-downtime key rollover.",
                            }
                        ]
                    },
                )

        # 3. Gmail REST API
        if host == "gmail.googleapis.com":
            if path.endswith("/drafts") and method == "POST":
                return httpx.Response(200, json={"id": "draft-1"})
            if path.endswith("/users/me/messages"):
                return httpx.Response(
                    200,
                    json={"messages": [{"id": "m1"}, {"id": "m2"}]},
                )
            if "/users/me/messages/" in path:
                mid = path.rsplit("/", 1)[-1]
                if mid == "m1":
                    return httpx.Response(
                        200,
                        json={
                            "payload": {
                                "headers": [
                                    {"name": "Subject", "value": "Auth service rollout update"},
                                    {"name": "From", "value": "Alex Chen"},
                                ]
                            }
                        },
                    )
                return httpx.Response(
                    200,
                    json={
                        "payload": {
                            "headers": [
                                {"name": "Subject", "value": "Q3 latency benchmarks"},
                                {"name": "From", "value": "Maya Patel"},
                            ]
                        }
                    },
                )

        # 4. Slack Web API
        if host == "slack.com":
            if path == "/api/auth.test":
                return httpx.Response(
                    200,
                    json={"ok": True, "team_id": "T12345", "team": "SonarEng", "user": "sonar-bot"},
                )
            if path == "/api/conversations.list":
                return httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "channels": [
                            {"id": "C01", "name": "eng-alerts"},
                            {"id": "C02", "name": "release-ops"},
                            {"id": "C03", "name": "general"},
                        ],
                    },
                )
            if path == "/api/chat.postMessage":
                return httpx.Response(200, json={"ok": True})
            if path == "/api/conversations.history":
                return httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "messages": [
                            {
                                "text": "DevOps confirmed the staging canary passed all smoke checks and is ready for production promotion."
                            }
                        ],
                    },
                )

        # 5. GitHub REST API
        if host == "api.github.com":
            if path == "/user":
                return httpx.Response(200, json={"login": "sonar-dev"})
            if path.endswith("/branches"):
                return httpx.Response(
                    200,
                    json=[
                        {"name": "main"},
                        {"name": "feat/redis-jwt-rotation"},
                        {"name": "fix/oauth-refresh"},
                    ],
                )
            if path.endswith("/actions/runs"):
                return httpx.Response(
                    200,
                    json={
                        "workflow_runs": [
                            {
                                "name": "CI Unit & Integration",
                                "conclusion": "success",
                                "status": "completed",
                                "head_branch": "feat/redis-jwt-rotation",
                            }
                        ]
                    },
                )
            if path.endswith("/issues") and not path.startswith("/search/"):
                return httpx.Response(
                    200,
                    json=[
                        {"number": 42, "title": "Redis connection pool tuning"},
                        {"number": 45, "title": "OAuth token refresh"},
                    ],
                )
            if path == "/search/issues":
                return httpx.Response(
                    200,
                    json={
                        "items": [
                            {
                                "number": 18,
                                "title": "Add Redis JWT rotation with 15m grace window",
                                "state": "open",
                                "repository_url": "https://api.github.com/repos/google/adk-python",
                            }
                        ]
                    },
                )
            if "/pulls/" in path:
                pr_num = int(path.rsplit("/", 1)[-1])
                return httpx.Response(
                    200,
                    json={
                        "number": pr_num,
                        "title": "Add Redis JWT rotation with 15m grace window",
                        "state": "open",
                        "user": {"login": "sonar-dev"},
                        "head": {"label": "sonar-dev:feat/redis-jwt-rotation"},
                    },
                )
            if path.endswith("/pulls"):
                return httpx.Response(
                    200,
                    json=[
                        {
                            "number": 18,
                            "title": "Add Redis JWT rotation with 15m grace window",
                            "state": "open",
                            "user": {"login": "sonar-dev"},
                        }
                    ],
                )

        return httpx.Response(200, json={"ok": True})

    return httpx.MockTransport(_handler)

