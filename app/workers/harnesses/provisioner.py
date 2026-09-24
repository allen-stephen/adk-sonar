"""Unified preflight health-check and sandbox provisioning manager for coding harnesses."""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.workers.harnesses.base import (
    CodingHarness,
    HarnessEvent,
    SandboxContext,
    exec_in_sandbox,
)

logger = logging.getLogger(__name__)

HORIZON_GITHUB_SPEC = (
    "git+https://github.com/google/adk-samples.git"
    "#subdirectory=core/python/long-horizon-harness"
)

# Sandbox images differ in ways that silently break provisioning: the managed
# shell sandbox runs as an unprivileged user with no virtualenv (so a bare
# `uv pip install` fails with "No virtual environment found" and `npm install -g`
# fails with EACCES on /usr/local), while a root image or a prebuilt custom
# image needs neither workaround. Rather than hardcode one layout, the container
# is probed once and the result feeds every command we generate.
DEFAULT_SANDBOX_VENV_DIRNAME = ".sonar-venv"
FALLBACK_SANDBOX_VENV_PATH = f"/workspace/{DEFAULT_SANDBOX_VENV_DIRNAME}"
FALLBACK_SANDBOX_NPM_PREFIX = "$HOME/.local"
PROBED_HARNESS_BINARIES = ("claude", "agy", "horizon", "uv", "npm", "git")

# POSIX sh (the sandbox /exec shell is dash, not bash) emitting `key=value` lines.
SANDBOX_PROBE_COMMAND = r"""
printf 'uid=%s\n' "$(id -u 2>/dev/null)"
printf 'home=%s\n' "$HOME"
printf 'virtual_env=%s\n' "${VIRTUAL_ENV:-}"
_npm_prefix="$(npm config get prefix 2>/dev/null)"
printf 'npm_prefix=%s\n' "$_npm_prefix"
if [ -n "$_npm_prefix" ] && [ -w "$_npm_prefix" ]; then
  printf 'npm_prefix_writable=1\n'
else
  printf 'npm_prefix_writable=0\n'
fi
for _d in /workspace "$HOME" /opt /usr/local; do
  [ -w "$_d" ] && printf 'writable=%s\n' "$_d"
done
for _b in claude agy horizon uv npm git; do
  command -v "$_b" >/dev/null 2>&1 && printf 'binary=%s\n' "$_b"
done
exit 0
""".strip()


def parse_sandbox_probe(output: str) -> dict[str, Any]:
    """Parse `key=value` lines emitted by :data:`SANDBOX_PROBE_COMMAND`."""
    parsed: dict[str, Any] = {"writable": [], "binaries": []}
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key == "writable":
            if value:
                parsed["writable"].append(value)
        elif key == "binary":
            if value:
                parsed["binaries"].append(value)
        elif key == "uid":
            try:
                parsed["uid"] = int(value)
            except ValueError:
                continue
        elif key == "npm_prefix_writable":
            parsed["npm_prefix_writable"] = value == "1"
        else:
            parsed[key] = value
    return parsed


