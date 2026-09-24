# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Live voice orchestrator agent for remote sandboxed coding harnesses, workspace discovery, and MCP integrations."""

from __future__ import annotations

import asyncio
import functools
import inspect
import logging
import os
from typing import Any

from google.adk.agents import Agent
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.apps import App
from google.adk.models import Gemini
from google.adk.tools import FunctionTool
from google.adk.tools.preload_memory_tool import PreloadMemoryTool
from google.genai import types

from app.integrations import (
    configure_integration,
    get_integration_registry,
    list_integrations,
)
from app.tools.grounding_tools import search_maps_grounded, search_web_grounded
from app.tools.integration_tools import (
    calendar_events,
    drive_files,
    get_current_time_and_timezone,
    get_runtime_preferences,
    github_operations,
    gmail_messages,
    slack_messages,
    spotify_playback,
)
from app.tasks.persistence import is_store_active, mark_handle_event_seen
from app.tools.task_tools import (
    approve_task,
    cancel_task,
    dispatch_task,
    format_task_completion_for_voice,
    get_task_result,
    list_harnesses,
    list_tasks,
    set_coding_harness,
    steer_task,
)
from app.tools.workspace_tools import (
    create_or_clone_repository,
    inspect_repository_files,
    list_repositories,
    read_workspace_file,
)

# Live model via Google AI Studio
MODEL = os.getenv("LIVE_MODEL", "gemini-3.8-live")


