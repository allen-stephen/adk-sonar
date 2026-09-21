"""Workspace and repository inspection tools operating directly against the real workspace filesystem and git."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def get_workspace_root() -> Path:
    """Resolves the active workspace directory outside the project tree so IDE git watchers are unaffected."""
    default_root = Path(tempfile.gettempdir()) / "adk-sonar-workspaces"
    root = Path(os.getenv("ORCHESTRATOR_WORKSPACE_ROOT", str(default_root))).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _git_info(repo_dir: Path) -> tuple[str, str]:
    """Returns (branch_name, status_summary) for a directory if it is a git or orchestrator-initialized repo."""
    meta_file = repo_dir / ".orchestrator_repo.json"
    if meta_file.exists() and not (repo_dir / ".git").exists():
        import json

        try:
            meta = json.loads(meta_file.read_text())
            branch = meta.get("branch", "main")
            baseline = set(meta.get("baseline_files", []))
            current_files = {
                str(f.relative_to(repo_dir))
                for f in repo_dir.rglob("*")
                if f.is_file() and not f.name.startswith(".") and ".worktrees" not in f.parts
            }
            diff_files = current_files - baseline
            status = "clean" if not diff_files else f"{len(diff_files)} modified or untracked files"
            return (branch, status)
        except Exception:
            return ("main", "clean")

    if not (repo_dir / ".git").exists():
        return ("untracked", "not a git repository")
    try:
        branch_proc = subprocess.run(
            ["git", "-C", str(repo_dir), "symbolic-ref", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        branch = branch_proc.stdout.strip() or "main"

        status_proc = subprocess.run(
            ["git", "-C", str(repo_dir), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        lines = [line for line in status_proc.stdout.splitlines() if line.strip()]
        status = "clean" if not lines else f"{len(lines)} modified or untracked files"
        return (branch, status)
    except Exception:
        return ("unknown", "unknown")


async def list_repositories() -> str:
    """List all repositories and project directories in the workspace, along with their git branch and working tree status.

    Returns:
        Spoken summary of repositories present in the workspace.
    """
    root = get_workspace_root()
    repos = sorted([d for d in root.iterdir() if d.is_dir() and not d.name.startswith(".")])
    if not repos:
        return (
            f"Your workspace at {root.name} currently has no repositories. "
            "You can create a new repository or clone an existing Git URL using create_or_clone_repository."
        )

    entries: list[str] = []
    for r in repos:
        branch, status = _git_info(r)
        file_count = sum(
            1
            for f in r.rglob("*")
            if f.is_file() and ".git" not in f.parts
        )
        entries.append(f"{r.name} on branch {branch} ({status}, {file_count} files)")

    try:
        from app.api_routes import record_context_surface

        record_context_surface(
            kind="workspace",
            title="WORKSPACE · REPOSITORIES",
            subtitle=f"{len(repos)} repositories in active workspace",
            brand_icon="github",
            badge=f"{len(repos)} repos",
            bullets=entries,
            surface_id="a2ui-ctx-workspace",
        )
    except Exception:
        pass

    return f"You have {len(repos)} repositories in your workspace: " + "; ".join(entries) + "."


async def inspect_repository_files(repo: str, subdirectory: str = "") -> str:
    """List the files inside a repository in the workspace.

    Args:
        repo: Name of the repository directory (e.g. 'agents-cli', 'test-a').
        subdirectory: Optional relative path inside the repository.

    Returns:
        Spoken list of files inside the repository.
    """
    root = get_workspace_root()
    slug = repo.strip().lower().replace(" ", "-")
    repo_dir = root / slug
    if not repo_dir.exists():
        for candidate in root.iterdir():
            if candidate.is_dir() and candidate.name.lower() == repo.strip().lower():
                repo_dir = candidate
                break
        else:
            existing = [d.name for d in root.iterdir() if d.is_dir() and not d.name.startswith(".")]
            avail_msg = f"Existing repositories are: {', '.join(existing)}." if existing else "No repositories exist yet."
            return f"Repository {repo} was not found in the workspace. {avail_msg}"

    target = (repo_dir / subdirectory).resolve() if subdirectory else repo_dir.resolve()
    if not str(target).startswith(str(repo_dir.resolve())) or not target.exists():
        return f"Subdirectory {subdirectory} does not exist in {repo_dir.name}."

    files = [
        str(f.relative_to(repo_dir))
        for f in sorted(target.rglob("*"))
        if f.is_file() and ".git" not in f.parts
    ]
    if not files:
        return f"Repository {repo_dir.name} has no tracked files yet."
    shown = files[:30]
    suffix = f" and {len(files) - 30} more files" if len(files) > 30 else ""
    return f"Files in {repo_dir.name}: {', '.join(shown)}{suffix}."


async def read_workspace_file(repo: str, file_path: str) -> str:
    """Read the contents of a specific file inside a repository in the workspace.

    Args:
        repo: Name of the repository.
        file_path: Relative path to the file within the repository.

    Returns:
        Contents of the requested file.
    """
    root = get_workspace_root()
    slug = repo.strip().lower().replace(" ", "-")
    repo_dir = root / slug
    target = (repo_dir / file_path).resolve()
    if not str(target).startswith(str(repo_dir.resolve())) or not target.is_file():
        return f"File {file_path} was not found in repository {repo}."
    content = target.read_text(errors="replace")
    return f"Contents of {file_path} in {repo_dir.name}:\n{content[:4000]}"


async def create_or_clone_repository(repo_name: str, git_url: str = "") -> str:
    """Create a new git-initialized repository in the workspace, or clone one from a Git URL.

    Args:
        repo_name: Directory name for the repository (e.g. 'test-a' or 'prime-scripts').
        git_url: Optional HTTPS Git URL to clone. If empty, initializes a fresh git repository on branch main.

    Returns:
        Spoken confirmation of the created or cloned repository.
    """
    root = get_workspace_root()
    slug = repo_name.strip().lower().replace(" ", "-")
    repo_dir = root / slug

    if repo_dir.exists():
        branch, status = _git_info(repo_dir)
        return f"Repository {slug} already exists on branch {branch} ({status})."

    if git_url.strip():
        try:
            subprocess.run(
                ["git", "clone", "--depth", "1", git_url.strip(), str(repo_dir)],
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
            )
            return f"Cloned {git_url} into workspace repository {slug}."
        except Exception as exc:
            return f"Failed to clone {git_url}: {exc}"

    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / "README.md").write_text(f"# {slug}\n")
    if os.getenv("ORCHESTRATOR_DISABLE_HOST_GIT", "").lower() in {"1", "true", "yes"}:
        import json

        (repo_dir / ".orchestrator_repo.json").write_text(
            json.dumps({"branch": "main", "baseline_files": []})
        )
    else:
        subprocess.run(["git", "-C", str(repo_dir), "init", "-b", "main"], check=False, capture_output=True)
    return f"Initialized new git repository {slug} on branch main."
