"""Core protocols, shared data structures, git worktree isolation, and stream parsers for coding harnesses."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from app.tools.workspace_tools import get_workspace_root
from app.workers.base import WorkerExecutionResult

logger = logging.getLogger(__name__)



@dataclass
class SandboxContext:
    """Shared execution context for a per-user Vertex Sandbox or local worktree."""

    user_id: str = "default_user"
    sandbox_name: str = "voice-worker-default_user"
    lb_host: str = field(
        default_factory=lambda: os.getenv(
            "VERTEX_SANDBOX_LB_HOST", "sandbox.aiplatform.googleapis.com"
        )
    )
    routing_token: str = field(
        default_factory=lambda: os.getenv("VERTEX_SANDBOX_ROUTING_TOKEN", "sandbox-routing-token")
    )
    sandbox_token: str = field(
        default_factory=lambda: os.getenv("VERTEX_SANDBOX_TOKEN", "sandbox-auth-token")
    )
    exec_port: int = 8080
    a2a_port: int = 8081
    worktree_dir: Path | None = None
    branch: str | None = None
    # Optional httpx transport override. Production leaves this as None; tests
    # inject an `httpx.MockTransport` to drive the real harness code paths.
    http_transport: Any | None = None

    def build_sandbox_headers(self, port: int | None = None) -> dict[str, str]:
        """Return Vertex Agent Platform Sandbox routing headers for a container port."""
        target_port = port or self.exec_port
        return {
            "Authorization": f"Bearer {self.sandbox_token}",
            "X-Sandbox-Routing-Token": self.routing_token,
            "X-Sandbox-Port": str(target_port),
        }


@dataclass
class HarnessEvent:
    """Non-blocking progress or completion event emitted by a coding harness."""

    task_id: str
    harness: str
    kind: str  # "progress" | "approval_needed" | "completed" | "error"
    message: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SandboxExecResult:
    """Outcome of a single `/exec` shell invocation inside the Vertex Sandbox.

    `error` is set only for transport-level failures (unreachable host, timeout,
    non-2xx response, unparseable body). A command that ran and failed is
    reported with `error=None` and a non-zero `exit_code`, so callers can
    distinguish "the sandbox is broken" from "the command failed".
    """

    exit_code: int
    stdout: str = ""
    stderr: str = ""
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.exit_code == 0


async def exec_in_sandbox(
    context: SandboxContext,
    command: str,
    *,
    env: dict[str, str] | None = None,
    timeout_s: float = 180.0,
    transport: Any | None = None,
) -> SandboxExecResult:
    """POST a shell command to the Vertex Sandbox `/exec` endpoint (Port 8080).

    This is the single HTTP seam for sandbox command execution, so tests can
    inject an `httpx.MockTransport` and assert on routing headers, non-2xx
    handling, and timeout behavior.

    Never raises and never fabricates success: transport failures come back as
    `SandboxExecResult(exit_code=1, error=...)`.
    """
    import httpx

    headers = context.build_sandbox_headers(port=context.exec_port)
    exec_url = f"https://{context.lb_host}/exec"
    try:
        async with httpx.AsyncClient(
            transport=transport if transport is not None else context.http_transport,
            headers=headers,
            timeout=httpx.Timeout(timeout_s, connect=15.0),
        ) as client:
            resp = await client.post(
                exec_url,
                json={"command": command, "env": env or {}},
            )
    except Exception as exc:
        return SandboxExecResult(
            exit_code=1,
            error=f"Sandbox /exec unreachable at {context.lb_host}: {exc}",
        )

    if resp.status_code >= 400:
        return SandboxExecResult(
            exit_code=resp.status_code,
            error=(
                f"Sandbox /exec returned HTTP {resp.status_code} "
                f"from {context.lb_host}: {resp.text[:300]}"
            ),
        )

    try:
        data = resp.json() if resp.content else {}
    except Exception as exc:
        return SandboxExecResult(
            exit_code=1,
            error=f"Sandbox /exec returned an unparseable response body: {exc}",
        )

    if not isinstance(data, dict):
        return SandboxExecResult(
            exit_code=1,
            error=f"Sandbox /exec returned unexpected payload type {type(data).__name__}.",
        )

    stdout = str(data.get("stdout") or data.get("output") or "")
    stderr = str(data.get("stderr") or "")

    # An absent exit_code reasonably means "the sandbox didn't say", so default
    # to 0. A present but non-numeric one is a broken contract, and coercing it
    # to 0 would report a malformed response as a successful command.
    raw_exit_code = data.get("exit_code", 0)
    try:
        exit_code = int(raw_exit_code)
    except (TypeError, ValueError):
        return SandboxExecResult(
            exit_code=1,
            stdout=stdout,
            stderr=stderr,
            error=(
                "Sandbox /exec returned a non-numeric exit_code "
                f"({raw_exit_code!r}) from {context.lb_host}."
            ),
        )
    return SandboxExecResult(exit_code=exit_code, stdout=stdout, stderr=stderr)


async def launch_detached_command(
    context: SandboxContext,
    command: str,
    *,
    run_id: str,
    worktree_path: str,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Launch a harness command detached inside the sandbox under `.sonar/runs/{run_id}/`."""
    from app.auth import get_sandbox_gcp_env

    merged_env = {**get_sandbox_gcp_env(), **(env or {})}
    run_dir = f"/workspace/.sonar/runs/{run_id}"
    adc_prelude = ""
    if merged_env.get("CLOUDSDK_AUTH_ACCESS_TOKEN") and not merged_env.get(
        "GOOGLE_APPLICATION_CREDENTIALS"
    ):
        adc_prelude = (
            'mkdir -p /workspace/.sonar && '
            'printf \'#!/bin/sh\\necho "{\\"version\\":1,\\"success\\":true,\\"token_type\\":\\"urn:ietf:params:oauth:token-type:access_token\\",\\"access_token\\":\\"%s\\",\\"expiration_time\\":%s}"\\n\' '
            '"$CLOUDSDK_AUTH_ACCESS_TOKEN" "$(( $(date +%s) + 3500 ))" > /workspace/.sonar/gcp_token.sh && '
            'chmod +x /workspace/.sonar/gcp_token.sh && '
            'printf \'{"type":"external_account","audience":"//iam.googleapis.com/projects/0/locations/global/workloadIdentityPools/sonar/providers/sandbox","subject_token_type":"urn:ietf:params:oauth:token-type:access_token","token_url":"https://sts.googleapis.com/v1/token","credential_source":{"executable":{"command":"/workspace/.sonar/gcp_token.sh"}}}\\n\' '
            '> /workspace/.sonar/gcp_adc.json && '
            'export GOOGLE_APPLICATION_CREDENTIALS=/workspace/.sonar/gcp_adc.json '
            'GOOGLE_EXTERNAL_ACCOUNT_ALLOW_EXECUTABLES=1; '
        )
    script = (
        f"{adc_prelude}"
        f"mkdir -p {shlex.quote(run_dir)} {shlex.quote(worktree_path)} && "
        f"cd {shlex.quote(worktree_path)} && "
        f'echo \'{{"state":"running","started_at":\'$(date +%s)\'}}\' > {shlex.quote(run_dir)}/status.json && '
        f"(setsid {command} > {shlex.quote(run_dir)}/stdout.jsonl 2> {shlex.quote(run_dir)}/stderr.log; "
        f'EC=$?; echo "{{\\"state\\":\\"completed\\",\\"exit_code\\":$EC,\\"ended_at\\":$(date +%s)}}" > {shlex.quote(run_dir)}/status.json) & '
        f"PID=$!; echo $PID > {shlex.quote(run_dir)}/pid"
    )
    result = await exec_in_sandbox(context, script, env=merged_env, timeout_s=15.0)
    return {
        "run_id": run_id,
        "run_dir": run_dir,
        "worktree_path": worktree_path,
        "exit_code": result.exit_code,
        "error": result.error,
    }


