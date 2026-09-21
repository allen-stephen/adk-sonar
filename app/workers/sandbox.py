"""Vertex Agent Platform Sandbox worker backend supporting configurable coding harnesses."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from typing import Any

from app.workers.base import WorkerBackend, WorkerExecutionResult
from app.workers.harnesses import (
    CodingHarness,
    HarnessEvent,
    HarnessNotProvisionedError,
    SandboxContext,
    SandboxProvisioner,
    get_coding_harness,
    get_sandbox_provisioner,
)
from app.workers.harnesses.base import ensure_repo_and_worktree

logger = logging.getLogger(__name__)


class SandboxWorker(WorkerBackend):
    """Executes configurable coding harnesses in a persistent Vertex Agent Platform Sandbox.

    Reattaches to the user's running sandbox container (TTL default 14 days),
    preserving git clones, installed packages, and harness session history
    across individual task runs.

    Multiple concurrent tasks (of the same or different harnesses) run inside the
    same user sandbox via isolated per-task git worktrees (`.worktrees/{repo}/{task_id}`
    on branch `agent/{task_id}`) and task-scoped session IDs.
    """

    def __init__(
        self,
        *,
        project_id: str | None = None,
        location: str | None = None,
        mock_mode: bool | None = None,
        default_harness: str | None = None,
        provisioner: SandboxProvisioner | None = None,
    ) -> None:
        self.project_id = project_id or os.getenv(
            "GOOGLE_CLOUD_PROJECT", "agents-cli-test-dev-qi5zi1"
        )
        self.location = location or os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1")
        self.default_harness = default_harness
        self._provisioner = provisioner

        if mock_mode is None:
            if os.getenv("PYTEST_CURRENT_TEST"):
                self.mock_mode = True
            else:
                self.mock_mode = os.getenv("MOCK_REMOTE_RUNNER", "false").lower() in {
                    "true",
                    "1",
                    "yes",
                }
        else:
            self.mock_mode = mock_mode

        self._running_tasks: dict[str, Any] = {}

    @property
    def provisioner(self) -> SandboxProvisioner:
        return self._provisioner or get_sandbox_provisioner()

    async def _ensure_sandbox(self, user_id: str = "default_user") -> dict[str, str]:
        """Finds or provisions the persistent per-user Vertex Agent Engine Sandbox container."""
        sandbox_name = f"voice-worker-{user_id}"
        if self.mock_mode or os.getenv("PYTEST_CURRENT_TEST"):
            return {
                "sandbox_name": sandbox_name,
                "lb_host": "mock-sandbox-lb.aiplatform.googleapis.com",
                "routing_token": "mock-token",
                "sandbox_token": "mock-auth",
            }

        try:
            return await asyncio.to_thread(self._resolve_live_vertex_sandbox, user_id)
        except Exception as exc:
            logger.warning("Falling back to default sandbox info (%s)", exc)
            return {
                "sandbox_name": sandbox_name,
                "lb_host": f"{self.location}-aiplatform.googleapis.com",
                "routing_token": "live-token",
                "sandbox_token": "live-auth",
            }

    def _resolve_live_vertex_sandbox(self, user_id: str = "default_user") -> dict[str, str]:
        """Reattaches to or creates a live Vertex Agent Engine Sandbox in GCP dynamically."""
        import httpx
        import vertexai
        from app.auth import persist_env_vars

        sb_project = (
            os.getenv("SANDBOX_GCP_PROJECT")
            or os.getenv("GOOGLE_CLOUD_PROJECT")
            or self.project_id
        )
        sb_location = os.getenv("SANDBOX_GCP_LOCATION", "us-central1")
        caller_sa = os.getenv(
            "SANDBOX_CALLER_SA",
            f"lha-run@{sb_project}.iam.gserviceaccount.com",
        )
        pinned_sb = os.getenv("VERTEX_SANDBOX_RESOURCE_NAME", "").strip()
        display_name = f"voice-worker-{user_id}"

        client = vertexai.Client(
            project=sb_project,
            location=sb_location,
            http_options={"api_version": "v1beta1"},
        )
        sandbox_token = str(client.agent_engines.sandboxes.generate_access_token(caller_sa))

        def _is_reachable(lb_host: str, routing_token: str) -> bool:
            try:
                r = httpx.get(
                    f"https://{lb_host}/healthz",
                    headers={
                        "Authorization": f"Bearer {sandbox_token}",
                        "X-Sandbox-Routing-Token": routing_token,
                        "X-Sandbox-Port": "8080",
                    },
                    timeout=6.0,
                )
                return r.status_code == 200
            except Exception:
                return False

        if pinned_sb:
            try:
                sb_obj = client.agent_engines.sandboxes.get(name=pinned_sb)
                conn = getattr(sb_obj, "connection_info", None)
                if conn and conn.load_balancer_hostname and conn.routing_token:
                    if _is_reachable(conn.load_balancer_hostname, conn.routing_token):
                        return {
                            "sandbox_name": str(sb_obj.name),
                            "lb_host": str(conn.load_balancer_hostname),
                            "routing_token": str(conn.routing_token),
                            "sandbox_token": sandbox_token,
                        }
            except Exception:
                pass

        # Dynamically discover or resolve the parent ReasoningEngine and Sandbox Template
        engine_name = os.getenv("AGENT_ENGINE_RESOURCE_NAME", "").strip()
        if not engine_name:
            for eng in client.agent_engines.list():
                res = getattr(eng, "api_resource", None)
                if res and getattr(res, "name", None):
                    engine_name = str(res.name)
                    break

        template_name = os.getenv("SANDBOX_TEMPLATE_RESOURCE_NAME", "").strip()
        if not template_name and engine_name:
            tmpls = list(client.agent_engines.sandboxes.templates.list(name=engine_name))
            if tmpls:
                template_name = str(tmpls[-1].name)

        op = client.agent_engines.sandboxes.create(
            name=engine_name,
            config={
                "owner": user_id,
                "sandbox_environment_template": template_name,
                "display_name": display_name,
                "ttl": "1209600s",
            },
        )
        resp = op.response
        persist_env_vars({"VERTEX_SANDBOX_RESOURCE_NAME": str(resp.name)})
        return {
            "sandbox_name": str(resp.name),
            "lb_host": str(resp.connection_info.load_balancer_hostname),
            "routing_token": str(resp.connection_info.routing_token),
            "sandbox_token": sandbox_token,
        }

    def resolve_harness(
        self, harness: str | CodingHarness | None = None
    ) -> CodingHarness:
        if isinstance(harness, CodingHarness):
            return harness
        return get_coding_harness(harness or self.default_harness)

    async def execute_task(
        self,
        *,
        goal: str,
        repo: str,
        task_id: str,
        mode: str = "execute",
        require_approval: bool = False,
        harness: str | None = None,
        session_id: str | None = None,
        on_event: Callable[[HarnessEvent], None] | None = None,
    ) -> WorkerExecutionResult:
        harness_impl = self.resolve_harness(harness)
        sandbox_info = await self._ensure_sandbox()
        _, worktree_dir, branch_name = ensure_repo_and_worktree(repo, task_id)

        context = SandboxContext(
            user_id="default_user",
            sandbox_name=sandbox_info["sandbox_name"],
            lb_host=sandbox_info["lb_host"],
            routing_token=sandbox_info["routing_token"],
            sandbox_token=sandbox_info["sandbox_token"],
            worktree_dir=worktree_dir,
            branch=branch_name,
            mock_mode=self.mock_mode,
        )

        # Hard preflight gate: verify or provision the selected harness inside the sandbox
        # before allowing any task execution.
        try:
            await self.provisioner.ensure_provisioned(
                harness_impl,
                context,
                on_event=on_event,
                task_id=task_id,
            )
        except HarnessNotProvisionedError as exc:
            err_msg = str(exc)
            logger.error("Sandbox preflight gate blocked task %s: %s", task_id, err_msg)
            return WorkerExecutionResult(
                exit_code=1,
                summary=err_msg,
                response_text=err_msg,
                error=err_msg,
                branch=branch_name,
                worktree_path=str(worktree_dir),
                harness=harness_impl.name,
                events=[err_msg],
            )

        logger.info(
            "Executing task %s in Vertex Sandbox %s (harness=%s, mode=%s, branch=%s, worktree=%s)",
            task_id,
            context.sandbox_name,
            harness_impl.name,
            mode,
            branch_name,
            worktree_dir,
        )

        self._running_tasks[task_id] = {
            "harness": harness_impl.name,
            "sandbox_name": context.sandbox_name,
            "branch": branch_name,
            "worktree_dir": str(worktree_dir),
        }
        try:
            return await harness_impl.execute_in_sandbox(
                goal=goal,
                repo=repo,
                task_id=task_id,
                context=context,
                mode=mode,
                require_approval=require_approval,
                session_id=session_id,
                on_event=on_event,
            )
        finally:
            self._running_tasks.pop(task_id, None)

    async def cancel_task(self, task_id: str) -> bool:
        logger.info("Cancelling task %s in sandbox", task_id)
        self._running_tasks.pop(task_id, None)
        return True
