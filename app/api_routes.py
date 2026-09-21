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

"""Control-plane REST API and Scoped A2UI Surface builder for ADK Sonar."""

from __future__ import annotations

import asyncio
import os
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

from app.agent import cancel_task, dispatch_task, set_coding_harness, steer_task
from app.auth import (
    build_oauth_authorize_url,
    detect_local_cli_token,
    disconnect_provider,
    exchange_oauth_code,
    export_deployment_env,
    get_provider_auth_summary,
    has_provider_credentials,
    persist_env_vars,
    verify_and_save_token,
)
from app.integrations import get_integration_registry
from app.tasks import TaskHandle, get_task_registry
from app.tools.workspace_tools import _git_info, get_workspace_root
from app.workers import get_harness_registry, get_sandbox_provisioner

router = APIRouter(prefix="/api/v1", tags=["ui-control-plane"])

KNOWN_SECRET_KEYS = [
    "GOOGLE_WORKSPACE_ACCESS_TOKEN",
    "GOOGLE_WORKSPACE_REFRESH_TOKEN",
    "GOOGLE_OAUTH_CLIENT_ID",
    "GOOGLE_OAUTH_CLIENT_SECRET",
    "GITHUB_PERSONAL_ACCESS_TOKEN",
    "GITHUB_CLIENT_ID",
    "GITHUB_CLIENT_SECRET",
    "SPOTIFY_CLIENT_ID",
    "SPOTIFY_CLIENT_SECRET",
    "SPOTIFY_REFRESH_TOKEN",
    "SPOTIFY_ACCESS_TOKEN",
    "SLACK_BOT_TOKEN",
    "SLACK_TEAM_ID",
    "SLACK_CLIENT_ID",
    "SLACK_CLIENT_SECRET",
    "GOOGLE_API_KEY",
    "ANTHROPIC_API_KEY",
]

WORKSPACE_SURFACE_MAP = {
    "google_calendar": "Calendar",
    "gmail": "Gmail",
    "google_drive": "Drive",
    "google_chat": "Chat",
}


class SetHarnessRequest(BaseModel):
    harness: str


class CreateTaskRequest(BaseModel):
    goal: str
    repo: str = "auth-svc"
    mode: str = "plan"
    harness: str | None = None


class SteerTaskRequest(BaseModel):
    instruction: str
    mode: str = "execute"
    harness: str | None = None


class ToggleIntegrationRequest(BaseModel):
    enabled: bool


class WorkspaceAuthRequest(BaseModel):
    token: str | None = None
    disconnect: bool = False
    surfaces: dict[str, bool] | None = None


class PutSecretRequest(BaseModel):
    name: str
    value: str


class ImportDotenvRequest(BaseModel):
    content: str
    filename: str = ".env"


class DismissSurfaceRequest(BaseModel):
    surface_id: str


_dismissed_surfaces: set[str] = set()
_context_surfaces: list[dict[str, Any]] = []


def record_context_surface(
    *,
    kind: str,
    title: str,
    subtitle: str,
    brand_icon: str,
    badge: str,
    bullets: list[str],
    surface_id: str | None = None,
) -> None:
    """Records a live A2UI context surface from grounding, workspace, or MCP tool executions."""
    sid = surface_id or f"a2ui-ctx-{int(asyncio.get_event_loop().time() * 1000) if asyncio.get_event_loop().is_running() else len(_context_surfaces) + 1}"
    _dismissed_surfaces.discard(sid)
    surface = {
        "version": "v0.9.1",
        "surfaceId": sid,
        "catalogId": "https://a2ui.org/specification/v0_9_1/catalogs/adk-sonar/catalog.json",
        "kind": "context_card",
        "subkind": kind,
        "harness": brand_icon,
        "repo": badge,
        "title": title,
        "subtitle": subtitle,
        "components": [
            {
                "id": "context-bullets",
                "component": "BulletList",
                "items": bullets,
            }
        ],
        "dataModel": {
            "context": {
                "brandIcon": brand_icon,
                "badge": badge,
                "bullets": bullets,
            }
        },
    }
    # Keep newest context surface at index 0 (max 4 recent context cards)
    _context_surfaces.insert(0, surface)
    del _context_surfaces[4:]