async def poll_detached_command(
    context: SandboxContext,
    run_handle: dict[str, Any],
    *,
    offset: int = 0,
) -> tuple[dict[str, Any], str, int]:
    """Polls status.json and tails new stdout.jsonl bytes past `offset`."""
    import json

    run_dir = run_handle.get("run_dir") or f"/workspace/.sonar/runs/{run_handle.get('run_id')}"
    script = (
        f"cat {shlex.quote(run_dir)}/status.json 2>/dev/null || echo '{{\"state\":\"running\"}}'; "
        f'echo ""; echo "---STATUS_DELIMITER---"; '
        f"tail -c +{offset + 1} {shlex.quote(run_dir)}/stdout.jsonl 2>/dev/null || true"
    )
    res = await exec_in_sandbox(context, script, timeout_s=15.0)
    raw = res.stdout or ""
    parts = raw.split("---STATUS_DELIMITER---", 1)
    status_json_str = parts[0].strip() if parts else "{}"
    new_stdout = parts[1].lstrip("\n") if len(parts) > 1 else ""

    try:
        status_data = json.loads(status_json_str) if status_json_str else {"state": "running"}
    except Exception:
        status_data = {"state": "running"}

    new_offset = offset + len(new_stdout.encode("utf-8"))
    return status_data, new_stdout, new_offset


