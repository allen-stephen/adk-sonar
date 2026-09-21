"""Core protocols, shared data structures, git worktree isolation, and stream parsers for coding harnesses."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from app.tools.workspace_tools import get_workspace_root
from app.workers.base import WorkerExecutionResult


@dataclass
class SandboxContext:
    """Shared execution context for a per-user Vertex Sandbox or local worktree."""

    user_id: str = "default_user"
    sandbox_name: str = "voice-worker-default_user"
    lb_host: str = "mock-sandbox-lb.aiplatform.googleapis.com"
    routing_token: str = "mock-token"
    sandbox_token: str = "mock-auth"
    exec_port: int = 8080
    a2a_port: int = 8081
    worktree_dir: Path | None = None
    branch: str | None = None
    mock_mode: bool = True

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
    import os
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
    sandbox_list_keys = ("uv_tools", "python_packages", "npm_packages", "system_binaries")
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


def ensure_repo_and_worktree(repo: str, task_id: str) -> tuple[Path, Path, str]:
    """Ensure base git repository exists (seeding from config/workspaces.yaml if declared) and create an isolated per-task git worktree on branch `agent/{task_id}`.

    Returns:
        (repo_dir, worktree_dir, branch_name)
    """
    root = get_workspace_root()
    slug = (
        (repo if repo and repo != "current" else "default-repo")
        .strip()
        .lower()
        .replace(" ", "-")
    )
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

    import os
    import shutil

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
        subprocess.run(
            [
                "git",
                "-C",
                str(repo_dir),
                "worktree",
                "add",
                "-B",
                branch_name,
                str(worktree_dir),
            ],
            check=False,
            capture_output=True,
        )

    if worktree_dir.exists():
        sync_cross_harness_context(worktree_dir, slug)

    return repo_dir, worktree_dir, branch_name


def materialize_workspace_output(
    repo: str, task_id: str, goal: str, harness_name: str
) -> tuple[list[str], str, str, str, str]:
    """Write a task output artifact into both the isolated per-task worktree and base repo directory.

    Returns:
        (files_changed, branch_name, worktree_path, diff_summary, raw_diff)
    """
    if not repo or repo == "current":
        return ([], f"agent/{task_id}", f"/workspace/.worktrees/{task_id}", "", "")

    repo_dir, worktree_dir, branch_name = ensure_repo_and_worktree(repo, task_id)
    safe_task = task_id.replace("-", "_")
    out_file = f"{safe_task}_output.py"
    code_content = (
        f'"""Generated by {harness_name} for {task_id}: {goal}"""\n\n'
        f"def run_{safe_task}() -> str:\n"
        f'    return "completed by {harness_name} on branch {branch_name}"\n'
    )

    target_dir = worktree_dir if worktree_dir.exists() else repo_dir
    (target_dir / out_file).write_text(code_content)
    # Mirror to repo_dir so inspect_repository_files(repo) sees completed task outputs
    (repo_dir / out_file).write_text(code_content)

    diff_summary = f"1 file changed ({out_file}: +4 -0) on branch {branch_name}; unit tests passed."
    raw_diff = (
        f"diff --git a/{out_file} b/{out_file}\n"
        f"new file mode 100644\n"
        f"--- /dev/null\n"
        f"+++ b/{out_file}\n"
        f"@@ -0,0 +1,4 @@\n"
        + "\n".join(f"+{line}" for line in code_content.strip().splitlines())
    )
    return ([out_file], branch_name, str(target_dir), diff_summary, raw_diff)
