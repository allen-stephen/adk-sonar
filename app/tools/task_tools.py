"""Task orchestration, worktree isolation, and HITL approval tools for the Voice Orchestrator."""

from __future__ import annotations

from typing import Any

from app.tasks import get_task_registry
from app.workers import (
    get_harness_registry,
    get_sandbox_provisioner,
    get_worker_backend,
)

# Recognized spellings for the two execution modes. Anything outside these sets is
# rejected rather than coerced: `execute` grants the harness write access plus
# `Bash`, so silently treating an unrecognized string as `execute` would turn a
# misheard or mistyped read-only request into an autonomous code-writing run.
_PLAN_MODES = frozenset(
    {"plan", "planning", "plan_mode", "plan-mode", "read-only", "readonly", "dry-run"}
)
_EXECUTE_MODES = frozenset({"execute", "execution", "exec", "run", "apply"})


def _resolve_mode(mode: str | None) -> str | None:
    """Normalize a caller-supplied mode, returning None when it is unrecognized."""
    raw = (mode or "").strip().lower()
    if not raw:
        return "execute"
    if raw in _PLAN_MODES:
        return "plan"
    if raw in _EXECUTE_MODES:
        return "execute"
    return None


async def dispatch_task(
    goal: str,
    repo: str = "current",
    mode: str = "execute",
    harness: str | None = None,
    require_approval: bool = False,
    task_id: str | None = None,
) -> str:
    """Dispatch a new long-running coding task to a sandboxed coding harness in an isolated git worktree.

    Args:
        goal: The instructions or objective for the coding task.
        repo: Target repository name or identifier (defaults to 'current').
        mode: Either 'plan' (harness explores and comes back with a plan and clarifying questions before writing code) or 'execute' (harness writes code, runs tests, and creates files).
        harness: Optional coding harness to run inside the sandbox ('claude', 'horizon', or 'antigravity'). Defaults to the active default harness.
        require_approval: If True, the task pauses in 'awaiting_approval' with a diff summary before committing or pushing.
        task_id: Optional explicit task ID (defaults to auto-generated unique ID).

    Returns:
        Spoken confirmation with the assigned task ID, isolated branch, mode, and harness.
    """
    registry = get_task_registry()
    worker = get_worker_backend()
    harness_reg = get_harness_registry()
    resolved_mode = _resolve_mode(mode)
    if resolved_mode is None:
        return (
            f"Cannot dispatch: '{mode}' is not a valid mode. "
            "Use 'plan' to inspect read-only, or 'execute' to apply changes."
        )
    normalized_mode = resolved_mode

    try:
        harness_impl = harness_reg.get(harness)
    except ValueError as exc:
        return f"Cannot dispatch: {exc}"

    try:
        assigned_id, handle = await registry.register(
            goal=goal,
            repo=repo,
            harness=harness_impl.name,
            mode=normalized_mode,
            require_approval=require_approval,
            task_id=task_id,
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
            f"Started {assigned_id} in {normalized_mode} mode on {repo} "
            f"(branch {handle.branch}) using {harness_impl.display_name}."
        )
    except ValueError as exc:
        return f"Cannot dispatch: {exc}"


_VOICE_DIFF_CHAR_BUDGET = 1200
_VOICE_RESPONSE_CHAR_BUDGET = 600


def _summarize_diff_for_voice(raw_diff: str) -> str:
    """Return a bounded excerpt of a unified diff suitable for the voice model context.

    The full `raw_diff` remains on `TaskHandle` for the A2UI screen card, but
    injecting tens of KB of `@@` hunks into the real-time audio model's context
    causes it to either hallucinate file details or read diff syntax aloud.
    """
    stripped = raw_diff.strip()
    if len(stripped) <= _VOICE_DIFF_CHAR_BUDGET:
        return stripped
    excerpt = stripped[:_VOICE_DIFF_CHAR_BUDGET]
    omitted = len(stripped) - _VOICE_DIFF_CHAR_BUDGET
    return f"{excerpt}\n... [diff truncated for voice context; {omitted} more chars on screen]"