async def cancel_detached_command(
    context: SandboxContext,
    run_handle: dict[str, Any],
) -> bool:
    """Cancels a detached harness execution by terminating its process group."""
    run_dir = run_handle.get("run_dir") or f"/workspace/.sonar/runs/{run_handle.get('run_id')}"
    script = (
        f"PID=$(cat {shlex.quote(run_dir)}/pid 2>/dev/null); "
        f'if [ -n "$PID" ]; then '
        f'  kill -TERM -"$PID" 2>/dev/null || kill -TERM "$PID" 2>/dev/null || true; '
        f"  sleep 0.2; "
        f'  kill -KILL -"$PID" 2>/dev/null || kill -KILL "$PID" 2>/dev/null || true; '
        f'  echo \'{{"state":"cancelled","exit_code":-1}}\' > {shlex.quote(run_dir)}/status.json; '
        f"fi"
    )
    res = await exec_in_sandbox(context, script, timeout_s=10.0)
    return res.ok


def _collect_worktree_changes_sync(
    worktree_path: str | Path,
) -> tuple[list[str], str, str]:
    """Blocking implementation of :func:`collect_worktree_changes`."""
    wt = Path(worktree_path)
    if not wt.exists():
        return ([], "", "")

    if (wt / ".git").exists():
        # Mark untracked files intent-to-add so `git diff` reports their contents
        # and line counts. This only touches the index, never the working tree.
        subprocess.run(
            ["git", "-C", str(wt), "add", "-A", "-N"],
            check=False,
            capture_output=True,
        )
        status = subprocess.run(
            ["git", "-C", str(wt), "status", "--porcelain"],
            check=False,
            capture_output=True,
            text=True,
        )
        if status.returncode == 0:
            files: list[str] = []
            for line in status.stdout.splitlines():
                entry = line[2:].strip() if len(line) > 2 else ""
                if " -> " in entry:  # renames report "old -> new"
                    entry = entry.split(" -> ", 1)[1]
                if entry:
                    files.append(entry.strip('"'))

            if not files:
                return ([], "", "")

            raw_diff = subprocess.run(
                ["git", "-C", str(wt), "diff", "HEAD"],
                check=False,
                capture_output=True,
                text=True,
            ).stdout
            numstat = subprocess.run(
                ["git", "-C", str(wt), "diff", "--numstat", "HEAD"],
                check=False,
                capture_output=True,
                text=True,
            ).stdout

            added = deleted = 0
            for row in numstat.splitlines():
                cols = row.split("\t")
                if len(cols) >= 2:
                    if cols[0].isdigit():
                        added += int(cols[0])
                    if cols[1].isdigit():
                        deleted += int(cols[1])

            summary = (
                f"{len(files)} file(s) changed (+{added} -{deleted}) "
                f"in {wt.name}."
            )
            return (sorted(files), summary, raw_diff)

    # No git metadata (ORCHESTRATOR_DISABLE_HOST_GIT, or an uninitialized worktree).
    # Fall back to a baseline file-set comparison and say so, rather than inventing a diff.
    baseline: set[str] = set()
    meta_file = wt / ".orchestrator_repo.json"
    if meta_file.exists():
        try:
            baseline = set(json.loads(meta_file.read_text()).get("baseline_files", []))
        except Exception:
            baseline = set()

    current = {
        str(f.relative_to(wt))
        for f in wt.rglob("*")
        if f.is_file() and not f.name.startswith(".") and ".worktrees" not in f.parts
    }
    new_files = sorted(current - baseline)
    if not new_files:
        return ([], "", "")
    summary = (
        f"{len(new_files)} file(s) changed in {wt.name} "
        "(no git metadata available, so line counts are unavailable)."
    )
    return (new_files, summary, "")