@dataclass
class SandboxRuntimeProfile:
    """Resolved Python/Node execution layout for one sandbox container.

    Built by :func:`resolve_sandbox_runtime_profile` from, in descending
    priority: environment variables, the `sandbox:` section of the workspaces
    manifest, a live probe of the container, then built-in fallbacks.
    """

    venv_path: str = FALLBACK_SANDBOX_VENV_PATH
    # None means the container's existing npm prefix is already writable, so no
    # `npm config set prefix` override is needed (e.g. a root container).
    npm_prefix: str | None = FALLBACK_SANDBOX_NPM_PREFIX
    is_root: bool = False
    reuses_existing_venv: bool = False
    preinstalled: tuple[str, ...] = ()
    probed: bool = False

    @property
    def python_bin(self) -> str:
        """Absolute interpreter path, used to launch daemons without `uv run`."""
        return f"{self.venv_path}/bin/python"

    def has_binary(self, name: str) -> bool:
        return name in self.preinstalled

    def path_prelude(self) -> str:
        """`export PATH=...;` prefix exposing venv and npm-prefix binaries."""
        segments = [f"{self.venv_path}/bin"]
        if self.npm_prefix:
            segments.insert(0, f"{self.npm_prefix}/bin")
        return f'export PATH="{":".join(segments)}:$PATH"; '

    def venv_bootstrap_command(self) -> str:
        """Idempotent command creating the shared virtualenv if absent."""
        return (
            f'mkdir -p "$(dirname "{self.venv_path}")" && '
            f'([ -x "{self.python_bin}" ] || uv venv "{self.venv_path}")'
        )

    def uv_pip_install(self, specs: str | Sequence[str]) -> str:
        """`uv pip install` into the resolved virtualenv, one quoted spec each.

        Quoting is not cosmetic: an unquoted `pkg>=1.0` is parsed by the shell
        as an output redirection, which creates a file named `=1.0` and
        installs nothing.
        """
        quoted = " ".join(shlex.quote(str(s)) for s in _as_sequence(specs))
        return f'VIRTUAL_ENV="{self.venv_path}" uv pip install --upgrade {quoted}'

    def uv_tool_install(self, tools: str | Sequence[str]) -> str:
        """`uv tool install` (venv-independent; uv manages its own tool envs)."""
        quoted = " ".join(shlex.quote(str(t)) for t in _as_sequence(tools))
        return f"uv tool install --upgrade {quoted}"

    def npm_global_install(self, packages: str | Sequence[str]) -> str:
        """`npm install -g`, redirecting the prefix only when required."""
        quoted = " ".join(shlex.quote(str(p)) for p in _as_sequence(packages))
        if not self.npm_prefix:
            return f"npm install -g {quoted}"
        return (
            f'mkdir -p "{self.npm_prefix}/bin" && '
            f'npm config set prefix "{self.npm_prefix}" >/dev/null 2>&1; '
            f"npm install -g {quoted}"
        )

    def agents_cli_setup_command(self) -> str:
        """Install `google-agents-cli` bundled ADK skills across all sandbox harnesses."""
        return (
            f"{self.path_prelude()}"
            "agents-cli setup --skip-auth --agent all || "
            "uvx google-agents-cli setup --skip-auth --agent all"
        )

    def skills_install(self, skill_spec: str) -> str:
        """Install an agent skill spec globally across all sandbox harnesses via `npx -y skills add`."""
        return (
            f"{self.path_prelude()}"
            f"npx -y skills add {skill_spec.strip()} -g -a '*' -y"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "venv_path": self.venv_path,
            "npm_prefix": self.npm_prefix,
            "is_root": self.is_root,
            "reuses_existing_venv": self.reuses_existing_venv,
            "preinstalled": list(self.preinstalled),
            "probed": self.probed,
        }


def _as_sequence(value: str | Sequence[str]) -> list[str]:
    return [value] if isinstance(value, str) else list(value)


def _manifest_sandbox_section() -> dict[str, Any]:
    from app.workers.harnesses.base import load_workspaces_manifest

    section = load_workspaces_manifest().get("sandbox") or {}
    return section if isinstance(section, dict) else {}