def _summarize_response_for_voice(raw_text: str) -> str:
    """Return a bounded excerpt of a harness response suitable for the voice model context."""
    stripped = " ".join(raw_text.strip().split())
    if len(stripped) <= _VOICE_RESPONSE_CHAR_BUDGET:
        return stripped
    excerpt = stripped[:_VOICE_RESPONSE_CHAR_BUDGET].rstrip()
    omitted = len(stripped) - _VOICE_RESPONSE_CHAR_BUDGET
    return f"{excerpt}... [response truncated for voice context; {omitted} more chars available in the artifact reader on screen]"


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
        parts.append(f"Response: {_summarize_response_for_voice(handle.response_text)}")
    elif handle.summary:
        parts.append(_summarize_response_for_voice(handle.summary))

    artifacts = getattr(handle, "artifacts", None) or []
    if artifacts:
        titles = [str(a.get("title") or a.get("name")) for a in artifacts[:4] if isinstance(a, dict)]
        if titles:
            parts.append(f"Deliverable artifacts on screen: {', '.join(titles)}.")

    if handle.diff_summary:
        parts.append(f"Diff summary: {handle.diff_summary}")
    if handle.raw_diff:
        parts.append(f"Diff excerpt:\n{_summarize_diff_for_voice(handle.raw_diff)}")
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
    resolved_mode = _resolve_mode(mode)
    if resolved_mode is None:
        return (
            f"Cannot steer {key}: '{mode}' is not a valid mode. "
            "Use 'plan' to revise the plan, or 'execute' to apply changes."
        )
    normalized_mode = resolved_mode

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

    # Preserve the task's approval gate when steering from plan into execute mode.
    keep_approval = bool(getattr(existing, "require_approval", False))
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
                require_approval=keep_approval,
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
        Spoken confirmation describing what actually happened to the task's branch.
    """
    registry = get_task_registry()
    key = task_id.strip().lower()
    try:
        handle = await registry.approve(key)
        if handle.approval_committed and handle.approval_pushed:
            return (
                f"Approved {handle.task_id}. Committed and pushed branch "
                f"{handle.branch} for {handle.repo}."
            )
        if handle.approval_committed:
            return (
                f"Approved {handle.task_id} and committed branch {handle.branch} for "
                f"{handle.repo}, but the push did not succeed. "
                f"{handle.approval_detail or ''}".strip()
            )
        return (
            f"Approved {handle.task_id}, but nothing was committed on branch "
            f"{handle.branch}. {handle.approval_detail or ''} "
            "Tell the user the changes were not saved.".strip()
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
        t
        for t in all_tasks
        if t.status in {"completed", "failed", "cancelled", "orphaned"}
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


def _first_sentence(text: str | None, fallback: str, max_chars: int = 180) -> str:
    """Extract a single clean sentence capped at `max_chars` for brief spoken notifications."""
    cleaned = " ".join((text or "").strip().split())
    if not cleaned:
        return fallback
    for sep in (". ", "! ", "? "):
        if sep in cleaned:
            first = cleaned.split(sep, 1)[0].strip()
            if first:
                cleaned = first + "."
                break
    if len(cleaned) > max_chars:
        return cleaned[: max_chars - 3].rstrip() + "..."
    return cleaned


def format_task_completion_for_voice(handle: Any) -> str:
    """Format a concise 1-sentence headline payload for spoken WHEN_IDLE delivery (details remain on the A2UI card and in get_task_result)."""
    branch_label = handle.branch or f"agent/{handle.task_id}"
    if handle.status == "awaiting_input":
        headline = _first_sentence(
            handle.summary or handle.response_text,
            f"Finished planning for {handle.repo}.",
        )
        top_question = (
            handle.questions[0]
            if handle.questions
            else "Ready for your direction on the plan."
        )
        return (
            f"{handle.task_id} on {handle.repo} ({handle.harness}) finished planning: {headline} "
            f"Questions for you: {top_question} "
            "(Speak at most ONE or TWO short sentences summarizing the core question; the full plan is already on the user's screen. Do NOT call steer_task yet.)"
        )
    if handle.status == "awaiting_approval":
        file_count = len(handle.files_changed or [])
        files_part = (
            f" Files changed: {', '.join(handle.files_changed[:3])}."
            if handle.files_changed
            else ""
        )
        count_phrase = f"{file_count} file(s) modified" if file_count else (handle.diff_summary or "changes staged")
        return (
            f"{handle.task_id} on {handle.repo} ({handle.harness}) has changes ready for review on branch {branch_label} ({count_phrase}).{files_part} "
            "(Speak at most ONE short sentence letting the user know the diff is ready on screen and asking if they want to commit and push; do NOT call approve_task yet.)"
        )
    if handle.status == "failed":
        err_headline = _first_sentence(
            handle.error or handle.summary,
            "Task failed in the sandbox.",
            max_chars=160,
        )
        return (
            f"{handle.task_id} on {handle.repo} ({handle.harness}) failed: {err_headline} "
            "(Speak ONE plain sentence stating the issue without file paths or stack traces, and offer the next step.)"
        )
    headline = _first_sentence(
        handle.summary,
        "All requested changes verified.",
    )
    if handle.files_changed:
        files_part = (
            f" Modified {len(handle.files_changed)} file(s): "
            f"{', '.join(handle.files_changed[:3])}."
        )
        return (
            f"{handle.task_id} on {handle.repo} ({handle.harness}, branch {branch_label}) completed: "
            f"{headline}{files_part} "
            f"(Speak at most ONE short sentence announcing that {handle.task_id} finished; the full summary and worktree on {branch_label} are on the user's screen.)"
        ).strip()
    return (
        f"{handle.task_id} on {handle.repo} ({handle.harness}, branch {branch_label}) completed: "
        f"{headline} "
        f"(Speak at most ONE short sentence announcing that {handle.task_id} finished; the details are on the user's screen.)"
    ).strip()