async def _collect_sandbox_worktree_changes(
    context: SandboxContext,
    worktree_path: str,
) -> tuple[list[str], str, str]:
    """Derive `(files_changed, diff_summary, raw_diff)` directly from a worktree inside the Vertex Sandbox."""
    cmd = (
        f"cd {shlex.quote(worktree_path)} 2>/dev/null && "
        "git add -A -N >/dev/null 2>&1 && "
        "STATUS=$(git status --porcelain 2>/dev/null); "
        'if [ -n "$STATUS" ]; then '
        '  printf "%s\\n" "$STATUS"; '
        "  echo '---NUMSTAT---'; git diff --numstat HEAD 2>/dev/null; "
        "  echo '---DIFF---'; git diff HEAD 2>/dev/null; "
        "else "
        "  BASE=$(git merge-base HEAD main 2>/dev/null || git rev-parse HEAD~1 2>/dev/null || echo ''); "
        '  if [ -n "$BASE" ] && [ "$BASE" != "$(git rev-parse HEAD 2>/dev/null)" ]; then '
        '    git diff --name-only "$BASE" HEAD 2>/dev/null | sed "s/^/M  /"; '
        '    echo "---NUMSTAT---"; git diff --numstat "$BASE" HEAD 2>/dev/null; '
        '    echo "---DIFF---"; git diff "$BASE" HEAD 2>/dev/null; '
        "  fi; "
        "fi"
    )
    res = await exec_in_sandbox(context, cmd, timeout_s=30.0)
    if res.exit_code != 0:
        # A broken git invocation must not masquerade as "no changes made", or a
        # harness that silently did nothing scores the same as one that worked.
        detail = (res.error or res.stderr or "").strip()[:200]
        return (
            [],
            f"Could not inspect changes in {worktree_path}: git exited "
            f"{res.exit_code}. {detail}".strip(),
            "",
        )
    if not res.stdout:
        return ([], "", "")

    parts = res.stdout.split("---DIFF---", 1)
    raw_diff = parts[1].strip() if len(parts) > 1 else ""
    header_parts = parts[0].split("---NUMSTAT---", 1)
    status_text = header_parts[0].strip()
    numstat_text = header_parts[1].strip() if len(header_parts) > 1 else ""

    files: list[str] = []
    for line in status_text.splitlines():
        entry = line[2:].strip() if len(line) > 2 else ""
        if " -> " in entry:
            entry = entry.split(" -> ", 1)[1]
        if entry:
            files.append(entry.strip('"'))

    if not files:
        return ([], "", "")

    added = deleted = 0
    for row in numstat_text.splitlines():
        cols = row.split("\t")
        if len(cols) >= 2:
            if cols[0].isdigit():
                added += int(cols[0])
            if cols[1].isdigit():
                deleted += int(cols[1])

    wt_name = worktree_path.rstrip("/").split("/")[-1]
    summary = f"{len(files)} file(s) changed (+{added} -{deleted}) in {wt_name}."
    return (sorted(files), summary, raw_diff)


async def collect_worktree_changes(
    worktree_path: str | Path,
    context: SandboxContext | None = None,
) -> tuple[list[str], str, str]:
    """Derive `(files_changed, diff_summary, raw_diff)` from real state in a worktree.

    If a SandboxContext is provided and worktree_path is inside the sandbox (/workspace),
    changes are inspected inside the remote container via `/exec`. Otherwise, runs
    against the local host filesystem.
    """
    if context is not None and str(worktree_path).startswith("/workspace"):
        return await _collect_sandbox_worktree_changes(context, str(worktree_path))
    return await asyncio.to_thread(_collect_worktree_changes_sync, worktree_path)


_ARTIFACT_DOC_EXTENSIONS = {".md", ".txt", ".csv", ".html"}
_IGNORED_ARTIFACT_NAMES = {
    "agents.md",
    "claude.md",
    "gemini.md",
    "readme.md",
    "package.json",
    "package-lock.json",
    "tsconfig.json",
}


def _humanize_artifact_title(rel_path: str, content: str) -> str:
    """Derive a clean human-readable title from markdown heading or filename."""
    for line in content.splitlines()[:6]:
        stripped = line.strip()
        if stripped.startswith("# "):
            heading = stripped[2:].strip().strip("*_`")
            if heading:
                return heading[:64]
    stem = Path(rel_path).stem.replace("_", " ").replace("-", " ").strip()
    return stem.title() if stem else Path(rel_path).name


async def collect_worktree_artifacts(
    worktree_path: str | Path,
    files_changed: list[str],
    context: SandboxContext | None = None,
) -> list[dict[str, str]]:
    """Extract content of readable document deliverables (.md, .txt, .csv, .html) created/modified in the worktree."""
    candidates = [
        f
        for f in (files_changed or [])
        if Path(f).suffix.lower() in _ARTIFACT_DOC_EXTENSIONS
        and Path(f).name.lower() not in _IGNORED_ARTIFACT_NAMES
    ][:4]
    if not candidates:
        return []

    artifacts: list[dict[str, str]] = []
    if context is not None and str(worktree_path).startswith("/workspace"):
        wt_str = str(worktree_path)
        for rel in candidates:
            cmd = f"head -c 14000 {shlex.quote(wt_str + '/' + rel)} 2>/dev/null || true"
            res = await exec_in_sandbox(context, cmd, timeout_s=15.0)
            text = (res.stdout or "").strip()
            if text:
                artifacts.append(
                    {
                        "name": rel,
                        "title": _humanize_artifact_title(rel, text),
                        "content": text,
                    }
                )
        return artifacts

    wt = Path(worktree_path)
    for rel in candidates:
        target = wt / rel
        if target.is_file():
            try:
                text = target.read_text(encoding="utf-8", errors="replace")[:14000].strip()
                if text:
                    artifacts.append(
                        {
                            "name": rel,
                            "title": _humanize_artifact_title(rel, text),
                            "content": text,
                        }
                    )
            except Exception:
                continue
    return artifacts




