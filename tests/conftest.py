"""Global pytest fixtures ensuring unit and integration tests never mutate the real workspace or project config files."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]

#: Fallback used by `get_workspace_root()` when `ORCHESTRATOR_WORKSPACE_ROOT` is
#: unset. It lives outside any tmp_path and survives across runs, so anything a
#: test writes here leaks permanently.
SHARED_WORKSPACE_FALLBACK = Path(tempfile.gettempdir()) / "adk-sonar-workspaces"


def git(*args: str) -> subprocess.CompletedProcess[str]:
    """Run a git command, capturing output and never raising."""
    return subprocess.run(
        ["git", *args], check=False, capture_output=True, text=True
    )


def worktree_entries(repo_dir: Path) -> list[dict[str, object]]:
    """Parse `git worktree list --porcelain`, excluding the main working tree.

    Each record carries `path`, `branch`, and `prunable`. Git sets `prunable`
    when a registration points at a directory that no longer exists, which is
    exactly the orphaned-worktree condition worth failing on.
    """
    result = git("-C", str(repo_dir), "worktree", "list", "--porcelain")
    if result.returncode != 0:
        return []

    records: list[dict[str, object]] = []
    current: dict[str, object] | None = None
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            current = {
                "path": Path(line.removeprefix("worktree ").strip()),
                "branch": None,
                "prunable": None,
            }
            records.append(current)
        elif current is None:
            continue
        elif line.startswith("branch "):
            current["branch"] = line.removeprefix("branch ").strip()
        elif line.startswith("prunable"):
            current["prunable"] = line.removeprefix("prunable").strip() or "unknown"

    # The first entry is always the main working tree.
    return records[1:]


def registered_worktrees(repo_dir: Path) -> list[Path]:
    """Return the worktree paths git has registered for `repo_dir`, excluding the main one."""
    return [entry["path"] for entry in worktree_entries(repo_dir)]  # type: ignore[misc]




@pytest.fixture(autouse=True)
def _isolate_workspace_and_project_files(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Redirect ORCHESTRATOR_WORKSPACE_ROOT and generated config paths to an isolated temp dir for every test."""
    from app.app_utils.http_client import set_default_http_transport
    from tests.fakes import integrations_transport

    isolated_ws = tmp_path_factory.mktemp("test-workspaces")
    isolated_cfg = tmp_path_factory.mktemp("test-config")
    monkeypatch.setenv("ORCHESTRATOR_WORKSPACE_ROOT", str(isolated_ws))
    monkeypatch.setenv("ORCHESTRATOR_STATE_DIR", str(isolated_cfg / "state"))
    monkeypatch.setenv("ORCHESTRATOR_DOTENV_PATH", str(isolated_cfg / ".env"))
    monkeypatch.setenv("ORCHESTRATOR_LOAD_DOTENV", "0")
    monkeypatch.setenv("ORCHESTRATOR_SKIP_STARTUP_WARMUP", "1")
    monkeypatch.setenv("ORCHESTRATOR_PERSIST_TASKS", "0")
    monkeypatch.setenv("ORCHESTRATOR_DISABLE_HOST_GIT", "1")
    monkeypatch.setenv(
        "ORCHESTRATOR_WORKSPACES_LOCAL_YAML",
        str(isolated_cfg / "workspaces.local.yaml"),
    )

    set_default_http_transport(integrations_transport())

    try:
        import scripts.setup as setup_mod

        monkeypatch.setattr(setup_mod, "MCP_JSON_OUT", isolated_cfg / ".mcp.json")
    except Exception:
        pass

    yield
    set_default_http_transport(None)