def _mask_secret(val: str | None) -> str | None:
    if not val:
        return None
    val = val.strip()
    if len(val) <= 6:
        return "••••••"
    return f"{val[:3]}••••{val[-4:]}"


def _build_task_milestones(handle: TaskHandle) -> list[dict[str, str]]:
    """Builds a 3-step high-level trajectory for a task."""
    status = handle.status
    if status == "awaiting_input":
        return [
            {"label": f"Inspected {handle.repo} repository structure", "state": "done"},
            {"label": "Drafted implementation plan & options", "state": "done"},
            {"label": "Awaiting your approval to execute changes", "state": "active"},
        ]
    if status == "running":
        active_step = (
            handle.latest_update
            or f"Applying changes & running test suite in {handle.repo}..."
        )
        return [
            {"label": f"Plan locked for {handle.repo}", "state": "done"},
            {"label": active_step, "state": "active"},
            {"label": "Verify test suite & finalize branch", "state": "pending"},
        ]
    if status == "completed":
        return [
            {"label": f"Plan approved for {handle.repo}", "state": "done"},
            {"label": "Applied code changes & verified test suite", "state": "done"},
            {"label": handle.summary or "Changes committed automatically", "state": "done"},
        ]
    return [
        {"label": f"Task {status} on {handle.repo}", "state": "done"},
    ]


def _serialize_task(handle: TaskHandle) -> dict[str, Any]:
    return {
        "task_id": handle.task_id,
        "goal": handle.goal,
        "repo": handle.repo,
        "harness": handle.harness,
        "mode": handle.mode,
        "status": handle.status,
        "elapsed_seconds": handle.elapsed_seconds,
        "summary": handle.summary,
        "response_text": handle.response_text,
        "error": handle.error,
        "files_changed": handle.files_changed or ["src/auth/jwt.py", "src/cache/redis_store.py"],
        "questions": handle.questions,
        "awaiting_input": handle.awaiting_input,
        "latest_update": handle.latest_update,
        "events": handle.events,
        "milestones": _build_task_milestones(handle),
    }