@runtime_checkable
class CodingHarness(Protocol):
    """Protocol for pluggable coding harnesses running inside the Sandbox."""

    name: str
    display_name: str
    execution_mode: str  # "sandbox_exec" (Port 8080 /exec) | "sandbox_a2a" (Port 8081 RemoteA2aAgent)
    description: str
    binary_name: str
    fallback_binaries: tuple[str, ...]

    def build_command(
        self,
        *,
        goal: str,
        repo: str,
        task_id: str,
        mode: str = "execute",
        binary: str | None = None,
    ) -> list[str]:
        """Return the CLI argv list executed inside the sandbox or local worktree."""
        ...

    def format_sandbox_command(
        self,
        *,
        goal: str,
        repo: str,
        task_id: str,
        mode: str = "execute",
    ) -> str:
        """Return the command or endpoint description executed inside the sandbox."""
        ...

    def build_env(self) -> dict[str, str]:
        """Return environment variables required by this harness inside the sandbox."""
        ...

    def parse_stream_line(self, line: str, task_id: str) -> HarnessEvent | None:
        """Parse a single stdout line (e.g. stream-json) into a structured HarnessEvent."""
        ...

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
        """Execute the coding task non-blockingly inside the provided SandboxContext."""
        ...


def parse_stream_json_line(
    line: str, task_id: str, harness_name: str
) -> HarnessEvent | None:
    """Shared parser for JSONL stream-json output from Claude Code (`type`) and Antigravity (`event`) CLIs."""
    stripped = line.strip()
    if not stripped:
        return None
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return HarnessEvent(
            task_id=task_id,
            harness=harness_name,
            kind="progress",
            message=stripped[:200],
        )

    # 1. Antigravity (`agy`) stream-json envelope: `{"event": "init" | "step_update" | "result", ...}`
    agy_event = payload.get("event")
    if isinstance(agy_event, str):
        conv_id = payload.get("conversation_id")
        if agy_event == "init":
            init_meta = dict(payload)
            if conv_id:
                init_meta["session_id"] = conv_id
            return HarnessEvent(
                task_id=task_id,
                harness=harness_name,
                kind="progress",
                message=f"Initialized {harness_name} session {conv_id or ''}".strip(),
                metadata=init_meta,
            )
        if agy_event == "step_update":
            step = payload.get("step_update") or {}
            step_type = step.get("step_type", "")
            conv_id = step.get("conversation_id") or conv_id
            meta = dict(payload)
            if conv_id:
                meta["session_id"] = conv_id
            if step_type == "tool":
                tool_name = step.get("tool_name") or (step.get("tool_info") or {}).get("name") or "tool"
                return HarnessEvent(
                    task_id=task_id,
                    harness=harness_name,
                    kind="progress",
                    message=f"Running {tool_name}",
                    metadata=meta,
                )
            text_delta = step.get("text_delta")
            if isinstance(text_delta, str) and text_delta.strip():
                return HarnessEvent(
                    task_id=task_id,
                    harness=harness_name,
                    kind="progress",
                    message=text_delta.strip(),
                    metadata=meta,
                )
            return None
        if agy_event == "result":
            res_obj = payload.get("result") if isinstance(payload.get("result"), dict) else payload
            conv_id = res_obj.get("conversation_id") or conv_id
            status_val = str(res_obj.get("status", "SUCCESS")).upper()
            is_err = status_val in {"ERROR", "INVALID", "CANCELED", "INTERRUPTED"} or bool(
                res_obj.get("error")
            )
            resp_text = (
                res_obj.get("error")
                if is_err and res_obj.get("error")
                else (res_obj.get("response") or res_obj.get("result") or "Task finished.")
            )
            meta = dict(res_obj)
            if conv_id:
                meta["session_id"] = conv_id
            return HarnessEvent(
                task_id=task_id,
                harness=harness_name,
                kind="error" if is_err else "completed",
                message=str(resp_text).strip(),
                metadata=meta,
            )

    # 2. Claude Code (`claude -p`) stream-json envelope: `{"type": "system" | "assistant" | "tool_use" | "result", ...}`
    event_type = payload.get("type", "")
    if event_type == "system" and payload.get("subtype") == "init":
        sess_id = payload.get("session_id")
        return HarnessEvent(
            task_id=task_id,
            harness=harness_name,
            kind="progress",
            message=f"Initialized {harness_name} session {sess_id or ''}".strip(),
            metadata=payload,
        )
    if event_type == "result":
        result_text = (
            payload.get("result")
            or payload.get("response")
            or payload.get("summary")
            or "Task finished."
        )
        is_error = bool(payload.get("is_error", False)) or str(
            payload.get("status", "")
        ).upper() == "ERROR"
        return HarnessEvent(
            task_id=task_id,
            harness=harness_name,
            kind="error" if is_error else "completed",
            message=str(result_text).strip(),
            metadata=payload,
        )
    if event_type in {"assistant", "message"}:
        content = (
            payload.get("message", {}).get("content") or payload.get("text") or ""
        )
        if isinstance(content, list):
            texts = [
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            ]
            content = " ".join(t for t in texts if t)
        if content:
            return HarnessEvent(
                task_id=task_id,
                harness=harness_name,
                kind="progress",
                message=str(content),
                metadata=payload,
            )
    if event_type == "tool_use":
        tool_name = payload.get("name", "tool")
        return HarnessEvent(
            task_id=task_id,
            harness=harness_name,
            kind="progress",
            message=f"Running {tool_name}",
            metadata=payload,
        )
    return None