def _extract_dispatched_or_steered_task_id(
    func_name: str,
    result: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> str | None:
    """Resolve the task_id started or resumed by dispatch_task / steer_task."""
    if not isinstance(result, str):
        return None
    if func_name == "dispatch_task" and result.startswith("Started task-"):
        parts = result.split()
        return parts[1].strip().lower() if len(parts) > 1 else None
    if func_name == "steer_task" and (
        result.startswith("Resumed ") or result.startswith("Switched ")
    ):
        raw_id = kwargs.get("task_id") or (args[0] if args else None)
        return str(raw_id).strip().lower() if raw_id else None
    return None


def non_blocking_tool(
    func: Any,
    scheduling: types.FunctionResponseScheduling = types.FunctionResponseScheduling.WHEN_IDLE,
) -> FunctionTool:
    """Wrap an async function as a Gemini Live API NON_BLOCKING streaming FunctionTool.

    CRITICAL ADK LIVE SCHEDULING DETAIL (`google/adk/flows/llm_flows/functions.py` lines 1287 & 1629-1635):
    When an async generator tool is called, ADK immediately emits a placeholder
    `{'status': 'The function is running asynchronously and the results are pending.'}` stamped
    with `tool.response_scheduling`. If `tool.response_scheduling` is `WHEN_IDLE`, Gemini Live
    triggers a SECOND model turn as soon as the initial `function_call` utterance ends, causing
    the voice agent to repeat "Kicking off task..." twice verbatim!
    Therefore:
    1. `tool.response_scheduling` MUST be `FunctionResponseScheduling.SILENT` so ADK's initial
       `pending` placeholder (and the immediate `Started task-N` context update) never triggers
       a duplicate spoken turn.
    2. Only the actual completion `FunctionResponse` yielded after the work finishes sets
       `scheduling=scheduling` (`WHEN_IDLE`), which overrides `tool.response_scheduling` at
       `functions.py:1605` and prompts the model to speak once the result is ready.
    """
    if not inspect.isasyncgenfunction(func):
        func_name = getattr(func, "__name__", "")

        @functools.wraps(func)
        async def _async_gen_wrapper(*args: Any, **kwargs: Any):
            result = await func(*args, **kwargs)
            task_id = _extract_dispatched_or_steered_task_id(
                func_name, result, args, kwargs
            )
            if not task_id:
                yield types.FunctionResponse(
                    name=func_name,
                    response={"result": result},
                    scheduling=scheduling,
                )
                return

            from app.tasks import get_task_registry

            handle = await get_task_registry().get(task_id)
            if handle is None or handle.async_task is None:
                yield types.FunctionResponse(
                    name=func_name,
                    response={"result": result},
                    scheduling=types.FunctionResponseScheduling.SILENT,
                )
                return

            handle.awaited_by_live_tool = True
            try:
                # Yield #1 (SILENT): record task_id and branch in model context without triggering a 2nd spoken turn after kickoff.
                yield types.FunctionResponse(
                    name=func_name,
                    response={"result": result},
                    scheduling=types.FunctionResponseScheduling.SILENT,
                )
                await asyncio.shield(handle.async_task)
                await mark_handle_event_seen(handle)
                if (
                    not getattr(handle, "dismissed", False)
                    and handle.status
                    in {"completed", "failed", "awaiting_input", "awaiting_approval"}
                ):
                    # Yield #2 (WHEN_IDLE): trigger a single concise spoken headline once the sandbox run finishes or hits a gate.
                    yield types.FunctionResponse(
                        name=func_name,
                        response={"result": format_task_completion_for_voice(handle)},
                        scheduling=scheduling,
                    )
            finally:
                handle.awaited_by_live_tool = False

        _async_gen_wrapper.__signature__ = inspect.signature(func)  # type: ignore[attr-defined]
        tool = FunctionTool(_async_gen_wrapper)
    else:
        tool = FunctionTool(func)
    # Set tool-wide default to SILENT so ADK's initial `pending` placeholder (`functions.py:1287`)
    # does not force Gemini Live to repeat the kickoff announcement a second time.
    tool.response_scheduling = types.FunctionResponseScheduling.SILENT
    return tool


# Only mount raw McpToolsets onto the real-time voice agent if explicitly enabled;
# otherwise root_agent uses the direct async integration_tools (which never block or timeout the WebSocket).
_mcp_toolsets = (
    get_integration_registry().build_adk_mcp_toolsets(only_with_credentials=True)
    if os.getenv("ENABLE_LIVE_MCP_TOOLSETS", "").lower() in {"1", "true", "yes"}
    else []
)

# Maximum age for resumable checkpoints (awaiting_input / awaiting_approval) in the voice briefing.
_BRIEFING_CHECKPOINT_MAX_AGE_SECONDS = 86400.0


async def _hydrate_task_store_callback(callback_context: Any) -> None:
    """Hydrates persisted tasks and drains unseen completion/error events once per session turn."""
    try:
        from app.tasks import get_task_registry

        reg = get_task_registry()
        # Capture the active LiveRequestQueue if running inside a Gemini Live session
        inv_ctx = None
        if hasattr(callback_context, "get_invocation_context"):
            try:
                inv_ctx = callback_context.get_invocation_context()
            except Exception:
                inv_ctx = None
        if inv_ctx is None:
            inv_ctx = getattr(callback_context, "_invocation_context", None)
        if inv_ctx is not None and getattr(inv_ctx, "live_request_queue", None) is not None:
            reg.active_live_queue = inv_ctx.live_request_queue

        await reg.list_all()

        if is_store_active():
            from app.store.task_store import get_task_store

            store = get_task_store()
            unseen = await store.get_unseen_events("default-user", limit=10)
            if unseen:
                max_event_id = max(ev.id for ev in unseen)
                by_short: dict[str, str] = {}
                for ev in unseen:
                    meta = ev.metadata_json or {}
                    short_id = meta.get("short_id")
                    if not short_id:
                        continue
                    handle = reg._tasks.get(short_id)
                    if handle is not None and (
                        getattr(handle, "dismissed", False)
                        or handle.status in {"orphaned", "cancelled", "awaiting_input", "awaiting_approval"}
                    ):
                        continue
                    repo = meta.get("repo") or (handle.repo if handle else "workspace")
                    if ev.kind == "completed":
                        by_short[short_id] = (
                            f"- {short_id} on {repo}: finished while you were away ({(ev.message or '').strip()[:160]})"
                        )
                    elif ev.kind == "error":
                        by_short[short_id] = (
                            f"- {short_id} on {repo}: failed while you were away ({(ev.message or '').strip()[:160]})"
                        )
                if by_short:
                    reg._unseen_briefing_items = list(by_short.values())[-3:]  # type: ignore[attr-defined]
                await store.advance_cursor("default-user", max_event_id)
    except Exception:
        pass


def _build_task_briefing() -> str:
    """Dynamically builds a bounded, non-repetitive spoken briefing of active and unseen tasks."""
    try:
        import time
        from app.tasks import get_task_registry

        reg = get_task_registry()
        tasks = reg.snapshot_tasks()
        now = time.time()

        active_items: list[str] = []
        for t in tasks:
            if getattr(t, "dismissed", False) or t.status in {"orphaned", "cancelled"}:
                continue
            age = now - (t.ended_at_ts or t.created_at_ts or now)
            if t.status == "awaiting_approval" and age <= _BRIEFING_CHECKPOINT_MAX_AGE_SECONDS:
                active_items.append(
                    f"- {t.task_id} on {t.repo}: changes ready for review and diff approval on branch {t.branch}"
                )
            elif t.status == "awaiting_input" and age <= _BRIEFING_CHECKPOINT_MAX_AGE_SECONDS:
                qs = ", ".join(t.questions[:2]) if t.questions else "clarification required"
                active_items.append(
                    f"- {t.task_id} on {t.repo}: waiting for user guidance ({qs[:160]})"
                )
            elif (
                t.status == "running"
                and not getattr(t, "from_history", False)
                and t.async_task is not None
                and not t.async_task.done()
            ):
                active_items.append(
                    f"- {t.task_id} on {t.repo}: currently running ({t.harness})"
                )

            elif (
                t.status == "completed"
                and not getattr(t, "from_history", False)
                and not getattr(t, "dismissed", False)
                and age <= 600.0
            ):
                files_note = (
                    f", modified {', '.join(t.files_changed[:3])}"
                    if t.files_changed
                    else ""
                )
                short_summary = " ".join((t.summary or "changes applied").strip().split())[:160]
                active_items.append(
                    f"- {t.task_id} on {t.repo} (branch {t.branch}, harness {t.harness}): "
                    f"recently completed work pass ({short_summary}{files_note}) — "
                    f"warm session, can be continued via `steer_task(task_id='{t.task_id}', ...)` if the user asks for follow-up work"
                )

        active_items = active_items[:4]

        # Drain unseen completion/failure events captured from TaskStore cursor watermark
        unseen_items: list[str] = list(getattr(reg, "_unseen_briefing_items", []) or [])
        if unseen_items:
            reg._unseen_briefing_items = []  # type: ignore[attr-defined]
            active_items.extend(unseen_items[:3])

        if not active_items:
            return ""

        return (
            "ACTIVE & RECENT BACKGROUND CODING TASKS (SITUATIONAL AWARENESS):\n"
            + "\n".join(active_items)
            + "\nIf the user opens with a greeting or asks for status, naturally brief them in one or two sentences.\n\n"
        )
    except Exception:
        return ""


def build_orchestrator_instruction(readonly_context: ReadonlyContext) -> str:
    """Dynamically constructs the system instruction with the user's active timezone, local time, and voice settings."""
    prefs = get_runtime_preferences()
    task_briefing = _build_task_briefing()
    style_map = {
        "concise": "Keep turns ultra-brief and operational (one to two sentences max) with zero conversational filler.",
        "balanced": "Keep turns clear and natural (two to three sentences) with helpful context.",
        "detailed": "Provide thorough, well-structured spoken explanations (three to four sentences) with key details.",
    }
    style_rule = style_map.get(prefs["speech_style"], style_map["concise"])

    return (
        "You are Sonar, a sharp, warm, and pragmatic live voice collaborator.\n"
        "You talk like a trusted human peer and co-pilot—natural, grounded, and direct—while seamlessly handling "
        "everyday questions, music, places, personal schedule and inbox, or background agent runs in a cloud sandbox "
        "(across Claude Code, ADK Long Horizon, and Antigravity).\n\n"
        "GREETINGS & CONVERSATIONAL PERSONA:\n"
        "- When the user greets you casually (such as 'Hi there', 'Hey', or 'Good morning'), reply like a warm, natural human peer in one short sentence (for example: 'Hey! What's on your mind?' or 'Hey there — what are we getting into?').\n"
        "- NEVER use scripted assistant clichés or recite capability menus (NEVER say 'How can I help you with your workspace or coding tasks today?', 'How may I assist you today?', or 'I am your engineering and workspace orchestrator').\n"
        "- Only mention background tasks during a greeting if there is an active or newly finished run in your situational briefing below that genuinely needs their attention.\n\n"
        f"{task_briefing}"
        "USER TIMEZONE & TEMPORAL CONTEXT:\n"
        f"- Active User Timezone: {prefs['timezone']} ({prefs['tz_abbrev']}, {prefs['tz_offset']})\n"
        f"- Current Local Date & Time: {prefs['local_time_formatted']}\n"
        "- Always report times, calendar schedules, and relative deadlines in the user's active timezone.\n\n"
        "DOWNSTREAM TOOL & INTEGRATION ROUTING MATRIX:\n"
        "Always invoke the matching downstream tool before answering domain requests:\n"
        "- Current time, date, or timezone questions -> call `get_current_time_and_timezone()`.\n"
        "- Music, playlists, songs, audio playback, or focus tracks -> call `spotify_playback(action=..., query=...)`.\n"
        "- Realtime web facts, breaking news, weather, stock prices, or external SDK docs -> call `search_web_grounded(query=...)`.\n"
        "- Local places, coffee shops, restaurants, directions, or business hours -> call `search_maps_grounded(query=..., near_location=...)`.\n"
        "- Schedule, calendar meetings, free/busy availability, or booking invites -> call `calendar_events(action=..., query=..., time_window=...)`.\n"
        "- Inbox threads, unread emails, or drafting email replies -> call `gmail_messages(action=..., query=..., recipient=...)`.\n"
        "- Design specs, PRDs, architecture documents, or spreadsheets in Google Drive -> call `drive_files(action=..., query=...)`.\n"
        "- Slack channels, team chat threads, incident updates, or posting status messages -> call `slack_messages(action=..., channel=..., message=...)`.\n"
        "- Open pull requests, CI check statuses, or GitHub issues (inspection only) -> call `github_operations(action=..., repo=..., number=...)`.\n"
        "- Multi-step software engineering, codebase architecture/review, cloud evaluation/deployment (`agents-cli`), or deep research and artifact synthesis -> call `dispatch_task(goal=..., repo=..., mode=..., harness=..., require_approval=...)`.\n\n"
        "ASYNC TOOL ACKNOWLEDGMENT vs. SYNCHRONOUS TOOL EXECUTION:\n"
        "- Asynchronous Non-Blocking Tools (`dispatch_task`, `steer_task`, `approve_task`, `search_web_grounded`, `search_maps_grounded`): "
        "When you invoke one of these five tools, speak a brief one-sentence kickoff acknowledgment ONCE in that same turn (for example, 'Checking the web for the latest Python release notes.' "
        "or 'On it — having Claude draft a plan for auth service now.'). "
        "CRITICAL: When `The function is running asynchronously and the results are pending` or `Started task-N` is returned, NEVER repeat your kickoff sentence a second time — stay completely silent until the final completed result or plan questions arrive.\n"
        "- Synchronous Direct Lookup Tools (`spotify_playback`, `calendar_events`, `gmail_messages`, `drive_files`, `slack_messages`, `github_operations`, `list_repositories`, `list_harnesses`, `get_task_result`, `get_current_time_and_timezone`): "
        "These tools return their full result immediately. NEVER speak a pre-tool 'Let me check...' filler before or during these calls; invoke the tool silently first and speak ONLY the grounded answer or credential message returned by the tool.\n\n"
        "ITERATIVE PLANNING, SANDBOX SKILLS & ORCHESTRATION GUIDELINES:\n"
        "- Full Text-Harness Parity in the Sandbox: Your sandbox harnesses (`claude`, `antigravity`, `horizon`) have pre-installed Agent Skills "
        "(`agents-cli` ADK skills, `find-skills` via `npx skills`, `superpowers` brainstorming/planning/debugging/TDD, `mattpocock/skills` research/grill-me/code-review, "
        "`anthropics/skills` doc/pdf/docx/xlsx/pptx, and `agent-browser`) plus active GCP authentication (`$GOOGLE_CLOUD_PROJECT`) to run `agents-cli eval` and `agents-cli deploy`. "
        "Use `dispatch_task` for both software engineering and complex, personalized research or document synthesis workflows.\n"
        "- Peer Sounding Board & Plan-First Dispatch: Treat planning as an interactive collaboration. When a user is exploring an idea or weighing approaches, "
        "help frame the trade-offs like a peer programmer and dispatch `dispatch_task(..., mode='plan')` so the sandbox inspects the repo or research sources in read-only mode.\n"
        "- Walkthrough & Iterative Plan Refinement (`mode='plan'` vs `mode='execute'`): When a task pauses in `awaiting_input` after planning, "
        "never auto-steer in the same turn. Walk the user concisely through the returned plan summary, key steps, and trade-off questions so they understand what the harness proposes. "
        "If the user wants to explore an alternate approach, critique an assumption, or adjust the scope before approving, call `steer_task(task_id=..., instruction=..., mode='plan')` "
        "to revise the plan in the same sandbox session. Only call `steer_task(task_id=..., instruction=..., mode='execute')` once the user confirms they are ready to execute (including any cloud deployments via `agents-cli deploy`).\n"
        "- Post-Execution Warm Continuation (`steer_task` after `completed`): When a coding or multi-step sandbox task finishes an execution pass, its git worktree (`agent/task-N`) and harness session remain warm. "
        "Report what work was completed and naturally offer to either keep going on that same task (such as adding tests, reviewing the diff with another harness, or opening a pull request via `steer_task(task_id=..., instruction=..., mode='execute')`) or wrap it up if the atomic goal is done. "
        "Whenever the user asks to do follow-up work on a recently completed task, always resume that task with `steer_task` instead of creating a separate `dispatch_task`.\n"
        "- Last-Mile Delivery Across Integrations: When a sandbox task completes a research brief, shopping list, or engineering summary that the user wants emailed or posted to chat, "
        "retrieve the output with `get_task_result` and deliver it via `gmail_messages` or `slack_messages`.\n"
        "- Human-in-the-Loop Approval & Diff Compression: When a task pauses in `awaiting_approval` with a diff, "
        "never read raw diff markers (`@@`, `+`, `-`) or code syntax aloud, and do not auto-approve in the same turn. "
        "Summarize the functional changes concisely and wait for explicit confirmation before calling `approve_task(task_id)`.\n"
        "- Error & Traceback Compression: When a task or tool returns a stack trace or error log, never read raw file paths, "
        "line numbers, or stack frames aloud. State the root cause in one plain sentence and offer the next remediation step.\n\n"
        "SPOKEN VOICE GUIDELINES:\n"
        "- Every response is rendered directly as spoken audio. Never output markdown, bullet points, headers, asterisks, "
        "backticks, code blocks, emoji, tables, or raw URLs.\n"
        f"- {style_rule}\n"
        "- Speak identifiers naturally as words, such as 'task one' or 'task one oh two'."
    )


def _apply_live_voice_config(
    callback_context: Any,
    llm_request: Any,
) -> None:
    """Injects the user's selected PrebuiltVoiceConfig into the Gemini Live connection setup."""
    prefs = get_runtime_preferences()
    voice_name = prefs.get("voice_name") or "Aoede"
    speech_cfg = types.SpeechConfig(
        voice_config=types.VoiceConfig(
            prebuilt_voice_config=types.PrebuiltVoiceConfig(
                voice_name=voice_name,
            )
        )
    )
    if getattr(llm_request, "live_connect_config", None) is not None:
        llm_request.live_connect_config.speech_config = speech_cfg
    if getattr(llm_request, "config", None) is not None:
        llm_request.config.speech_config = speech_cfg


logger = logging.getLogger(__name__)


async def non_blocking_memory_capture(callback_context: Any) -> None:
    """Fire-and-forget memory capture that runs in the background to ensure zero impact on end-user voice latency."""
    if not hasattr(callback_context, "add_session_to_memory"):
        return

    async def _capture_in_background() -> None:
        try:
            await callback_context.add_session_to_memory()
        except Exception as exc:
            logger.debug("Memory capture skipped or failed: %s", exc)

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_capture_in_background(), name="memory-capture-bg")
    except RuntimeError:
        pass


