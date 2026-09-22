"""Local worker backend running configurable coding harnesses via git worktrees on the host machine."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from collections.abc import Callable
from pathlib import Path

from app.workers.base import WorkerBackend, WorkerExecutionResult
from app.workers.harnesses import (
    CodingHarness,
    HarnessEvent,
    HarnessNotProvisionedError,
    SandboxContext,
    get_coding_harness,
    get_sandbox_provisioner,
)

logger = logging.getLogger(__name__)


class LocalWorker(WorkerBackend):
    """Executes a coding harness locally in an isolated git worktree.

    Useful for developers testing on their personal machines where Claude Code,
    Antigravity CLI, or a local ADK Long Horizon server is available.
    """

    def __init__(
        self,
        worktrees_dir: Path | None = None,
        default_harness: str | None = None,
    ) -> None:
        from app.tools.workspace_tools import get_workspace_root

        self.worktrees_dir = worktrees_dir or (get_workspace_root() / ".worktrees")
        self.default_harness = default_harness
        self._processes: dict[str, asyncio.subprocess.Process] = {}

    def _find_claude_binary(self) -> str | None:
        return shutil.which("claude")

    def _find_harness_binary(self, harness: CodingHarness) -> str | None:
        for candidate in (harness.binary_name, *harness.fallback_binaries):
            found = shutil.which(candidate)
            if found:
                return found
        return None

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
        harness_impl = get_coding_harness(harness or self.default_harness)
        task_dir = self.worktrees_dir / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        context = SandboxContext(
            user_id="local_user",
            sandbox_name="local-worktree",
            worktree_dir=task_dir,
            branch=f"agent/{task_id}",
        )

        try:
            await get_sandbox_provisioner().ensure_provisioned(
                harness_impl,
                context,
                on_event=on_event,
                task_id=task_id,
            )
        except HarnessNotProvisionedError as exc:
            err_msg = str(exc)
            return WorkerExecutionResult(
                exit_code=1,
                summary=err_msg,
                response_text=err_msg,
                error=err_msg,
                branch=context.branch,
                worktree_path=str(task_dir),
                harness=harness_impl.name,
                events=[err_msg],
            )

        if harness_impl.execution_mode == "sandbox_a2a":
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

        binary = self._find_harness_binary(harness_impl)
        if not binary:
            return WorkerExecutionResult(
                exit_code=1,
                summary=(
                    f"{harness_impl.display_name} CLI ({harness_impl.binary_name}) not found on local PATH. "
                    f"Please install {harness_impl.display_name} or switch WORKER_BACKEND/CODING_HARNESS."
                ),
                error=f"{harness_impl.binary_name} binary not found",
                harness=harness_impl.name,
            )

        cmd = harness_impl.build_command(
            goal=goal,
            repo=repo,
            task_id=task_id,
            binary=binary,
        )
        logger.info(
            "Executing local task %s [%s] in %s: %s",
            task_id,
            harness_impl.name,
            task_dir,
            " ".join(cmd),
        )

        events_log: list[str] = []
        final_summary: str | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(task_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={
                    **os.environ,
                    **harness_impl.build_env(),
                },
            )
            self._processes[task_id] = proc

            if proc.stdout:
                while True:
                    raw_line = await proc.stdout.readline()
                    if not raw_line:
                        break
                    decoded = raw_line.decode("utf-8", errors="replace")
                    ev = harness_impl.parse_stream_line(decoded, task_id)
                    if ev:
                        events_log.append(ev.message)
                        if on_event:
                            on_event(ev)
                        if ev.kind == "completed":
                            final_summary = ev.message

            _, stderr = await proc.communicate()
            exit_code = proc.returncode or 0

            if exit_code == 0:
                summary = (
                    final_summary
                    or f"Task {task_id} completed successfully via {harness_impl.display_name}."
                )
            else:
                summary = f"Task {task_id} failed with exit code {exit_code} ({harness_impl.display_name})."

            return WorkerExecutionResult(
                exit_code=exit_code,
                summary=summary,
                error=stderr.decode("utf-8", errors="replace")
                if exit_code != 0
                else None,
                harness=harness_impl.name,
                events=events_log,
            )
        except asyncio.CancelledError:
            await self.cancel_task(task_id)
            return WorkerExecutionResult(
                exit_code=-1,
                summary=f"Task {task_id} was cancelled.",
                harness=harness_impl.name,
                events=events_log,
            )
        finally:
            self._processes.pop(task_id, None)

    async def cancel_task(self, task_id: str) -> bool:
        proc = self._processes.get(task_id)
        if proc and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            except TimeoutError:
                proc.kill()
            return True
        return False