def _rewrite_github_fork_url(git_url: str, github_user: str) -> tuple[str, str | None]:
    """Rewrite a canonical GitHub repo URL to `github.com/<github_user>/<repo>.git` and return `(fork_url, upstream_url)`."""
    import re

    m = re.match(r"^(https://github\.com/|git@github\.com:)([^/]+)/([^/]+?)(\.git)?$", git_url.strip())
    if not m:
        return git_url, None
    prefix, owner, repo_name, suffix = m.groups()
    if owner.lower() == github_user.lower():
        return git_url, None
    ext = suffix or ".git"
    fork_url = f"{prefix}{github_user}/{repo_name}{ext}"
    return fork_url, git_url


def _merge_repo_lists(
    base_list: list[dict[str, Any]],
    override_list: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged_by_name: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for item in base_list:
        if isinstance(item, dict) and item.get("name"):
            name = str(item["name"])
            merged_by_name[name] = dict(item)
            order.append(name)
    for item in override_list:
        if isinstance(item, dict) and item.get("name"):
            name = str(item["name"])
            if name in merged_by_name:
                merged_by_name[name].update(item)
            else:
                merged_by_name[name] = dict(item)
                order.append(name)
    return [merged_by_name[k] for k in order]


def load_workspaces_manifest(local_override_path: Path | None = None) -> dict[str, Any]:
    """Load `config/workspaces.yaml` and deep-merge `config/workspaces.local.yaml` (personal forks, branches, packages)."""
    import yaml

    config_dir = Path(__file__).resolve().parents[3] / "config"
    manifest_path = config_dir / "workspaces.yaml"
    if not manifest_path.exists():
        return {}
    try:
        base_data: dict[str, Any] = yaml.safe_load(manifest_path.read_text()) or {}
    except Exception:
        return {}

    env_local = os.getenv("ORCHESTRATOR_WORKSPACES_LOCAL_YAML")
    override_file = (
        local_override_path
        or (Path(env_local) if env_local else config_dir / "workspaces.local.yaml")
    )
    if override_file and override_file.exists():
        try:
            local_data: dict[str, Any] = yaml.safe_load(override_file.read_text()) or {}
        except Exception:
            local_data = {}
    else:
        local_data = {}

    if not local_data:
        return base_data

    result = dict(base_data)
    for key in ("workspace_root", "github_user", "prefer_forks", "default_harness"):
        if key in local_data:
            result[key] = local_data[key]

    # Merge sandbox section
    base_sb = dict(base_data.get("sandbox") or {})
    local_sb = dict(local_data.get("sandbox") or {})
    sandbox_list_keys = (
        "uv_tools",
        "python_packages",
        "npm_packages",
        "skills",
        "system_binaries",
    )
    for list_key in sandbox_list_keys:
        if list_key in local_sb:
            combined = list(
                dict.fromkeys(
                    list(base_sb.get(list_key, [])) + list(local_sb.get(list_key, []))
                )
            )
            base_sb[list_key] = combined
    for k, v in local_sb.items():
        if k not in sandbox_list_keys:
            base_sb[k] = v
    result["sandbox"] = base_sb

    # Merge repositories and seed_repositories by name
    result["repositories"] = _merge_repo_lists(
        list(base_data.get("repositories", [])),
        list(local_data.get("repositories", [])),
    )
    result["seed_repositories"] = _merge_repo_lists(
        list(base_data.get("seed_repositories", [])),
        list(local_data.get("seed_repositories", [])),
    )

    # Apply automatic fork + upstream URL rewriting when `github_user` and `prefer_forks` are active
    github_user = str(result.get("github_user") or "").strip()
    prefer_forks = bool(result.get("prefer_forks", False))
    if github_user and prefer_forks:
        rewritten_repos: list[dict[str, Any]] = []
        for repo in result.get("repositories", []):
            r_copy = dict(repo)
            if r_copy.get("git_url") and not r_copy.get("upstream_url"):
                fork_url, upstream_url = _rewrite_github_fork_url(
                    str(r_copy["git_url"]), github_user
                )
                r_copy["git_url"] = fork_url
                if upstream_url:
                    r_copy["upstream_url"] = upstream_url
            rewritten_repos.append(r_copy)
        result["repositories"] = rewritten_repos

        # Also point Horizon package spec to the developer's adk-samples fork if not explicitly overridden
        if not result["sandbox"].get("horizon_package_spec"):
            adk_repo = next(
                (r for r in result["repositories"] if r.get("name") == "adk-samples"),
                None,
            )
            branch = (adk_repo or {}).get("branch", "main")
            result["sandbox"]["horizon_package_spec"] = (
                f"git+https://github.com/{github_user}/adk-samples.git@{branch}"
                "#subdirectory=core/python/long-horizon-harness"
            )

    return result


def sync_cross_harness_context(target_dir: Path, repo_name: str) -> None:
    """Synchronize AGENTS.md, CLAUDE.md, GEMINI.md, and .mcp.json in `target_dir` so all harnesses share identical state."""
    if not target_dir.exists():
        return

    manifest = load_workspaces_manifest()
    shared_rules = (
        manifest.get("sandbox", {}).get("shared_context_rules")
        or f"# {repo_name} Shared Workspace Context\nShared across Claude Code, Antigravity, and ADK Long Horizon.\n"
    ).strip()

    # Preserve any existing instructions if already customized in one of the files
    existing_text: str | None = None
    for fname in ("AGENTS.md", "CLAUDE.md", "GEMINI.md"):
        candidate = target_dir / fname
        if candidate.exists():
            content = candidate.read_text().strip()
            if content:
                existing_text = content
                break

    final_rules = (existing_text or f"# Repository: {repo_name}\n\n{shared_rules}").strip() + "\n"
    for fname in ("AGENTS.md", "CLAUDE.md", "GEMINI.md"):
        dest = target_dir / fname
        if not dest.exists() or dest.read_text() != final_rules:
            dest.write_text(final_rules)

    project_mcp = Path(__file__).resolve().parents[3] / ".mcp.json"
    if project_mcp.exists():
        try:
            mcp_text = project_mcp.read_text()
            dest_mcp = target_dir / ".mcp.json"
            if not dest_mcp.exists() or dest_mcp.read_text() != mcp_text:
                dest_mcp.write_text(mcp_text)
        except Exception:
            pass


def _repo_slug(repo: str) -> str:
    """Normalize a repo name to its on-disk workspace directory name."""
    return (
        (repo if repo and repo != "current" else "default-repo")
        .strip()
        .lower()
        .replace(" ", "-")
    )


def ensure_repo_and_worktree(repo: str, task_id: str) -> tuple[Path, Path, str]:
    """Ensure base git repository exists (seeding from config/workspaces.yaml if declared) and create an isolated per-task git worktree on branch `agent/{task_id}`.

    Returns:
        (repo_dir, worktree_dir, branch_name)
    """
    root = get_workspace_root()
    slug = _repo_slug(repo)
    repo_dir = root / slug
    repo_dir.mkdir(parents=True, exist_ok=True)

    manifest = load_workspaces_manifest()
    seed_spec = next(
        (
            s
            for s in manifest.get("seed_repositories", [])
            if isinstance(s, dict) and s.get("name") == slug
        ),
        None,
    )

    if seed_spec and isinstance(seed_spec.get("files"), dict):
        for rel_path, file_content in seed_spec["files"].items():
            target_file = repo_dir / rel_path
            if not target_file.exists():
                target_file.parent.mkdir(parents=True, exist_ok=True)
                target_file.write_text(str(file_content))

    sync_cross_harness_context(repo_dir, slug)
    readme = repo_dir / "README.md"
    if not readme.exists():
        readme.write_text(f"# {slug}\n")

    disable_git = os.getenv("ORCHESTRATOR_DISABLE_HOST_GIT", "").lower() in {"1", "true", "yes"}

    if disable_git:
        meta_file = repo_dir / ".orchestrator_repo.json"
        if not meta_file.exists():
            baseline_files = sorted(
                str(f.relative_to(repo_dir))
                for f in repo_dir.rglob("*")
                if f.is_file() and not f.name.startswith(".") and ".worktrees" not in f.parts
            )
            meta_file.write_text(json.dumps({"branch": "main", "baseline_files": baseline_files}))
        if task_id == "seed-init":
            return repo_dir, repo_dir, "main"
        branch_name = f"agent/{task_id}"
        worktrees_root = root / ".worktrees" / slug
        worktrees_root.mkdir(parents=True, exist_ok=True)
        worktree_dir = worktrees_root / task_id
        if not worktree_dir.exists():
            shutil.copytree(
                repo_dir,
                worktree_dir,
                ignore=shutil.ignore_patterns(".git", ".worktrees", "__pycache__"),
                dirs_exist_ok=True,
            )
        sync_cross_harness_context(worktree_dir, slug)
        return repo_dir, worktree_dir, branch_name

    # Real application runtime: full Git repository initialization + isolated `git worktree add -B agent/<task_id>`
    if not (repo_dir / ".git").exists():
        subprocess.run(
            ["git", "-C", str(repo_dir), "init", "-b", "main"],
            check=False,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(repo_dir), "add", "."],
            check=False,
            capture_output=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(repo_dir),
                "-c",
                "user.name=ADKSonar",
                "-c",
                "user.email=sonar@example.com",
                "commit",
                "-m",
                "Initial commit",
            ],
            check=False,
            capture_output=True,
        )

    status_check = subprocess.run(
        ["git", "-C", str(repo_dir), "status", "--porcelain"],
        check=False,
        capture_output=True,
        text=True,
    )
    head_check = subprocess.run(
        ["git", "-C", str(repo_dir), "rev-parse", "--verify", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    if head_check.returncode != 0 or (task_id == "seed-init" and status_check.stdout.strip()):
        subprocess.run(
            ["git", "-C", str(repo_dir), "add", "."],
            check=False,
            capture_output=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(repo_dir),
                "-c",
                "user.name=ADKSonar",
                "-c",
                "user.email=sonar@example.com",
                "commit",
                "-m",
                "Initialize repository seed and context",
            ],
            check=False,
            capture_output=True,
        )

    if task_id == "seed-init":
        return repo_dir, repo_dir, "main"

    branch_name = f"agent/{task_id}"
    worktrees_root = root / ".worktrees" / slug
    worktrees_root.mkdir(parents=True, exist_ok=True)
    worktree_dir = worktrees_root / task_id

    if not worktree_dir.exists():
        # A stale registration from a previously removed worktree makes
        # `worktree add` fail outright, so clear those first.
        subprocess.run(
            ["git", "-C", str(repo_dir), "worktree", "prune"],
            check=False,
            capture_output=True,
        )
        branch_exists = (
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo_dir),
                    "rev-parse",
                    "--verify",
                    "--quiet",
                    f"refs/heads/{branch_name}",
                ],
                check=False,
                capture_output=True,
            ).returncode
            == 0
        )
        # `-B` resets the branch to HEAD. Using it on a branch that already
        # exists would discard commits from an earlier run of this same task,
        # so only create the branch when it is genuinely new.
        add_args = (
            ["worktree", "add", str(worktree_dir), branch_name]
            if branch_exists
            else ["worktree", "add", "-B", branch_name, str(worktree_dir)]
        )
        added = subprocess.run(
            ["git", "-C", str(repo_dir), *add_args],
            check=False,
            capture_output=True,
            text=True,
        )
        if added.returncode != 0:
            logger.error(
                "git worktree add failed for %s (%s): %s",
                worktree_dir,
                branch_name,
                added.stderr.strip() or "unknown error",
            )

    if worktree_dir.exists():
        sync_cross_harness_context(worktree_dir, slug)

    return repo_dir, worktree_dir, branch_name


