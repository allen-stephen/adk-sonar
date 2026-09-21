"""Task orchestration, worktree isolation, and HITL approval tools for the Voice Orchestrator."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator

from app.tasks import get_task_registry
from app.workers import (
    get_harness_registry,
    get_sandbox_provisioner,
    get_worker_backend,
)


async def dispatch_task(
    goal: str,
    repo: str = "current",
    mode: str = "execute",
    harness: str | None = None,
    require_approval: bool = False,
) -> str:
    """Dispatch a new long-running coding task to a sandboxed coding harness in an isolated git worktree.

    Args:
        goal: The instructions or objective for the coding task.
        repo: Target repository name or identifier (defaults to 'current').
        mode: Either 'plan' (harness explores and comes back with a plan and clarifying questions before writing code) or 'execute' (harness writes code, runs tests, and creates files).
        harness: Optional coding harness to run inside the sandbox ('claude', 'horizon', or 'antigravity'). Defaults to the active default harness.
        require_approval: If True, the task pauses in 'awaiting_approval' with a diff summary before committing or pushing.

    Returns:
        Spoken confirmation with the assigned task ID, isolated branch, mode, and harness.
    """
    registry = get_task_registry()
    worker = get_worker_backend()
    harness_reg = get_harness_registry()
    normalized_mode = "plan" if mode.strip().lower() == "plan" else "execute"

    try:
        harness_impl = harness_reg.get(harness)
    except ValueError as exc:
        return f"Cannot dispatch: {exc}"

    try:
        task_id, handle = await registry.register(
            goal=goal,
            repo=repo,
            harness=harness_impl.name,
            mode=normalized_mode,
            task_coro_fn=lambda tid, on_ev: worker.execute_task(
                goal=goal,
                repo=repo,
                task_id=tid,
                mode=normalized_mode,
                require_approval=require_approval,
                harness=harness_impl.name,
                on_event=on_ev,
            ),
        )
        return (
            f"Started {task_id} in {normalized_mode} mode on {repo} "
            f"(branch {handle.branch}) using {harness_impl.display_name}."
        )
    except ValueError as exc:
        return f"Cannot dispatch: {exc}"


async def get_task_result(task_id: str) -> str:
    """Get the detailed response, diff summary, changed files, error trace, and any clarifying questions or pending approval gates from a task.

    Args:
        task_id: The task identifier, such as 'task-1'.

    Returns:
        Structured summary of the task output, diff, questions, or errors for the voice layer to synthesize.
    """
    registry = get_task_registry()
    handle = await registry.get(task_id.strip().lower())
    if handle is None:
        return f"No task found with ID {task_id}."

    if handle.status == "running":
        return (
            f"{handle.task_id} is still running in {handle.mode} mode on branch "
            f"{handle.branch} ({handle.elapsed_seconds} seconds elapsed)."
        )

    parts = [
        f"{handle.task_id} (harness: {handle.harness}, branch: {handle.branch}) status is {handle.status}."
    ]
    if handle.response_text:
        parts.append(f"Response: {handle.response_text}")
    elif handle.summary:
        parts.append(handle.summary)

    if handle.diff_summary:
        parts.append(f"Diff summary: {handle.diff_summary}")
    if handle.raw_diff:
        parts.append(f"Raw git diff:\n{handle.raw_diff}")
    if handle.files_changed:
        parts.append(f"Files changed: {', '.join(handle.files_changed)}.")
    if handle.questions:
        parts.append(
            f"Questions for you: {' '.join(handle.questions)} "
            "(STOP calling tools now and ask the user these questions aloud; do NOT call steer_task yet.)"
        )
    if handle.awaiting_approval and handle.pending_action:
        parts.append(
            f"HITL Approval Gate: Waiting for explicit user voice approval before executing: {handle.pending_action}. "
            "(STOP calling tools now and ask the user for confirmation; do NOT call approve_task yet.)"
        )
    if handle.error:
        parts.append(f"Error trace:\n{handle.error}")

    return " ".join(parts)


async def steer_task(
    task_id: str,
    instruction: str,
    mode: str = "execute",
    harness: str | None = None,
) -> str:
    """Steer a task or answer its clarifying questions using session resumption or cross-harness handoff in its shared worktree.

    Args:
        task_id: The ID of the task to steer or resume (e.g. 'task-1').
        instruction: Your answers, feedback, or updated direction.
        mode: Mode for the resumed run ('execute' by default to apply changes, or 'plan').
        harness: Optional coding harness to switch this task onto ('claude', 'horizon', or 'antigravity') while preserving the same worktree and files.

    Returns:
        Spoken confirmation that the task session has been resumed with the new instructions.
    """
    from app.workers.harnesses.prompts import build_cross_harness_handoff

    registry = get_task_registry()
    harness_reg = get_harness_registry()
    worker = get_worker_backend()
    key = task_id.strip().lower()
    normalized_mode = "plan" if mode.strip().lower() == "plan" else "execute"

    existing = await registry.get(key)
    if existing is None:
        return f"Task {task_id} was not found."

    prev_harness_name = existing.harness
    try:
        target_harness = (
            harness_reg.get(harness)
            if harness
            else harness_reg.get(prev_harness_name)
        )
    except ValueError as exc:
        return str(exc)

    switched_harness = target_harness.name != prev_harness_name
    if switched_harness:
        handoff_block = build_cross_harness_handoff(
            previous_harness=prev_harness_name,
            new_harness=target_harness.name,
            previous_summary=existing.summary,
            files_changed=existing.files_changed,
            worktree_path=existing.worktree_path,
            branch=existing.branch,
        )
        resumed_goal = (
            f"{handoff_block}\n\n"
            f"Original goal: {existing.goal}\n"
            f"Follow-up instruction: {instruction}"
        )
        session_to_use = None
    else:
        resumed_goal = f"{existing.goal}\nFollow-up instruction: {instruction}"
        session_to_use = existing.claude_session_id

    try:
        await registry.resume(
            key,
            instruction=instruction,
            mode=normalized_mode,
            harness=target_harness.name,
            task_coro_fn=lambda tid, on_ev: worker.execute_task(
                goal=resumed_goal,
                repo=existing.repo,
                task_id=tid,
                mode=normalized_mode,
                harness=target_harness.name,
                session_id=session_to_use,
                on_event=on_ev,
            ),
        )
        if switched_harness:
            return (
                f"Switched {key} from {prev_harness_name} to {target_harness.display_name} "
                f"on shared branch {existing.branch} and resumed in {normalized_mode} mode."
            )
        return f"Resumed {key} in {normalized_mode} mode with your updated instructions."
    except ValueError as exc:
        return str(exc)


async def approve_task(task_id: str) -> str:
    """Approve a task that is waiting at a human-in-the-loop (HITL) approval gate to commit, push, or open a pull request.

    Only call this tool when the user has explicitly approved or confirmed the action.

    Args:
        task_id: The ID of the task to approve (e.g. 'task-1' or 'task-102').

    Returns:
        Spoken confirmation that the changes on the task's branch were committed and pushed.
    """
    registry = get_task_registry()
    key = task_id.strip().lower()
    try:
        handle = await registry.approve(key)
        return (
            f"Approved {handle.task_id}. Committed and pushed branch "
            f"{handle.branch} for {handle.repo}."
        )
    except ValueError as exc:
        return str(exc)


async def list_harnesses() -> str:
    """List all available coding harnesses in the sandbox and which one is currently default.

    Returns:
        Spoken summary of available coding harnesses and the active default.
    """
    harness_reg = get_harness_registry()
    default_h = harness_reg.default_harness
    names = [f"{h.display_name} ({h.name})" for h in harness_reg.list_all()]
    return (
        f"Available sandbox coding harnesses are: {', '.join(names)}. "
        f"The current default harness is {default_h.display_name}."
    )


async def set_coding_harness(harness_name: str) -> str:
    """Set the default sandbox coding harness for future tasks.

    Args:
        harness_name: Name of the harness to use by default ('claude', 'horizon', or 'antigravity').

    Returns:
        Spoken confirmation of the new default coding harness.
    """
    harness_reg = get_harness_registry()
    try:
        selected = harness_reg.set_default(harness_name)
        get_sandbox_provisioner().warm_up_in_background(selected)
        return f"Default coding harness is now set to {selected.display_name}."
    except ValueError as exc:
        return str(exc)


async def list_tasks() -> str:
    """List all currently running, awaiting-input, awaiting-approval, and recently finished coding tasks.

    Returns:
        Spoken summary of active and finished tasks including their harness, branch, and latest progress.
    """
    registry = get_task_registry()
    all_tasks = await registry.list_all()
    if not all_tasks:
        return "No tasks are currently running or queued."

    active = [t for t in all_tasks if t.status == "running"]
    waiting_input = [t for t in all_tasks if t.status == "awaiting_input"]
    waiting_approval = [t for t in all_tasks if t.status == "awaiting_approval"]
    finished = [
        t for t in all_tasks if t.status in {"completed", "failed", "cancelled"}
    ]

    parts: list[str] = []
    if active:
        active_desc = []
        for t in active:
            desc = f"{t.task_id} on {t.harness} (branch {t.branch}), running for {t.elapsed_seconds} seconds on {t.goal}"
            if t.latest_update:
                desc += f" ({t.latest_update})"
            active_desc.append(desc)
        parts.append(f"{len(active)} running: " + "; ".join(active_desc))
    else:
        parts.append("No active tasks")

    if waiting_input:
        wait_desc = [
            f"{t.task_id} ({t.harness}) waiting for your answer on {t.repo}"
            for t in waiting_input
        ]
        parts.append("Awaiting your input: " + "; ".join(wait_desc))

    if waiting_approval:
        app_desc = [
            f"{t.task_id} ({t.harness}) ready for diff approval on branch {t.branch}"
            for t in waiting_approval
        ]
        parts.append("Awaiting your approval: " + "; ".join(app_desc))

    if finished:
        fin_desc = [
            f"{t.task_id} ({t.harness}) {t.status}" for t in finished[-3:]
        ]
        parts.append("Recent tasks: " + "; ".join(fin_desc))

    return ". ".join(parts) + "."


async def cancel_task(task_id: str) -> str:
    """Cancel a running task by its task ID.

    Args:
        task_id: The ID of the task to cancel, e.g. 'task-1'.

    Returns:
        Spoken confirmation of cancellation.
    """
    registry = get_task_registry()
    worker = get_worker_backend()
    key = task_id.strip().lower()
    cancelled = await registry.cancel(key)
    await worker.cancel_task(key)
    if cancelled:
        return f"Cancelled {key}. Branch and logs are preserved."
    return f"Could not cancel {key}. It may have already finished or does not exist."


async def watch_tasks() -> AsyncGenerator[str, None]:
    """Streaming tool that monitors running tasks and yields updates as they finish, ask questions, or request diff approval.

    Yields:
        Spoken notification when a task completes, pauses with questions, requests diff approval, fails, or cancels.
    """
    registry = get_task_registry()
    while True:
        handle = await registry.wait_next_unconsumed(timeout_s=10.0)
        if handle:
            if handle.status == "awaiting_input":
                q_str = " ".join(handle.questions)
                yield f"{handle.task_id} on {handle.harness} finished planning for {handle.repo} and has questions: {q_str}"
            elif handle.status == "awaiting_approval":
                diff_str = handle.diff_summary or "Changes are ready."
                yield f"{handle.task_id} on {handle.harness} has changes ready on branch {handle.branch}. {diff_str} Ready for your approval to commit and push."
            else:
                status_text = (
                    "finished" if handle.status == "completed" else handle.status
                )
                summary = handle.summary or ""
                yield f"{handle.task_id} on {handle.harness} just {status_text}. {summary}"
        else:
            await asyncio.sleep(1)


async def stop_streaming(function_name: str) -> str:
    """Stop an ongoing streaming tool such as watch_tasks.

    Args:
        function_name: The name of the streaming function to stop, e.g. 'watch_tasks'.

    Returns:
        Spoken confirmation that streaming stopped.
    """
    return f"Stopped streaming for {function_name}."
