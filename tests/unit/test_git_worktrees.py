"""Real host-git coverage for worktree isolation and diff collection.

Every other test in the suite runs with `ORCHESTRATOR_DISABLE_HOST_GIT=1`, under
which `ensure_repo_and_worktree` substitutes a `copytree` for `git worktree add`
and `collect_worktree_changes` takes its "no git metadata available" fallback.
That left the production path — the one that actually runs for users — with no
coverage at all: no test had ever executed `git worktree add -B`, and no test had
ever asserted on a real unified diff or real line counts.

These tests use the `real_git_workspace` fixture, which enables host git inside a
disposable workspace root and asserts on teardown that no worktree registrations
leaked. See `tests/conftest.py`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.workers.harnesses.base import (
    collect_worktree_changes,
    ensure_repo_and_worktree,
    release_worktree,
)
from tests.conftest import git, registered_worktrees, worktree_entries


def _current_branch(path: Path) -> str:
    return git("-C", str(path), "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def test_worktrees_are_real_and_isolated_per_task(real_git_workspace: Path):
    """Two tasks on one repo get two real git worktrees on two branches."""
    repo_dir, wt_one, branch_one = ensure_repo_and_worktree("iso-repo", "task-1")
    repo_dir_again, wt_two, branch_two = ensure_repo_and_worktree("iso-repo", "task-2")

    assert repo_dir == repo_dir_again
    assert branch_one == "agent/task-1"
    assert branch_two == "agent/task-2"
    assert wt_one != wt_two

    # The base repo owns a real .git directory; a linked worktree's .git is a
    # *file* pointing back into it. This is the cheapest proof that we went
    # through `git worktree add` rather than the copytree fallback.
    assert (repo_dir / ".git").is_dir()
    assert (wt_one / ".git").is_file()
    assert (wt_two / ".git").is_file()

    assert {p.resolve() for p in registered_worktrees(repo_dir)} == {
        wt_one.resolve(),
        wt_two.resolve(),
    }
    assert _current_branch(wt_one) == "agent/task-1"
    assert _current_branch(wt_two) == "agent/task-2"
    assert _current_branch(repo_dir) == "main"

    # Work in one task is invisible to the other task and to the base repo.
    (wt_one / "only_in_task_one.py").write_text("x = 1\n")
    assert not (wt_two / "only_in_task_one.py").exists()
    assert not (repo_dir / "only_in_task_one.py").exists()


def test_reentry_reuses_the_existing_worktree(real_git_workspace: Path):
    """Re-dispatching the same task_id must not clobber in-progress work."""
    repo_dir, worktree, _ = ensure_repo_and_worktree("idem-repo", "task-9")
    (worktree / "work_in_progress.py").write_text("half = 'done'\n")

    repo_dir_again, worktree_again, branch_again = ensure_repo_and_worktree(
        "idem-repo", "task-9"
    )

    assert repo_dir_again == repo_dir
    assert worktree_again == worktree
    assert branch_again == "agent/task-9"
    assert (worktree_again / "work_in_progress.py").read_text() == "half = 'done'\n"
    assert len(registered_worktrees(repo_dir)) == 1


def test_seed_init_targets_the_base_repo_directly(real_git_workspace: Path):
    """The `seed-init` sentinel initializes the repo without adding a worktree."""
    repo_dir, worktree, branch = ensure_repo_and_worktree("seed-repo", "seed-init")

    assert worktree == repo_dir
    assert branch == "main"
    assert git("-C", str(repo_dir), "rev-parse", "--verify", "HEAD").returncode == 0
    assert registered_worktrees(repo_dir) == []


@pytest.mark.asyncio
async def test_fresh_worktree_reports_no_changes(real_git_workspace: Path):
    """A worktree with no edits yields empty results, not a fabricated summary."""
    _repo_dir, worktree, _branch = ensure_repo_and_worktree("clean-repo", "task-3")

    files, summary, raw_diff = await collect_worktree_changes(worktree)

    assert files == []
    assert summary == ""
    assert raw_diff == ""


@pytest.mark.asyncio
async def test_collect_worktree_changes_produces_a_real_diff(real_git_workspace: Path):
    """Modified and newly added files yield real line counts and a real unified diff."""
    _repo_dir, worktree, _branch = ensure_repo_and_worktree("diff-repo", "task-7")

    # Guard the arithmetic below: if repo seeding ever leaves the worktree
    # dirty, the exact counts asserted later would silently drift.
    baseline_files, _, _ = await collect_worktree_changes(worktree)
    assert baseline_files == [], f"worktree should start clean, got {baseline_files}"

    readme = worktree / "README.md"
    readme.write_text(readme.read_text() + "line A\nline B\n")
    (worktree / "new_module.py").write_text("def f():\n    return 1\n")

    files, summary, raw_diff = await collect_worktree_changes(worktree)

    assert files == ["README.md", "new_module.py"]
    # The fallback branch is what every other test exercises; assert explicitly
    # that we did not land there.
    assert "no git metadata" not in summary
    assert summary == "2 file(s) changed (+4 -0) in task-7."

    assert "diff --git a/README.md b/README.md" in raw_diff
    assert "diff --git a/new_module.py b/new_module.py" in raw_diff
    assert "@@" in raw_diff
    assert "+line A" in raw_diff
    assert "+    return 1" in raw_diff


@pytest.mark.asyncio
async def test_untracked_files_are_diffed_without_being_staged(
    real_git_workspace: Path,
):
    """`git add -A -N` must surface untracked content while leaving the index clean."""
    _repo_dir, worktree, _branch = ensure_repo_and_worktree("untracked-repo", "task-4")

    (worktree / "brand_new.py").write_text("a = 1\nb = 2\nc = 3\n")

    files, summary, raw_diff = await collect_worktree_changes(worktree)

    assert files == ["brand_new.py"]
    assert "(+3 -0)" in summary
    assert "diff --git a/brand_new.py b/brand_new.py" in raw_diff

    # Intent-to-add registers the path but must not stage its contents, or an
    # unrelated later `git commit` would sweep up agent scratch files. Git hides
    # `-N` entries from `diff --cached` entirely, so the evidence is the index
    # entry: the path is recorded against the well-known empty blob.
    empty_blob = "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"
    index_entry = git("-C", str(worktree), "ls-files", "-s", "brand_new.py").stdout
    assert empty_blob in index_entry, index_entry
    assert git("-C", str(worktree), "diff", "--cached", "--numstat").stdout == ""
    assert (worktree / "brand_new.py").read_text() == "a = 1\nb = 2\nc = 3\n"


@pytest.mark.asyncio
async def test_renames_and_deletions_are_reported_by_destination_path(
    real_git_workspace: Path,
):
    """Porcelain `R  old -> new` entries resolve to the destination path."""
    _repo_dir, worktree, _branch = ensure_repo_and_worktree("rename-repo", "task-5")

    (worktree / "doomed.py").write_text("gone = True\n")
    git("-C", str(worktree), "add", "doomed.py")
    git("-C", str(worktree), "commit", "-m", "Add a file to later delete")

    git("-C", str(worktree), "mv", "README.md", "DOCS.md")
    git("-C", str(worktree), "rm", "doomed.py")

    files, summary, raw_diff = await collect_worktree_changes(worktree)

    # The rename is reported at its new path, not as "README.md -> DOCS.md".
    assert "DOCS.md" in files
    assert not any("->" in f for f in files)
    assert "doomed.py" in files
    assert "no git metadata" not in summary
    assert raw_diff


def test_release_worktree_reclaims_the_checkout_and_keeps_the_branch(
    real_git_workspace: Path,
):
    """Cleanup must free the checkout without destroying committed work."""
    repo_dir, worktree, branch = ensure_repo_and_worktree("release-repo", "task-11")

    (worktree / "feature.py").write_text("def feature():\n    return 'shipped'\n")
    git("-C", str(worktree), "add", ".")
    git("-C", str(worktree), "commit", "-m", "Approved via voice")
    committed_sha = git("-C", str(worktree), "rev-parse", "HEAD").stdout.strip()
    assert committed_sha

    assert release_worktree("release-repo", "task-11") is True

    # Checkout gone, registration gone, nothing left prunable.
    assert not worktree.exists()
    assert registered_worktrees(repo_dir) == []
    assert not any(e["prunable"] for e in worktree_entries(repo_dir))

    # The branch and its commit survive, so the work is still recoverable.
    assert git(
        "-C", str(repo_dir), "rev-parse", "--verify", f"refs/heads/{branch}"
    ).stdout.strip() == committed_sha
    blob = git("-C", str(repo_dir), "show", f"{branch}:feature.py").stdout
    assert "return 'shipped'" in blob


def test_release_worktree_is_a_noop_when_nothing_exists(real_git_workspace: Path):
    """Releasing an unknown task must not raise or fabricate work."""
    ensure_repo_and_worktree("noop-repo", "seed-init")
    assert release_worktree("noop-repo", "task-does-not-exist") is False


def test_redispatch_after_release_preserves_prior_commits(real_git_workspace: Path):
    """Re-running a task whose worktree was released must not reset its branch.

    `git worktree add -B` resets the named branch to HEAD. Since a released task
    keeps its branch, using -B here would silently discard everything committed
    at the approval gate.
    """
    _repo_dir, worktree, branch = ensure_repo_and_worktree("revive-repo", "task-12")
    (worktree / "earlier.py").write_text("earlier = True\n")
    git("-C", str(worktree), "add", ".")
    git("-C", str(worktree), "commit", "-m", "Work from the first run")
    original_sha = git("-C", str(worktree), "rev-parse", "HEAD").stdout.strip()

    assert release_worktree("revive-repo", "task-12") is True

    _repo_dir_again, revived, branch_again = ensure_repo_and_worktree(
        "revive-repo", "task-12"
    )

    assert branch_again == branch
    assert revived.exists()
    assert git("-C", str(revived), "rev-parse", "HEAD").stdout.strip() == original_sha
    assert (revived / "earlier.py").read_text() == "earlier = True\n"