def release_worktree(repo: str, task_id: str) -> bool:
    """Remove a task's worktree checkout, leaving its branch and commits intact.

    The counterpart to :func:`ensure_repo_and_worktree`. Without it nothing ever
    reclaims these: each dispatch creates `.worktrees/<repo>/<task_id>` plus a
    branch `agent/<task_id>`, and a long-lived process accumulates one per task
    indefinitely.

    Only the working copy is deleted. `git worktree remove` leaves the branch
    ref and every commit on it in the base repository, so work that was
    committed at the approval gate survives and stays available for a PR.

    Returns:
        True if a worktree was removed. Never raises; cleanup failing must not
        fail the task it belongs to.
    """
    try:
        root = get_workspace_root()
        slug = _repo_slug(repo)
        repo_dir = root / slug
        worktree_dir = root / ".worktrees" / slug / task_id

        if not worktree_dir.exists():
            return False

        if not (repo_dir / ".git").is_dir():
            # ORCHESTRATOR_DISABLE_HOST_GIT: the "worktree" is a plain copy with
            # no registration to clean up.
            shutil.rmtree(worktree_dir, ignore_errors=True)
            return True

        removed = subprocess.run(
            [
                "git",
                "-C",
                str(repo_dir),
                "worktree",
                "remove",
                "--force",
                str(worktree_dir),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if removed.returncode != 0:
            logger.warning(
                "git worktree remove failed for %s: %s",
                worktree_dir,
                removed.stderr.strip() or "unknown error",
            )
            shutil.rmtree(worktree_dir, ignore_errors=True)

        # Drop any registration now pointing at a directory that is gone.
        subprocess.run(
            ["git", "-C", str(repo_dir), "worktree", "prune"],
            check=False,
            capture_output=True,
        )
        return True
    except Exception as exc:
        logger.warning("Worktree cleanup failed for %s/%s: %s", repo, task_id, exc)
        return False