def resolve_sandbox_runtime_profile(
    probe: dict[str, Any] | None = None,
) -> SandboxRuntimeProfile:
    """Resolve the sandbox execution layout from config, then a container probe.

    Explicit configuration always wins, so a probe never overrides a deliberate
    choice; the probe only fills in what was left unspecified.
    """
    manifest = _manifest_sandbox_section()
    configured_venv = (
        os.getenv("SANDBOX_VENV_PATH", "").strip()
        or str(manifest.get("venv_path") or "").strip()
    )
    configured_npm = (
        os.getenv("SANDBOX_NPM_PREFIX", "").strip()
        or str(manifest.get("npm_prefix") or "").strip()
    )

    probe = probe or {}
    home = str(probe.get("home") or "").strip()
    writable = list(probe.get("writable") or [])
    existing_venv = str(probe.get("virtual_env") or "").strip()

    if configured_venv:
        venv_path, reuses = configured_venv, False
    elif existing_venv:
        # A prebuilt image that ships its own environment: install into it
        # rather than shadowing it with a second one.
        venv_path, reuses = existing_venv, True
    else:
        candidates = [f"/workspace/{DEFAULT_SANDBOX_VENV_DIRNAME}"] if not writable else [
            f"{d.rstrip('/')}/{DEFAULT_SANDBOX_VENV_DIRNAME}"
            for d in writable
            if d in {"/workspace", home}
        ]
        venv_path = (candidates or [FALLBACK_SANDBOX_VENV_PATH])[0]
        reuses = False

    if configured_npm:
        npm_prefix: str | None = configured_npm
    elif probe.get("npm_prefix_writable"):
        npm_prefix = None
    elif home:
        npm_prefix = f"{home}/.local"
    else:
        npm_prefix = FALLBACK_SANDBOX_NPM_PREFIX

    return SandboxRuntimeProfile(
        venv_path=venv_path,
        npm_prefix=npm_prefix,
        is_root=probe.get("uid") == 0,
        reuses_existing_venv=reuses,
        preinstalled=tuple(probe.get("binaries") or ()),
        probed=bool(probe),
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


def get_default_provision_recipe(
    harness_name: str,
    a2a_port: int = 8081,
    profile: SandboxRuntimeProfile | None = None,
) -> dict[str, Any]:
    """Return canonical provisioning recipe (install commands, daemon startup, source metadata) for a harness.

    `profile` describes the target container's Python/Node layout; when omitted
    it is resolved from configuration alone (no live probe).
    """
    prof = profile or resolve_sandbox_runtime_profile()

    if harness_name == "horizon":
        manifest_spec = _manifest_sandbox_section().get("horizon_package_spec")
        pkg_spec = os.getenv("HORIZON_PACKAGE_SPEC") or manifest_spec or HORIZON_GITHUB_SPEC
        proj_id = (
            os.getenv("SANDBOX_GCP_PROJECT")
            or os.getenv("GOOGLE_CLOUD_PROJECT")
            or ""
        ).strip()
        spawn_py = (
            "import subprocess, os, sys, signal, time; "
            "signal.signal(signal.SIGHUP, signal.SIG_IGN); "
            f"p = subprocess.Popen(['{prof.python_bin}', '-m', 'uvicorn', "
            f"'horizon.fast_api_app:app', '--host', '0.0.0.0', '--port', '{a2a_port}'], "
            "stdin=subprocess.DEVNULL, stdout=open('/tmp/horizon-a2a.log', 'w'), "
            "stderr=subprocess.STDOUT, start_new_session=True, close_fds=True); "
            "open('/tmp/horizon.pid', 'w').write(str(p.pid))"
        )
        # Horizon resolves its root model at server start (horizon/models/selector.py
        # reads LHA_ROOT_MODEL), so pinning must happen here rather than per request.
        # Unset leaves the harness default. Valid names come from its MODEL_REGISTRY.
        lha_model = os.getenv("LHA_ROOT_MODEL", "").strip()
        lha_model_env = f"LHA_ROOT_MODEL={shlex.quote(lha_model)} " if lha_model else ""
        return {
            "version": "git:main@latest",
            "source_url": pkg_spec,
            "health_probe": f"GET http://127.0.0.1:{a2a_port}/.well-known/agent-card.json",
            "commands": [
                prof.venv_bootstrap_command(),
                prof.uv_pip_install(pkg_spec),
                (
                    f"(curl -fsSL http://127.0.0.1:{a2a_port}/.well-known/agent-card.json >/dev/null 2>&1) || "
                    "(([ -f /tmp/horizon.pid ] && kill $(cat /tmp/horizon.pid) 2>/dev/null || true); "
                    "LHA_ENVIRONMENT_BACKEND=local USE_IN_MEMORY_SESSION=true "
                    f"USE_IN_MEMORY_TASK_STORE=true GOOGLE_GENAI_USE_VERTEXAI=TRUE "
                    f"{lha_model_env}"
                    f"GOOGLE_CLOUD_PROJECT={shlex.quote(proj_id)} "
                    f"GOOGLE_CLOUD_LOCATION=global "
                    f"GOOGLE_APPLICATION_CREDENTIALS=/workspace/.sonar/application_default_credentials.json "
                    f"APP_URL=http://127.0.0.1:{a2a_port} "
                    f'nohup python3 -c "{spawn_py}" >/dev/null 2>&1 & sleep 2)'
                ),
            ],
        }
    if harness_name == "claude":
        return {
            "version": "cli:latest",
            "source_url": "https://www.npmjs.com/package/@anthropic-ai/claude-code",
            "health_probe": f"{prof.path_prelude()}claude --version",
            "commands": [
                f"{prof.path_prelude()}command -v claude >/dev/null 2>&1 || "
                f"({prof.npm_global_install('@anthropic-ai/claude-code@latest')})",
            ],
        }
    if harness_name == "antigravity":
        return {
            "version": "cli:latest",
            "source_url": "https://antigravity.google/cli/install.sh",
            "health_probe": f"{prof.path_prelude()}agy --version",
            "commands": [
                f"{prof.path_prelude()}command -v agy >/dev/null 2>&1 || "
                "(curl -fsSL https://antigravity.google/cli/install.sh | bash); "
                'mkdir -p "$HOME/.gemini/antigravity-cli" && '
                'printf \'{"modelProvider":"gemini"}\\n\' > "$HOME/.gemini/antigravity-cli/settings.json"',
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
        self._profile_locks: dict[str, asyncio.Lock] = {}
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._profiles: dict[str, SandboxRuntimeProfile] = {}

    def _get_lock(self, key: tuple[str, str]) -> asyncio.Lock:
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]

    def _get_profile_lock(self, sandbox_name: str) -> asyncio.Lock:
        if sandbox_name not in self._profile_locks:
            self._profile_locks[sandbox_name] = asyncio.Lock()
        return self._profile_locks[sandbox_name]

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

    async def _exec_in_sandbox(
        self,
        context: SandboxContext,
        command: str,
        env: dict[str, str] | None = None,
    ) -> tuple[int, str]:
        """Execute a provisioning or health-check shell command on Sandbox Port 8080 (/exec)."""
        result = await exec_in_sandbox(context, command, env=env, timeout_s=120.0)
        if result.error:
            return (result.exit_code or 1, result.error)
        return (result.exit_code, result.stdout + result.stderr)

    async def get_runtime_profile(
        self,
        context: SandboxContext,
        *,
        force_refresh: bool = False,
    ) -> SandboxRuntimeProfile:
        """Return the cached execution layout for this container, probing once.

        A failed probe is not fatal: provisioning falls back to the
        configuration-only profile, which is still correct for the common case.
        """
        cached = self._profiles.get(context.sandbox_name)
        if cached is not None and not force_refresh:
            return cached

        async with self._get_profile_lock(context.sandbox_name):
            cached = self._profiles.get(context.sandbox_name)
            if cached is not None and not force_refresh:
                return cached

            probe: dict[str, Any] = {}
            code, output = await self._exec_in_sandbox(context, SANDBOX_PROBE_COMMAND)
            if code == 0 and output.strip():
                probe = parse_sandbox_probe(output)
            else:
                logger.warning(
                    "Sandbox runtime probe failed in %s (exit %s); "
                    "falling back to configured defaults: %s",
                    context.sandbox_name,
                    code,
                    output.strip()[:200],
                )

            profile = resolve_sandbox_runtime_profile(probe)
            self._profiles[context.sandbox_name] = profile
            logger.info(
                "Resolved sandbox runtime profile for %s: %s",
                context.sandbox_name,
                profile.to_dict(),
            )
            return profile


    async def check_health(
        self,
        harness: CodingHarness,
        context: SandboxContext,
        profile: SandboxRuntimeProfile | None = None,
    ) -> tuple[bool, str | None]:
        """Verify that a coding harness is installed and responsive inside the sandbox."""
        if harness.execution_mode == "sandbox_a2a":
            headers = context.build_sandbox_headers(port=context.a2a_port)
            card_url = f"https://{context.lb_host}/a2a/horizon/.well-known/agent-card.json"
            if hasattr(harness, "get_sandbox_a2a_url"):
                base = harness.get_sandbox_a2a_url(context)
                card_url = f"{base}/.well-known/agent-card.json"
            last_err = None
            for _ in range(5):
                try:
                    async with httpx.AsyncClient(
                        transport=context.http_transport,
                        headers=headers,
                        timeout=httpx.Timeout(5.0, connect=3.0),
                    ) as client:
                        resp = await client.get(card_url)
                        if resp.status_code == 200:
                            return True, None
                        last_err = f"A2A agent-card returned HTTP {resp.status_code}"
                except Exception as exc:
                    last_err = f"A2A endpoint unreachable on port {context.a2a_port}: {exc}"
                if context.http_transport is None:
                    code, out = await self._exec_in_sandbox(
                        context,
                        f"curl -fsSL http://127.0.0.1:{context.a2a_port}/.well-known/agent-card.json >/dev/null",
                    )
                    if code == 0:
                        return True, None
                await asyncio.sleep(2.0)
            return False, last_err

        prof = profile or await self.get_runtime_profile(context)
        recipe = get_default_provision_recipe(harness.name, context.a2a_port, prof)
        code, output = await self._exec_in_sandbox(context, recipe["health_probe"])
        if code == 0:
            return True, None
        return False, output or f"Health probe failed with exit code {code}"


    async def _resolve_context(
        self, context: SandboxContext | None = None
    ) -> SandboxContext:
        """Resolve an active SandboxContext when callers omit `context` (e.g. harness switch warm-up)."""
        if context is not None:
            return context
        if os.getenv("ORCHESTRATOR_SKIP_STARTUP_WARMUP", "").lower() in {
            "1",
            "true",
            "yes",
        }:
            return SandboxContext()
        try:
            from app.workers.factory import get_worker_backend
            from app.workers.sandbox import SandboxWorker

            worker = get_worker_backend()
            if isinstance(worker, SandboxWorker):
                return await worker.resolve_sandbox_context()
        except Exception as exc:
            logger.debug("Could not resolve live Vertex Sandbox context: %s", exc)
        return SandboxContext()

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
        ctx = await self._resolve_context(context)
        key = (ctx.sandbox_name, harness.name)
        lock = self._get_lock(key)

        async with lock:
            state = self.get_status(harness.name, ctx.sandbox_name)
            profile = await self.get_runtime_profile(ctx, force_refresh=force_refresh)
            recipe = get_default_provision_recipe(harness.name, ctx.a2a_port, profile)
            state.version = recipe["version"]
            state.source_url = recipe["source_url"]

            if not force_refresh and state.status == "ready":
                healthy, err = await self.check_health(harness, ctx, profile)
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
                try:
                    custom_cmds = harness.build_provision_commands(profile=profile)
                except TypeError:
                    # Harnesses predating profile support still build their own
                    # commands; they inherit the recipe defaults instead.
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
                        message=f"Verifying {harness.display_name} environment...",
                    )
                )

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

            healthy, err = await self.check_health(harness, ctx, profile)
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
                        message=f"{harness.display_name} environment ready",
                    )
                )
            return state

    def warm_up_in_background(
        self,
        harness: CodingHarness,
        context: SandboxContext | None = None,
    ) -> asyncio.Task[HarnessProvisionStatus] | None:
        """Trigger eager non-blocking background provisioning when a user switches harnesses."""
        if context is None and os.getenv("ORCHESTRATOR_SKIP_STARTUP_WARMUP", "").lower() in {
            "1",
            "true",
            "yes",
        }:
            return None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None

        async def _runner() -> HarnessProvisionStatus:
            resolved_ctx = await self._resolve_context(context)
            try:
                return await self.ensure_provisioned(harness, resolved_ctx)
            except Exception as exc:
                logger.warning("Background warm-up for %s failed: %s", harness.name, exc)
                return self.get_status(
                    harness.name,
                    resolved_ctx.sandbox_name,
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
