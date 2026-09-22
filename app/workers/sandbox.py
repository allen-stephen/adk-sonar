"""Vertex Agent Platform Sandbox worker backend supporting configurable coding harnesses."""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
from collections.abc import Callable
from pathlib import Path
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
from app.workers.harnesses.base import (
    _repo_slug,
    ensure_repo_and_worktree,
    exec_in_sandbox,
    load_workspaces_manifest,
)

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
        default_harness: str | None = None,
        provisioner: SandboxProvisioner | None = None,
        connection: dict[str, str] | None = None,
        http_transport: Any | None = None,
    ) -> None:
        self.project_id = project_id or os.getenv("GOOGLE_CLOUD_PROJECT", "")
        self.location = location or os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1")
        self.default_harness = default_harness
        self._provisioner = provisioner
        # Tests supply `connection` + `http_transport` to drive the real harness
        # code paths without reaching Google Cloud.
        self._connection = connection
        self._http_transport = http_transport
        self._running_tasks: dict[str, Any] = {}
        self._repo_locks: dict[str, asyncio.Lock] = {}

    def _get_repo_lock(self, repo_slug: str) -> asyncio.Lock:
        if repo_slug not in self._repo_locks:
            self._repo_locks[repo_slug] = asyncio.Lock()
        return self._repo_locks[repo_slug]

    @property
    def provisioner(self) -> SandboxProvisioner:
        return self._provisioner or get_sandbox_provisioner()

    async def _ensure_sandbox(self, user_id: str = "default_user") -> dict[str, str]:
        """Finds or provisions the persistent per-user Vertex Agent Engine Sandbox container."""
        if self._connection is not None:
            return self._connection

        if os.getenv("VERTEX_SANDBOX_LB_HOST") and os.getenv("VERTEX_SANDBOX_ROUTING_TOKEN"):
            return {
                "sandbox_name": os.getenv("VERTEX_SANDBOX_RESOURCE_NAME") or f"voice-worker-{user_id}",
                "lb_host": os.getenv("VERTEX_SANDBOX_LB_HOST", ""),
                "routing_token": os.getenv("VERTEX_SANDBOX_ROUTING_TOKEN", ""),
                "sandbox_token": os.getenv("VERTEX_SANDBOX_TOKEN", ""),
            }

        return await asyncio.to_thread(self._resolve_live_vertex_sandbox, user_id)

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

    async def _sync_remote_repository_in_sandbox(
        self,
        context: SandboxContext,
        repo: str,
        task_id: str,
        branch_name: str,
    ) -> None:
        """Fetch latest commits from GitHub and prepare an isolated worktree for the task.

        Uses an asyncio.Lock per repository to serialize the 500ms fetch step
        across concurrent tasks targeting the same repo, while letting their
        actual coding executions run in full parallelism.
        """
        slug = _repo_slug(repo)
        lock = self._get_repo_lock(slug)
        async with lock:
            manifest = load_workspaces_manifest()
            repo_spec = next(
                (
                    r
                    for r in manifest.get("repositories", [])
                    if isinstance(r, dict) and r.get("name") == slug
                ),
                None,
            )
            git_url = str((repo_spec or {}).get("git_url") or "").strip()
            default_branch = str((repo_spec or {}).get("branch") or "main").strip()

            token = (
                os.getenv("GITHUB_PERSONAL_ACCESS_TOKEN", "").strip()
                or os.getenv("GH_TOKEN", "").strip()
            )
            auth_header = (
                f'-c http.extraHeader="Authorization: Bearer {token}" '
                if token
                else ""
            )

            disable_remote_git = os.getenv("ORCHESTRATOR_DISABLE_HOST_GIT", "").lower() in {
                "1",
                "true",
                "yes",
            }
            if disable_remote_git or not git_url:
                setup_cmd = (
                    f"mkdir -p /workspace/{slug} /workspace/.worktrees && "
                    f"cd /workspace/{slug} && "
                    f"([ -d .git ] || (git init -b {default_branch} && "
                    f"git config user.email 'agent@sonar.local' && git config user.name 'ADK Sonar' && "
                    f"echo '# {slug}' > README.md && git add . && git commit -m 'init')) && "
                    f"git worktree prune >/dev/null 2>&1 || true; "
                    f"(git worktree add -B {shlex.quote(branch_name)} /workspace/.worktrees/{shlex.quote(task_id)} >/dev/null 2>&1 || "
                    f"mkdir -p /workspace/.worktrees/{shlex.quote(task_id)})"
                )
            else:
                setup_cmd = (
                    f"mkdir -p /workspace/{slug} /workspace/.worktrees && "
                    f"cd /workspace/{slug} && "
                    f"if [ -d .git ]; then "
                    f"  (git remote get-url origin >/dev/null 2>&1 || git remote add origin {shlex.quote(git_url)}) && "
                    f"  git config user.email 'agent@sonar.local' && git config user.name 'ADK Sonar' && "
                    f"  GIT_TERMINAL_PROMPT=0 git {auth_header} fetch --all --prune --quiet 2>/dev/null || true; "
                    f"else "
                    f"  git config user.email 'agent@sonar.local' && git config user.name 'ADK Sonar' && "
                    f"  (GIT_TERMINAL_PROMPT=0 git {auth_header} clone --quiet {shlex.quote(git_url)} . 2>/dev/null || "
                    f"   (git init -b {default_branch} && git remote add origin {shlex.quote(git_url)} && "
                    f"    GIT_TERMINAL_PROMPT=0 git {auth_header} fetch --all --prune --quiet 2>/dev/null || true)); "
                    f"fi && "
                    f"git worktree prune >/dev/null 2>&1 || true; "
                    f"(git worktree add -B {shlex.quote(branch_name)} /workspace/.worktrees/{shlex.quote(task_id)} origin/{default_branch} >/dev/null 2>&1 || "
                    f" git worktree add -B {shlex.quote(branch_name)} /workspace/.worktrees/{shlex.quote(task_id)} HEAD >/dev/null 2>&1 || "
                    f" mkdir -p /workspace/.worktrees/{shlex.quote(task_id)})"
                )

            await exec_in_sandbox(context, setup_cmd, timeout_s=30.0)

    async def _push_task_branch_to_remote(
        self,
        context: SandboxContext,
        task_id: str,
        branch_name: str,
    ) -> None:
        """Push the completed task's git branch to origin so changes are preserved on GitHub."""
        token = (
            os.getenv("GITHUB_PERSONAL_ACCESS_TOKEN", "").strip()
            or os.getenv("GH_TOKEN", "").strip()
        )
        auth_header = (
            f'-c http.extraHeader="Authorization: Bearer {token}" '
            if token
            else ""
        )
        push_cmd = (
            f"cd /workspace/.worktrees/{shlex.quote(task_id)} 2>/dev/null && "
            f"GIT_TERMINAL_PROMPT=0 git {auth_header} push -u origin {shlex.quote(branch_name)} --quiet 2>/dev/null || true"
        )
        await exec_in_sandbox(context, push_cmd, timeout_s=30.0)

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
        _, worktree_dir, branch_name = ensure_repo_and_worktree(repo, task_id)
        use_host_worktree = os.getenv("ORCHESTRATOR_DISABLE_HOST_GIT", "").lower() in {
            "1",
            "true",
            "yes",
        }
        sandbox_wt_path = (
            str(worktree_dir)
            if use_host_worktree
            else f"/workspace/.worktrees/{task_id}"
        )

        try:
            sandbox_info = await self._ensure_sandbox()
        except Exception as exc:
            err_msg = f"Failed to resolve or provision Vertex Sandbox: {exc}"
            logger.error("Sandbox resolution failed for task %s: %s", task_id, err_msg)
            return WorkerExecutionResult(
                exit_code=1,
                summary=err_msg,
                response_text=err_msg,
                error=err_msg,
                branch=branch_name,
                worktree_path=sandbox_wt_path,
                harness=harness_impl.name,
                events=[err_msg],
            )

        context = SandboxContext(
            user_id="default_user",
            sandbox_name=sandbox_info["sandbox_name"],
            lb_host=sandbox_info["lb_host"],
            routing_token=sandbox_info["routing_token"],
            sandbox_token=sandbox_info["sandbox_token"],
            worktree_dir=Path(sandbox_wt_path),
            branch=branch_name,
            http_transport=self._http_transport,
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
                worktree_path=sandbox_wt_path,
                harness=harness_impl.name,
                events=[err_msg],
            )

        # Sync base repository with latest remote commits and checkout task worktree
        await self._sync_remote_repository_in_sandbox(
            context,
            repo=repo,
            task_id=task_id,
            branch_name=branch_name,
        )

        logger.info(
            "Executing task %s in Vertex Sandbox %s (harness=%s, mode=%s, branch=%s, worktree=%s)",
            task_id,
            context.sandbox_name,
            harness_impl.name,
            mode,
            branch_name,
            sandbox_wt_path,
        )

        run_dir = f"/workspace/.sonar/runs/{task_id}"
        self._running_tasks[task_id] = {
            "harness": harness_impl.name,
            "sandbox_name": context.sandbox_name,
            "branch": branch_name,
            "worktree_dir": sandbox_wt_path,
            "run_dir": run_dir,
            "context": context,
        }
        try:
            result = await harness_impl.execute_in_sandbox(
                goal=goal,
                repo=repo,
                task_id=task_id,
                context=context,
                mode=mode,
                require_approval=require_approval,
                session_id=session_id,
                on_event=on_event,
            )
            if (
                result.exit_code == 0
                and mode == "execute"
                and not use_host_worktree
            ):
                await self._push_task_branch_to_remote(context, task_id, branch_name)
            return result
        finally:
            self._running_tasks.pop(task_id, None)

    async def cancel_task(self, task_id: str) -> bool:
        logger.info("Cancelling task %s in sandbox", task_id)
        info = self._running_tasks.pop(task_id, None)
        if info:
            from app.workers.harnesses.base import (
                SandboxContext,
                cancel_detached_command,
            )

            context = info.get("context") or SandboxContext(
                user_id="default_user",
                sandbox_name=info.get("sandbox_name") or "default_sandbox",
                http_transport=self._http_transport,
            )
            run_handle = {
                "run_id": task_id,
                "run_dir": info.get("run_dir") or f"/workspace/.sonar/runs/{task_id}",
            }
            try:
                await cancel_detached_command(context, run_handle)
            except Exception as exc:
                logger.debug("Remote cancel command failed: %s", exc)
        return True