@pytest.fixture(autouse=True)
def _block_outbound_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail loudly if a test performs a real outbound HTTP request.

    Harness code must be driven through an injected `httpx.MockTransport` (see
    `tests/fakes.py`). Before the sandbox mock paths were removed from
    production code, several tests were silently making live calls to
    `aiplatform.googleapis.com`; this guard makes that impossible to reintroduce.

    `httpx.MockTransport` is unaffected because it never opens a connection.
    """
    import httpx

    real_async_connect = httpx.AsyncHTTPTransport.handle_async_request
    real_sync_connect = httpx.HTTPTransport.handle_request

    def _forbid(transport, request, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError(
            f"Blocked real network call to {request.url} during tests. "
            "Inject an httpx.MockTransport via SandboxContext(http_transport=...) "
            "or use a fake from tests/fakes.py."
        )

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _forbid)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _forbid)

    yield

    monkeypatch.setattr(
        httpx.AsyncHTTPTransport, "handle_async_request", real_async_connect
    )
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", real_sync_connect)


@pytest.fixture
def real_git_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    """Run host git for real, inside a disposable workspace root.

    Every other test runs with `ORCHESTRATOR_DISABLE_HOST_GIT=1`, which swaps
    `git worktree add` for a `copytree` and leaves worktrees with no `.git`.
    That means the real worktree isolation and diff-collection code paths are
    never exercised. This fixture turns host git back on for a single test.

    Two safety properties matter here, because real `git worktree add` writes
    registration metadata into the *parent* repo's `.git/worktrees/` that
    outlives the worktree directory itself:

    1. The workspace root is pinned inside `tmp_path`, and that is asserted
       against the value `get_workspace_root()` actually resolves, so a missing
       env var can never silently redirect writes to the shared
       `/tmp/adk-sonar-workspaces` fallback.
    2. Teardown explicitly removes and prunes every registered worktree and
       then asserts none survived, rather than just deleting the directory
       tree. A plain `rmtree` would leave stale registrations behind and hide
       the leak.

    Git is also made hermetic: no global or system config, no credential
    prompts, and an explicit committer identity, so results do not depend on
    the developer's machine or CI image.

    Yields:
        The workspace root, equivalent to `get_workspace_root()`.
    """
    root = tmp_path / "real-git-workspaces"
    root.mkdir()

    monkeypatch.setenv("ORCHESTRATOR_WORKSPACE_ROOT", str(root))
    monkeypatch.delenv("ORCHESTRATOR_DISABLE_HOST_GIT", raising=False)

    # Hermetic git: ignore ~/.gitconfig and /etc/gitconfig, never prompt.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "absent-gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(tmp_path / "absent-gitconfig-system"))
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "ADK Sonar Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "ADK Sonar Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.invalid")

    from app.tools.workspace_tools import get_workspace_root

    resolved = get_workspace_root()
    assert resolved == root.resolve(), (
        f"real_git_workspace must be contained in tmp_path, but "
        f"get_workspace_root() resolved to {resolved}"
    )

    try:
        yield root
    finally:
        problems: list[str] = []
        for repo_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            if not (repo_dir / ".git").is_dir():
                continue

            # Inspect before cleaning. `git worktree prune` deletes the evidence,
            # and `git worktree remove --force` exits 0 even when the directory
            # is already gone, so `prunable` is the only reliable signal that a
            # worktree directory was deleted without deregistering it.
            for entry in worktree_entries(repo_dir):
                if entry["prunable"]:
                    problems.append(
                        f"{repo_dir.name}: orphaned registration for "
                        f"{entry['path']} ({entry['prunable']})"
                    )

            for worktree in registered_worktrees(repo_dir):
                git("-C", str(repo_dir), "worktree", "remove", "--force", str(worktree))
            git("-C", str(repo_dir), "worktree", "prune")

            for surviving in registered_worktrees(repo_dir):
                problems.append(f"{repo_dir.name}: registration survived for {surviving}")

        shutil.rmtree(root, ignore_errors=True)
        assert not problems, (
            "Test left git worktrees in an unclean state:\n  "
            + "\n  ".join(problems)
            + "\nRemove worktrees with `git worktree remove` rather than deleting "
            "the directory, which leaves an orphaned registration behind."
        )


@pytest.fixture(scope="session", autouse=True)
def _no_orphaned_worktrees() -> Iterator[None]:
    """Fail the session if tests leaked git worktrees outside their tmp dirs.

    Backstop for the two ways a leak escapes `real_git_workspace`: registering a
    worktree against this repository itself, and falling through to the shared
    `/tmp/adk-sonar-workspaces` root that `get_workspace_root()` uses when
    `ORCHESTRATOR_WORKSPACE_ROOT` is unset.
    """
    before_project = set(registered_worktrees(PROJECT_ROOT))
    fallback_existed = SHARED_WORKSPACE_FALLBACK.exists()
    before_fallback = (
        {p.name for p in SHARED_WORKSPACE_FALLBACK.iterdir()} if fallback_existed else set()
    )

    yield

    after_project = set(registered_worktrees(PROJECT_ROOT))
    new_project = after_project - before_project
    assert not new_project, (
        "Tests registered git worktrees against the project repository: "
        f"{sorted(str(p) for p in new_project)}. Use the `real_git_workspace` "
        "fixture so worktrees are created under tmp_path and cleaned up."
    )

    after_fallback = (
        {p.name for p in SHARED_WORKSPACE_FALLBACK.iterdir()}
        if SHARED_WORKSPACE_FALLBACK.exists()
        else set()
    )
    new_fallback = after_fallback - before_fallback
    assert not new_fallback, (
        f"Tests wrote to the shared workspace fallback {SHARED_WORKSPACE_FALLBACK}: "
        f"{sorted(new_fallback)}. This directory persists across runs. Set "
        "ORCHESTRATOR_WORKSPACE_ROOT to a tmp_path in the failing test."
    )

