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
from app.tools.task_tools import (
    approve_task,
    cancel_task,
    dispatch_task,
    get_task_result,
    list_harnesses,
    list_tasks,
    set_coding_harness,
    steer_task,
    stop_streaming,
    watch_tasks,
)
from app.tools.workspace_tools import (
    create_or_clone_repository,
    inspect_repository_files,
    list_repositories,
    read_workspace_file,
)

# Live model via Google AI Studio
MODEL = os.getenv("LIVE_MODEL", "gemini-3.8-live")


def non_blocking_tool(
    func: Any,
    scheduling: types.FunctionResponseScheduling = types.FunctionResponseScheduling.WHEN_IDLE,
) -> FunctionTool:
    """Wrap an async function as a Gemini Live API NON_BLOCKING streaming FunctionTool.

    In ADK (`google/adk/flows/llm_flows/functions.py` lines 1181-1292), wrapping `func`
    as an async generator (`inspect.isasyncgenfunction`) instructs ADK to:
    1. Advertise `declaration.behavior = types.Behavior.NON_BLOCKING` to the Gemini Live API.
    2. Immediately return `{'status': 'The function is running asynchronously and the results are pending.'}`
       in <1ms so the voice model speaks an immediate acknowledgment without dead air.
    3. Execute `func` in a background `asyncio.Task` (`run_tool_and_update_queue`) and push the
       completed `FunctionResponse` into `live_request_queue` with `FunctionResponseScheduling.WHEN_IDLE`.
    """
    if not inspect.isasyncgenfunction(func):

        @functools.wraps(func)
        async def _async_gen_wrapper(*args: Any, **kwargs: Any):
            result = await func(*args, **kwargs)
            yield result

        _async_gen_wrapper.__signature__ = inspect.signature(func)  # type: ignore[attr-defined]
        tool = FunctionTool(_async_gen_wrapper)
    else:
        tool = FunctionTool(func)
    tool.response_scheduling = scheduling
    return tool


# Only mount raw McpToolsets onto the real-time voice agent if explicitly enabled;
# otherwise root_agent uses the direct async integration_tools (which never block or timeout the WebSocket).
_mcp_toolsets = (
    get_integration_registry().build_adk_mcp_toolsets(only_with_credentials=True)
    if os.getenv("ENABLE_LIVE_MCP_TOOLSETS", "").lower() in {"1", "true", "yes"}
    else []
)

async def _hydrate_task_store_callback(callback_context: Any) -> None:
    """Hydrates persisted tasks from TaskStore before the turn so _build_task_briefing sees all records."""
    try:
        from app.tasks import get_task_registry

        await get_task_registry().list_all()
    except Exception:
        pass


def _build_task_briefing() -> str:
    """Dynamically builds a concise spoken briefing of active or recently completed tasks."""
    try:
        from app.tasks import get_task_registry

        reg = get_task_registry()
        tasks = reg.snapshot_tasks()
        if not tasks:
            return ""

        active_items: list[str] = []
        for t in tasks:
            if t.status == "awaiting_approval":
                active_items.append(
                    f"- {t.task_id} on {t.repo}: changes ready for review and diff approval on branch {t.branch}"
                )
            elif t.status == "awaiting_input":
                qs = ", ".join(t.questions[:2]) if t.questions else "clarification required"
                active_items.append(
                    f"- {t.task_id} on {t.repo}: waiting for user guidance ({qs})"
                )
            elif t.status == "running":
                active_items.append(
                    f"- {t.task_id} on {t.repo}: currently running ({t.harness})"
                )

        if not active_items:
            recent = [t for t in tasks if t.status == "completed"]
            if recent:
                last = recent[-1]
                active_items.append(
                    f"- {last.task_id} on {last.repo}: recently completed ({last.summary or 'all changes verified'})"
                )

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
        "You are a concise, pragmatic voice-driven engineering and workspace orchestrator.\n"
        "You coordinate concurrent coding agents inside an isolated Vertex Agent Platform Sandbox "
        "(using per-task git worktrees across configurable harnesses such as Claude Code, ADK Long Horizon, "
        "and Antigravity) while also operating across a dynamic set of workspace, grounding, and MCP integration tools.\n\n"
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
        "These tools immediately return `The function is running asynchronously and the results are pending`. Whenever you call one of these five tools, "
        "ALWAYS speak a brief one-sentence acknowledgment aloud immediately (for example, 'Checking the web for the latest Python release notes.' "
        "or 'Checking Google Maps for quiet coffee shops in downtown Austin.' or 'Spinning up a Claude Code plan task on auth service now.'), "
        "and then summarize the outcome once the background result arrives.\n"
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
        watch_tasks,
        stop_streaming,
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
    "stop_streaming",
    "watch_tasks",
]