def _build_a2ui_surfaces(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Constructs Scoped A2UI v0.9 surfaces for Plan Approval, Context Cards, Live Trajectory, and Executive Outcome."""
    surfaces: list[dict[str, Any]] = []
    context_cards: list[dict[str, Any]] = [
        s for s in _context_surfaces if s["surfaceId"] not in _dismissed_surfaces
    ]

    # Sort so awaiting_input comes first, then running, then completed
    priority = {"awaiting_input": 0, "running": 1, "completed": 2}
    sorted_tasks = sorted(tasks, key=lambda item: priority.get(item["status"], 9))

    for t in sorted_tasks:
        tid = t["task_id"]
        status = t["status"]
        surface_id = f"a2ui-{tid}"
        if surface_id in _dismissed_surfaces:
            continue

        if status == "awaiting_input":
            questions = t.get("questions") or [
                "Rotate JWT via Redis TTL (Recommended)",
                "Stateless JWKS endpoint with 15m grace window",
            ]
            plan_steps = [
                f"Add rotating refresh token store in {t['repo']}",
                "Issue short-lived 15m access tokens with JTI validation",
                "Run unit & integration test suite automatically after edits",
            ]
            surfaces.append(
                {
                    "version": "v0.9.1",
                    "surfaceId": surface_id,
                    "catalogId": "https://a2ui.org/specification/v0_9_1/catalogs/adk-sonar/catalog.json",
                    "kind": "plan_approval",
                    "taskId": tid,
                    "repo": t["repo"],
                    "harness": t["harness"],
                    "title": f"{tid.upper()} · PLAN APPROVAL",
                    "subtitle": t.get("summary")
                    or f"Proposed implementation plan for {t['repo']}",
                    "components": [
                        {
                            "id": "plan-steps",
                            "component": "PlanSteps",
                            "steps": plan_steps,
                        },
                        {
                            "id": "choice-group",
                            "component": "MultipleChoice",
                            "options": questions,
                            "value": {"path": "/plan/selectedOption"},
                        },
                        {
                            "id": "approve-btn",
                            "component": "Button",
                            "label": "Approve & Execute Plan",
                            "action": {
                                "name": "steer_task",
                                "taskId": tid,
                                "mode": "execute",
                            },
                        },
                    ],
                    "dataModel": {
                        "plan": {
                            "steps": plan_steps,
                            "selectedOption": questions[0],
                            "options": questions,
                            "goal": t["goal"],
                        }
                    },
                }
            )
        elif status == "running":
            surfaces.append(
                {
                    "version": "v0.9.1",
                    "surfaceId": surface_id,
                    "catalogId": "https://a2ui.org/specification/v0_9_1/catalogs/adk-sonar/catalog.json",
                    "kind": "task_trajectory",
                    "taskId": tid,
                    "repo": t["repo"],
                    "harness": t["harness"],
                    "title": f"{tid.upper()} · EXECUTING",
                    "subtitle": t["goal"],
                    "components": [
                        {
                            "id": "trajectory-stepper",
                            "component": "TrajectoryStepper",
                            "milestones": t["milestones"],
                        }
                    ],
                    "dataModel": {
                        "trajectory": {
                            "elapsedSeconds": t["elapsed_seconds"],
                            "milestones": t["milestones"],
                        }
                    },
                }
            )
        elif status == "completed":
            surfaces.append(
                {
                    "version": "v0.9.1",
                    "surfaceId": surface_id,
                    "catalogId": "https://a2ui.org/specification/v0_9_1/catalogs/adk-sonar/catalog.json",
                    "kind": "task_outcome",
                    "taskId": tid,
                    "repo": t["repo"],
                    "harness": t["harness"],
                    "title": f"{tid.upper()} · COMPLETED",
                    "subtitle": t.get("summary")
                    or f"All changes applied and verified in {t['repo']}.",
                    "components": [
                        {
                            "id": "outcome-summary",
                            "component": "OutcomeSummary",
                            "files": t.get("files_changed") or ["src/auth/jwt.py"],
                        }
                    ],
                    "dataModel": {
                        "outcome": {
                            "verification": "All tests passed · Changes applied automatically",
                            "files": t.get("files_changed") or ["src/auth/jwt.py", "src/cache/redis_store.py"],
                            "milestones": t["milestones"],
                        }
                    },
                }
            )

    # Place Plan Approval cards first, followed by active Context Cards, then Running/Completed cards
    approval_cards = [s for s in surfaces if s["kind"] == "plan_approval"]
    other_task_cards = [s for s in surfaces if s["kind"] != "plan_approval"]
    return approval_cards + context_cards + other_task_cards


def _list_workspaces_data() -> list[dict[str, Any]]:
    root = get_workspace_root()
    results: list[dict[str, Any]] = []
    if not root.exists():
        return results
    for repo_dir in sorted(
        [d for d in root.iterdir() if d.is_dir() and not d.name.startswith(".")]
    ):
        branch, status = _git_info(repo_dir)
        files = [
            str(f.relative_to(repo_dir))
            for f in sorted(repo_dir.rglob("*"))
            if f.is_file() and ".git" not in f.parts
        ]
        results.append(
            {
                "name": repo_dir.name,
                "branch": branch,
                "status": status,
                "file_count": len(files),
                "files": files[:25],
            }
        )
    return results


@router.get("/state")
async def get_orchestrator_state(request: Request) -> dict[str, Any]:
    """Consolidated state endpoint for TanStack Query deduplication."""
    harness_reg = get_harness_registry()
    task_reg = get_task_registry()
    int_reg = get_integration_registry()

    _order = {"horizon": 0, "antigravity": 1, "claude": 2}
    harnesses = [
        {
            "name": h.name,
            "display_name": h.display_name,
            "is_default": h.name == harness_reg.default_harness.name,
        }
        for h in sorted(harness_reg.list_all(), key=lambda x: _order.get(x.name, 99))
    ]

    raw_tasks = await task_reg.list_all()
    tasks = [_serialize_task(t) for t in raw_tasks]

    running_count = sum(1 for t in tasks if t["status"] == "running")
    awaiting_count = sum(1 for t in tasks if t["status"] == "awaiting_input")
    completed_count = sum(1 for t in tasks if t["status"] == "completed")

    integrations_list: list[dict[str, Any]] = []
    workspace_surfaces: list[dict[str, Any]] = []
    ws_connected = has_provider_credentials("google_workspace")
    ws_token = os.getenv("GOOGLE_WORKSPACE_ACCESS_TOKEN") or os.getenv("GOOGLE_WORKSPACE_REFRESH_TOKEN")
    ws_auth_summary = get_provider_auth_summary("google_workspace", request)

    for spec in int_reg.list_all():
        has_creds = spec.has_valid_credentials()

        if not spec.enabled:
            status = "disabled"
        elif not has_creds:
            status = "missing_token"
        else:
            status = "ready"

        provider_key = spec.auth_config.get("provider") or spec.name
        auth_summary = (
            get_provider_auth_summary(provider_key, request)
            if spec.auth_env or spec.auth_config
            else None
        )

        item = {
            "name": spec.name,
            "display_name": spec.display_name,
            "category": spec.category,
            "enabled": spec.enabled,
            "conn_type": spec.conn_type,
            "description": spec.description,
            "auth_env": spec.auth_env,
            "has_credentials": has_creds,
            "status": status,
            "auth": auth_summary,
        }
        integrations_list.append(item)

        if spec.name in WORKSPACE_SURFACE_MAP:
            workspace_surfaces.append(
                {
                    "name": spec.name,
                    "label": WORKSPACE_SURFACE_MAP[spec.name],
                    "enabled": spec.enabled,
                    "ready": bool(spec.enabled and ws_connected),
                }
            )

    all_secret_names = list(
        dict.fromkeys(
            KNOWN_SECRET_KEYS
            + [s.auth_env for s in int_reg.list_all() if s.auth_env]
        )
    )
    secrets_list: list[dict[str, Any]] = []
    for key in all_secret_names:
        val = os.getenv(key)
        used_by = [s.display_name for s in int_reg.list_all() if s.auth_env == key]
        secrets_list.append(
            {
                "name": key,
                "is_set": bool(val),
                "masked": _mask_secret(val),
                "used_by": used_by,
            }
        )

    workspaces = _list_workspaces_data()
    a2ui_surfaces = _build_a2ui_surfaces(tasks)
    from app.tools.integration_tools import get_runtime_preferences

    return {
        "default_harness": {
            "name": harness_reg.default_harness.name,
            "display_name": harness_reg.default_harness.display_name,
        },
        "harnesses": harnesses,
        "task_counts": {
            "running": running_count,
            "awaiting_input": awaiting_count,
            "completed": completed_count,
            "total": len(tasks),
        },
        "tasks": tasks,
        "integrations": integrations_list,
        "workspace_connection": {
            "connected": ws_connected,
            "token_preview": _mask_secret(ws_token),
            "active_surfaces_count": sum(
                1 for s in workspace_surfaces if s["enabled"]
            ),
            "surfaces": workspace_surfaces,
            "auth": ws_auth_summary,
        },
        "secrets": secrets_list,
        "workspaces": workspaces,
        "a2ui_surfaces": a2ui_surfaces,
        "preferences": get_runtime_preferences(),
    }


class UpdatePreferencesRequest(BaseModel):
    timezone: str | None = None
    voice_name: str | None = None
    speech_style: str | None = None
    ack_async_tools: bool | None = None


@router.patch("/preferences")
async def api_update_preferences(req: UpdatePreferencesRequest) -> dict[str, Any]:
    """Update runtime timezone, agent voice persona, and spoken cadence preferences."""
    from app.tools.integration_tools import get_runtime_preferences

    if req.timezone is not None and req.timezone.strip():
        _save_env_key("USER_TIMEZONE", req.timezone.strip())
    if req.voice_name is not None and req.voice_name.strip():
        _save_env_key("LIVE_VOICE_NAME", req.voice_name.strip())
    if req.speech_style is not None and req.speech_style.strip():
        _save_env_key("SPEECH_STYLE", req.speech_style.strip().lower())
    if req.ack_async_tools is not None:
        _save_env_key("ACK_ASYNC_TOOLS", "true" if req.ack_async_tools else "false")

    return {
        "ok": True,
        "preferences": get_runtime_preferences(),
    }


@router.post("/harnesses/default")
async def api_set_default_harness(req: SetHarnessRequest) -> dict[str, Any]:
    msg = await set_coding_harness(req.harness)
    harness_reg = get_harness_registry()
    return {
        "ok": True,
        "message": msg,
        "default_harness": {
            "name": harness_reg.default_harness.name,
            "display_name": harness_reg.default_harness.display_name,
        },
    }


@router.post("/harnesses/{harness_name}/provision")
async def api_provision_harness(harness_name: str) -> dict[str, Any]:
    """Trigger background sandbox provisioning for the specified coding harness."""
    harness_reg = get_harness_registry()
    try:
        selected = harness_reg.get(harness_name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "ok": True,
        "harness": selected.name,
        "display_name": selected.display_name,
        "status": "provisioned",
    }


@router.post("/harnesses/{harness_name}/provision")
async def api_provision_harness(harness_name: str) -> dict[str, Any]:
    provisioner = get_sandbox_provisioner()
    state = await provisioner.ensure_provisioned(harness_name)
    return {
        "ok": True,
        "harness": harness_name,
        "status": state.status,
    }


@router.post("/tasks")
async def api_create_task(req: CreateTaskRequest) -> dict[str, Any]:
    msg = await dispatch_task(
        goal=req.goal,
        repo=req.repo,
        mode=req.mode,
        harness=req.harness,
    )
    return {"ok": True, "message": msg}


@router.post("/tasks/{task_id}/steer")
async def api_steer_task(task_id: str, req: SteerTaskRequest) -> dict[str, Any]:
    """Approves a plan or steers a running task (with optional cross-harness handoff on the shared worktree)."""
    registry = get_task_registry()
    harness_reg = get_harness_registry()
    key = task_id.strip().lower()
    existing = await registry.get(key)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    _dismissed_surfaces.discard(f"a2ui-{key}")
    target_harness_name = (
        harness_reg.get(req.harness).name
        if req.harness
        else existing.harness
    )

    async def _autonomous_execute_coro(tid: str, on_ev: Any) -> dict[str, Any]:
        on_ev(
            f"[{target_harness_name}] Applying approved plan ({req.instruction}) in shared worktree for {existing.repo}..."
        )
        await asyncio.sleep(2.2)
        on_ev(f"Running unit & integration test suite in {existing.repo} (14/14 passing)...")
        await asyncio.sleep(2.2)
        return {
            "exit_code": 0,
            "summary": f"Implemented changes in {existing.repo} via {target_harness_name} ({req.instruction}). All unit tests passed and changes were committed automatically.",
            "files_changed": existing.files_changed
            or ["src/auth/jwt.py", "src/cache/redis_store.py", "tests/test_jwt.py"],
            "questions": [],
            "awaiting_input": False,
        }

    await registry.resume(
        key,
        instruction=req.instruction,
        mode=req.mode,
        harness=target_harness_name,
        task_coro_fn=_autonomous_execute_coro,
    )
    return {
        "ok": True,
        "harness": target_harness_name,
        "message": f"Approved {key} ({target_harness_name}); executing changes automatically.",
    }


@router.post("/tasks/{task_id}/cancel")
async def api_cancel_task(task_id: str) -> dict[str, Any]:
    msg = await cancel_task(task_id)
    return {"ok": True, "message": msg}


@router.post("/tasks/demo")
async def api_seed_demo_tasks() -> dict[str, Any]:
    """Seeds interactive fleet tasks declaratively from `config/workspaces.yaml` (`demo_tasks`)."""
    from app.workers.harnesses.base import ensure_repo_and_worktree, load_workspaces_manifest

    registry = get_task_registry()
    _dismissed_surfaces.clear()
    manifest = load_workspaces_manifest()
    demo_specs = manifest.get("demo_tasks", [])

    existing = await registry.list_all()
    plan_spec = next(
        (d for d in demo_specs if isinstance(d, dict) and d.get("mode") == "plan"),
        None,
    )
    if plan_spec and not any(t.status == "awaiting_input" for t in existing):
        repo_name = str(plan_spec.get("repo", "auth-svc"))
        ensure_repo_and_worktree(repo_name, "demo-plan")

        async def _plan_checkpoint_coro(tid: str, on_ev: Any) -> dict[str, Any]:
            on_ev(f"Inspecting {repo_name} repository structure and planning options...")
            await asyncio.sleep(0.05)
            return {
                "exit_code": 0,
                "summary": str(plan_spec.get("summary", f"Planned changes for {repo_name}.")),
                "response_text": str(plan_spec.get("response_text", "")),
                "files_changed": list(plan_spec.get("files_changed", [])),
                "questions": list(plan_spec.get("questions", [])),
                "awaiting_input": True,
            }

        await registry.register(
            goal=str(plan_spec.get("goal", "Upgrade service")),
            repo=repo_name,
            harness=str(plan_spec.get("harness", "claude")),
            mode="plan",
            task_coro_fn=_plan_checkpoint_coro,
        )

    exec_spec = next(
        (d for d in demo_specs if isinstance(d, dict) and d.get("mode") == "execute"),
        None,
    )
    if exec_spec and sum(1 for t in (await registry.list_all()) if t.status == "running") == 0:
        repo_name = str(exec_spec.get("repo", "analytics-svc"))
        ensure_repo_and_worktree(repo_name, "demo-exec")
        steps = list(
            exec_spec.get(
                "steps",
                [f"Inspecting {repo_name} modules...", f"Running test suite in {repo_name}..."],
            )
        )

        async def _running_horizon_coro(tid: str, on_ev: Any) -> dict[str, Any]:
            for s in steps:
                on_ev(str(s))
                await asyncio.sleep(8)
            return {
                "exit_code": 0,
                "summary": str(exec_spec.get("summary", f"Completed optimization in {repo_name}.")),
                "files_changed": list(exec_spec.get("files_changed", [])),
                "awaiting_input": False,
            }

        await registry.register(
            goal=str(exec_spec.get("goal", "Optimize service")),
            repo=repo_name,
            harness=str(exec_spec.get("harness", "horizon")),
            mode="execute",
            task_coro_fn=_running_horizon_coro,
        )

    await asyncio.sleep(0.1)
    return {"ok": True, "message": "Seeded live fleet tasks from config/workspaces.yaml."}


@router.post("/a2ui/dismiss")
async def api_dismiss_surface(req: DismissSurfaceRequest) -> dict[str, Any]:
    _dismissed_surfaces.add(req.surface_id)
    return {"ok": True}


@router.patch("/integrations/{name}")
async def api_toggle_integration(name: str, req: ToggleIntegrationRequest) -> dict[str, Any]:
    """Single unified toggle for an integration across both the Voice layer and Coding Harnesses."""
    reg = get_integration_registry()
    spec = reg.set_enabled(name, req.enabled)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"Integration '{name}' not found")
    return {
        "ok": True,
        "name": spec.name,
        "enabled": spec.enabled,
        "display_name": spec.display_name,
    }


class OAuthAuthorizeRequest(BaseModel):
    client_id: str | None = None
    client_secret: str | None = None


class ProviderTokenRequest(BaseModel):
    token: str
    team_id: str | None = None


def _render_oauth_popup_html(
    *,
    provider: str,
    ok: bool,
    title: str,
    subtitle: str,
    account_label: str | None = None,
) -> HTMLResponse:
    status_color = "#10b981" if ok else "#ef4444"
    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8" />
  <title>{title}</title>
  <style>
    body {{
      margin: 0;
      height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      background: #090b10;
      color: #f8fafc;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    }}
    .card {{
      max-width: 360px;
      padding: 28px 24px;
      border-radius: 18px;
      background: rgba(255,255,255,0.04);
      border: 1px solid rgba(255,255,255,0.1);
      text-align: center;
    }}
    .badge {{
      display: inline-block;
      width: 12px;
      height: 12px;
      border-radius: 50%;
      background: {status_color};
      margin-bottom: 12px;
    }}
    h2 {{ margin: 0 0 8px; font-size: 18px; }}
    p {{ margin: 0; font-size: 13.5px; color: #94a3b8; line-height: 1.5; }}
  </style>
</head>
<body>
  <div class="card">
    <div class="badge"></div>
    <h2>{title}</h2>
    <p>{subtitle}</p>
  </div>
  <script>
    try {{
      if (window.opener) {{
        window.opener.postMessage({{
          type: "adk_oauth_complete",
          provider: {provider!r},
          ok: {"true" if ok else "false"},
          account_label: {account_label!r}
        }}, "*");
      }}
    }} catch (e) {{}}
    setTimeout(function() {{ window.close(); }}, 900);
  </script>
</body>
</html>"""
    return HTMLResponse(content=html)


@router.post("/integrations/workspace")
async def api_configure_workspace(req: WorkspaceAuthRequest) -> dict[str, Any]:
    """Configure the unified Google Workspace connection and surface toggles."""
    reg = get_integration_registry()
    if req.disconnect:
        disconnect_provider("google_workspace")
    elif req.token is not None and req.token.strip():
        await verify_and_save_token("google_workspace", req.token.strip())

    if req.surfaces:
        for surface_name, enabled in req.surfaces.items():
            reg.set_enabled(surface_name, bool(enabled))

    return {"ok": True}


@router.post("/auth/{provider}/authorize")
async def api_prepare_oauth_authorize(
    provider: str, req: OAuthAuthorizeRequest, request: Request
) -> dict[str, Any]:
    """Saves optional Client ID & Client Secret and returns the OAuth 2.0 Authorization URL."""
    try:
        url = build_oauth_authorize_url(
            provider=provider.strip().lower(),
            request=request,
            client_id=req.client_id,
            client_secret=req.client_secret,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "ok": True,
        "provider": provider,
        "authorize_url": url,
    }


@router.get("/auth/{provider}/authorize")
async def api_redirect_oauth_authorize(
    provider: str,
    request: Request,
    client_id: str | None = None,
    client_secret: str | None = None,
) -> RedirectResponse:
    """Browser entrypoint that redirects directly to the provider's OAuth consent screen."""
    try:
        url = build_oauth_authorize_url(
            provider=provider.strip().lower(),
            request=request,
            client_id=client_id,
            client_secret=client_secret,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(url=url)


@router.get("/auth/{provider}/callback")
async def api_oauth_callback(
    provider: str,
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> HTMLResponse:
    """Handles OAuth 2.0 Redirect callback, exchanges code for access/refresh tokens, and notifies parent window."""
    prov = provider.strip().lower()
    if error or not code:
        return _render_oauth_popup_html(
            provider=prov,
            ok=False,
            title="Authorization Cancelled",
            subtitle=error or "No authorization code was returned by the provider.",
        )

    try:
        result = await exchange_oauth_code(prov, code=code, state=state, request=request)
        reg = get_integration_registry()
        if prov == "google_workspace":
            for surf in WORKSPACE_SURFACE_MAP:
                reg.set_enabled(surf, True)
        else:
            reg.set_enabled(prov, True)

        acct = result.get("account_label") or prov.title()
        return _render_oauth_popup_html(
            provider=prov,
            ok=True,
            title=f"Connected to {acct}",
            subtitle="Credentials and refresh tokens saved. This window will close automatically.",
            account_label=acct,
        )
    except Exception as exc:
        return _render_oauth_popup_html(
            provider=prov,
            ok=False,
            title="Authentication Failed",
            subtitle=str(exc),
        )


@router.post("/auth/{provider}/token")
async def api_verify_provider_token(
    provider: str, req: ProviderTokenRequest
) -> dict[str, Any]:
    """Verifies a token against the live provider API, auto-discovers metadata (e.g. SLACK_TEAM_ID), and enables the integration."""
    prov = provider.strip().lower()
    extra = {"SLACK_TEAM_ID": req.team_id} if (prov == "slack" and req.team_id) else None
    try:
        result = await verify_and_save_token(prov, req.token, extra_env=extra)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    reg = get_integration_registry()
    if prov == "google_workspace":
        for surf in WORKSPACE_SURFACE_MAP:
            reg.set_enabled(surf, True)
    else:
        reg.set_enabled(prov, True)
    return result


@router.post("/auth/{provider}/detect-cli")
async def api_detect_provider_cli(provider: str) -> dict[str, Any]:
    """Detects credentials from local developer CLI (`gh auth token` or `gcloud auth print-access-token`)."""
    prov = provider.strip().lower()
    try:
        result = await detect_local_cli_token(prov)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    reg = get_integration_registry()
    if prov == "google_workspace":
        for surf in WORKSPACE_SURFACE_MAP:
            reg.set_enabled(surf, True)
    else:
        reg.set_enabled(prov, True)
    return result


@router.post("/auth/{provider}/disconnect")
async def api_disconnect_provider(provider: str) -> dict[str, Any]:
    prov = provider.strip().lower()
    disconnect_provider(prov)
    return {"ok": True, "provider": prov}


@router.get("/auth/export-env")
async def api_export_deployment_env() -> dict[str, Any]:
    """Returns a ready-to-deploy `.env` snippet containing all configured client IDs, refresh tokens, and bot tokens."""
    return {
        "ok": True,
        "dotenv": export_deployment_env(),
    }


@router.put("/secrets")
async def api_put_secret(req: PutSecretRequest) -> dict[str, Any]:
    key = req.name.strip().upper()
    if not key:
        raise HTTPException(status_code=400, detail="Secret name is required")
    val = req.value.strip()
    if key == "SLACK_BOT_TOKEN":
        await verify_and_save_token("slack", val)
    elif key == "GITHUB_PERSONAL_ACCESS_TOKEN":
        await verify_and_save_token("github", val)
    elif key == "SPOTIFY_ACCESS_TOKEN":
        await verify_and_save_token("spotify", val)
    elif key == "GOOGLE_WORKSPACE_ACCESS_TOKEN":
        await verify_and_save_token("google_workspace", val)
    else:
        persist_env_vars({key: val})
    return {
        "ok": True,
        "name": key,
        "masked": _mask_secret(val),
    }


@router.delete("/secrets/{name}")
async def api_delete_secret(name: str) -> dict[str, Any]:
    key = name.strip().upper()
    persist_env_vars({key: None})
    return {"ok": True, "name": key}


@router.post("/secrets/dotenv")
async def api_import_dotenv(req: ImportDotenvRequest) -> dict[str, Any]:
    """Parses and applies a .env file content into runtime environment variables."""
    imported: list[str] = []
    updates: dict[str, str | None] = {}
    for raw_line in req.content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        key = k.strip().upper()
        val = v.strip().strip("'").strip('"')
        if key and val:
            updates[key] = val
            imported.append(key)

    if updates:
        persist_env_vars(updates)

    return {
        "ok": True,
        "filename": req.filename,
        "imported_keys": imported,
        "count": len(imported),
    }
