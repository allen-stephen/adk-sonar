"""Unified preflight health-check and sandbox provisioning manager for coding harnesses."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.workers.harnesses.base import CodingHarness, HarnessEvent, SandboxContext

logger = logging.getLogger(__name__)

HORIZON_GITHUB_SPEC = (
    "git+https://github.com/google/adk-samples.git"
    "#subdirectory=core/python/long-horizon-harness"
)


class HarnessNotProvisionedError(RuntimeError):
    """Raised when a coding harness fails preflight provisioning or health verification in the sandbox."""


@dataclass
class HarnessProvisionStatus:
    """Tracks provisioning state and health metadata for a (sandbox, harness) pair."""

    harness: str
    sandbox_name: str
    status: str = "unprovisioned"  # "unprovisioned" | "provisioning" | "ready" | "error"
    version: str | None = None
    source_url: str | None = None
    last_checked_at: float | None = None
    provisioned_at: float | None = None
    error: str | None = None
    commands_executed: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "harness": self.harness,
            "sandbox_name": self.sandbox_name,
            "status": self.status,
            "version": self.version,
            "source_url": self.source_url,
            "last_checked_at": self.last_checked_at,
            "provisioned_at": self.provisioned_at,
            "error": self.error,
            "commands_executed": list(self.commands_executed),
        }


def get_default_provision_recipe(harness_name: str, a2a_port: int = 8081) -> dict[str, Any]:
    """Return canonical provisioning recipe (install commands, daemon startup, source metadata) for a harness."""
    if harness_name == "horizon":
        from app.workers.harnesses.base import load_workspaces_manifest

        manifest_spec = (
            load_workspaces_manifest().get("sandbox", {}).get("horizon_package_spec")
        )
        pkg_spec = os.getenv("HORIZON_PACKAGE_SPEC") or manifest_spec or HORIZON_GITHUB_SPEC
        return {
            "version": "git:main@latest",
            "source_url": pkg_spec,
            "health_probe": f"GET http://127.0.0.1:{a2a_port}/.well-known/agent-card.json",
            "commands": [
                f'uv pip install --upgrade "{pkg_spec}"',
                (
                    f"LHA_ENVIRONMENT_BACKEND=local USE_IN_MEMORY_SESSION=true "
                    f"USE_IN_MEMORY_TASK_STORE=true APP_URL=http://127.0.0.1:{a2a_port} "
                    f"nohup uv run uvicorn horizon.fast_api_app:app --host 0.0.0.0 --port {a2a_port} "
                    f"> /tmp/horizon-a2a.log 2>&1 &"
                ),
            ],
        }
    if harness_name == "claude":
        return {
            "version": "cli:latest",
            "source_url": "https://www.npmjs.com/package/@anthropic-ai/claude-code",
            "health_probe": "claude --version",
            "commands": [
                "command -v claude >/dev/null 2>&1 || npm install -g @anthropic-ai/claude-code@latest",
            ],
        }
    if harness_name == "antigravity":
        return {
            "version": "cli:latest",
            "source_url": "https://github.com/google/antigravity",
            "health_probe": "agy --version",
            "commands": [
                "command -v agy >/dev/null 2>&1 || npm install -g @google/antigravity@latest",
            ],
        }
    return {
        "version": "latest",
        "source_url": None,
        "health_probe": "true",
        "commands": [],
    }


class SandboxProvisioner:
    """Manages single-flight preflight checks, GitHub package upgrades, and daemon startup inside Vertex Sandboxes."""

    def __init__(self) -> None:
        self._states: dict[tuple[str, str], HarnessProvisionStatus] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._simulated_failures: dict[str, str] = {}

    def _get_lock(self, key: tuple[str, str]) -> asyncio.Lock:
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]

    def get_status(
        self,
        harness_name: str,
        sandbox_name: str = "voice-worker-default_user",
    ) -> HarnessProvisionStatus:
        key = (sandbox_name, harness_name)
        if key not in self._states:
            recipe = get_default_provision_recipe(harness_name)
            self._states[key] = HarnessProvisionStatus(
                harness=harness_name,
                sandbox_name=sandbox_name,
                status="unprovisioned",
                version=recipe["version"],
                source_url=recipe["source_url"],
            )
        return self._states[key]

    def simulate_failure(self, harness_name: str, reason: str | None) -> None:
        """Configure or clear a simulated provisioning failure for testing."""
        if reason is None:
            self._simulated_failures.pop(harness_name, None)
        else:
            self._simulated_failures[harness_name] = reason

    async def _exec_in_sandbox(
        self,
        context: SandboxContext,
        command: str,
        env: dict[str, str] | None = None,
    ) -> tuple[int, str]:
        """Execute a provisioning or health-check shell command on Sandbox Port 8080 (/exec)."""
        if context.mock_mode:
            await asyncio.sleep(0.01)
            return 0, f"mock-exec ok: {command}"

        headers = context.build_sandbox_headers(port=context.exec_port)
        exec_url = f"https://{context.lb_host}/exec"
        async with httpx.AsyncClient(
            headers=headers,
            timeout=httpx.Timeout(120.0, connect=10.0),
        ) as client:
            resp = await client.post(
                exec_url,
                json={"command": command, "env": env or {}},
            )
            if resp.status_code >= 400:
                return resp.status_code, resp.text
            data = resp.json() if resp.content else {}
            out_text = data.get("output")
            if out_text is None:
                out_text = (data.get("stdout") or "") + (data.get("stderr") or "")
            return int(data.get("exit_code", 0)), str(out_text)

    async def check_health(
        self,
        harness: CodingHarness,
        context: SandboxContext,
    ) -> tuple[bool, str | None]:
        """Verify that a coding harness is installed and responsive inside the sandbox."""
        if harness.name in self._simulated_failures:
            return False, self._simulated_failures[harness.name]

        state = self.get_status(harness.name, context.sandbox_name)

        if context.mock_mode:
            # In mock mode, a container starts unprovisioned until provisioning runs at least once
            if state.provisioned_at is not None:
                return True, None
            return False, f"{harness.display_name} not yet provisioned in {context.sandbox_name}"

        if harness.execution_mode == "sandbox_a2a":
            headers = context.build_sandbox_headers(port=context.a2a_port)
            card_url = f"https://{context.lb_host}/a2a/horizon/.well-known/agent-card.json"
            if hasattr(harness, "get_sandbox_a2a_url"):
                base = harness.get_sandbox_a2a_url(context)
                card_url = f"{base}/.well-known/agent-card.json"
            try:
                async with httpx.AsyncClient(
                    headers=headers,
                    timeout=httpx.Timeout(5.0, connect=3.0),
                ) as client:
                    resp = await client.get(card_url)
                    if resp.status_code == 200:
                        return True, None
                    return False, f"A2A agent-card returned HTTP {resp.status_code}"
            except Exception as exc:
                return False, f"A2A endpoint unreachable on port {context.a2a_port}: {exc}"

        recipe = get_default_provision_recipe(harness.name, context.a2a_port)
        code, output = await self._exec_in_sandbox(context, recipe["health_probe"])
        if code == 0:
            return True, None
        return False, output or f"Health probe failed with exit code {code}"

    async def ensure_provisioned(
        self,
        harness: CodingHarness,
        context: SandboxContext | None = None,
        *,
        force_refresh: bool = False,
        on_event: Callable[[HarnessEvent], None] | None = None,
        task_id: str = "preflight",
    ) -> HarnessProvisionStatus:
        """Single-flight preflight gate: ensures the harness is provisioned with the latest version before execution."""
        ctx = context or SandboxContext()
        key = (ctx.sandbox_name, harness.name)
        lock = self._get_lock(key)

        async with lock:
            state = self.get_status(harness.name, ctx.sandbox_name)
            recipe = get_default_provision_recipe(harness.name, ctx.a2a_port)
            state.version = recipe["version"]
            state.source_url = recipe["source_url"]

            if not force_refresh and state.status == "ready":
                healthy, err = await self.check_health(harness, ctx)
                state.last_checked_at = time.time()
                if healthy:
                    return state
                logger.warning(
                    "Harness %s in %s became unhealthy (%s); re-provisioning.",
                    harness.name,
                    ctx.sandbox_name,
                    err,
                )

            state.status = "provisioning"
            state.error = None
            state.last_checked_at = time.time()

            provision_cmds: list[str] = list(recipe["commands"])
            if hasattr(harness, "build_provision_commands"):
                custom_cmds = harness.build_provision_commands()
                if custom_cmds:
                    provision_cmds = list(custom_cmds)

            start_msg = (
                f"Provisioning {harness.display_name} ({state.version}) in sandbox "
                f"{ctx.sandbox_name}..."
            )
            logger.info(start_msg)
            if on_event:
                on_event(
                    HarnessEvent(
                        task_id=task_id,
                        harness=harness.name,
                        kind="progress",
                        message=start_msg,
                    )
                )

            if harness.name in self._simulated_failures:
                reason = self._simulated_failures[harness.name]
                state.status = "error"
                state.error = reason
                err_msg = (
                    f"Failed to provision {harness.display_name} in sandbox "
                    f"{ctx.sandbox_name}: {reason}"
                )
                if on_event:
                    on_event(
                        HarnessEvent(
                            task_id=task_id,
                            harness=harness.name,
                            kind="error",
                            message=err_msg,
                        )
                    )
                raise HarnessNotProvisionedError(err_msg)

            env = harness.build_env() if hasattr(harness, "build_env") else {}
            executed: list[str] = []
            for cmd in provision_cmds:
                code, output = await self._exec_in_sandbox(ctx, cmd, env=env)
                executed.append(cmd)
                if code != 0:
                    state.status = "error"
                    state.error = f"Command failed ({code}): {output}"
                    raise HarnessNotProvisionedError(state.error)

            state.commands_executed = executed
            state.provisioned_at = time.time()

            healthy, err = await self.check_health(harness, ctx)
            state.last_checked_at = time.time()
            if not healthy:
                state.status = "error"
                state.error = err or "Post-provisioning health verification failed"
                raise HarnessNotProvisionedError(
                    f"{harness.display_name} failed post-provisioning verification: {state.error}"
                )

            state.status = "ready"
            state.error = None
            ready_msg = (
                f"{harness.display_name} ({state.version}) is provisioned and ready "
                f"in sandbox {ctx.sandbox_name}."
            )
            logger.info(ready_msg)
            if on_event:
                on_event(
                    HarnessEvent(
                        task_id=task_id,
                        harness=harness.name,
                        kind="progress",
                        message=ready_msg,
                    )
                )
            return state

    def warm_up_in_background(
        self,
        harness: CodingHarness,
        context: SandboxContext | None = None,
    ) -> asyncio.Task[HarnessProvisionStatus] | None:
        """Trigger eager non-blocking background provisioning when a user switches harnesses."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None

        async def _runner() -> HarnessProvisionStatus:
            try:
                return await self.ensure_provisioned(harness, context)
            except Exception as exc:
                logger.warning("Background warm-up for %s failed: %s", harness.name, exc)
                return self.get_status(
                    harness.name,
                    (context or SandboxContext()).sandbox_name,
                )

        task = loop.create_task(_runner(), name=f"warmup-{harness.name}")
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task


_provisioner: SandboxProvisioner | None = None


def get_sandbox_provisioner() -> SandboxProvisioner:
    global _provisioner
    if _provisioner is None:
        _provisioner = SandboxProvisioner()
    return _provisioner


def reset_sandbox_provisioner() -> None:
    global _provisioner
    _provisioner = None