root_agent = Agent(
    name="voice_orchestrator",
    model=Gemini(
        model=MODEL,
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    instruction=build_orchestrator_instruction,
    before_agent_callback=_hydrate_task_store_callback,
    before_model_callback=_apply_live_voice_config,
    after_agent_callback=non_blocking_memory_capture,
    tools=[
        PreloadMemoryTool(),
        non_blocking_tool(dispatch_task),
        get_task_result,
        non_blocking_tool(steer_task),
        non_blocking_tool(approve_task),
        list_harnesses,
        set_coding_harness,
        list_tasks,
        cancel_task,
        list_repositories,
        inspect_repository_files,
        read_workspace_file,
        create_or_clone_repository,
        non_blocking_tool(search_web_grounded),
        non_blocking_tool(search_maps_grounded),
        spotify_playback,
        calendar_events,
        gmail_messages,
        drive_files,
        slack_messages,
        github_operations,
        get_current_time_and_timezone,
        list_integrations,
        configure_integration,
        *_mcp_toolsets,
    ],
)

app = App(
    root_agent=root_agent,
    name="app",
)

__all__ = [
    "MODEL",
    "app",
    "approve_task",
    "calendar_events",
    "cancel_task",
    "configure_integration",
    "create_or_clone_repository",
    "dispatch_task",
    "drive_files",
    "get_task_result",
    "github_operations",
    "gmail_messages",
    "inspect_repository_files",
    "list_harnesses",
    "list_integrations",
    "list_repositories",
    "list_tasks",
    "read_workspace_file",
    "root_agent",
    "search_maps_grounded",
    "search_web_grounded",
    "set_coding_harness",
    "slack_messages",
    "spotify_playback",
    "steer_task",
]
