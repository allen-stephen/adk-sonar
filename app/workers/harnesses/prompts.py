"""Dedicated worker-level system instructions and JSON schemas for headless coding harnesses.

Kept strictly separate from the voice orchestrator prompt in `app/agent.py` so each
coding harness (Claude Code, Antigravity, and ADK Long Horizon) shares a deterministic
2-phase headless contract:
  1. Phase 1 (`mode="plan"`): Read-only inspection + structured plan & clarifying questions (`--json-schema`).
  2. Phase 2 (`mode="execute"`): Session continuation (`--resume` / `--conversation`) + autonomous unattended execution.
"""

from __future__ import annotations

import json
from typing import Any

PLAN_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "plan_summary": {
            "type": "string",
            "description": "Concise 1-2 sentence summary of the proposed implementation plan.",
        },
        "steps": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Ordered technical steps to execute the plan.",
        },
        "questions": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Clarifying questions or architectural options for user approval before execution.",
        },
        "files_to_modify": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Repository file paths expected to be created or modified.",
        },
    },
    "required": ["plan_summary", "steps", "questions"],
}

PLAN_OUTPUT_SCHEMA_JSON: str = json.dumps(PLAN_OUTPUT_SCHEMA, separators=(",", ":"))

PLAN_SYSTEM_PROMPT_TEMPLATE = (
    "You are an autonomous software engineering, research, and workspace harness operating inside an isolated "
    "git worktree ({worktree_path}) on branch '{branch}' for workspace/repository '{repo}'.\n"
    "CURRENT PHASE: PHASE 1 — ITERATIVE PLANNING, RESEARCH & TRADE-OFF ANALYSIS (READ-ONLY).\n"
    "- Inspect the workspace files, codebase architecture, or primary research sources to design a concrete plan.\n"
    "- Leverage your pre-installed Agent Skills (`agents-cli` skills, `brainstorming`, `writing-plans`, `research`, "
    "`grill-me`, `code-review`, `doc-coauthoring`, `agent-browser`, or `npx -y skills find <query>` via `find-skills`) "
    "and active GCP authentication (`$GOOGLE_CLOUD_PROJECT`) as needed.\n"
    "- DO NOT modify, create, or delete workspace files, and DO NOT run mutating deployments (`agents-cli deploy`) during this phase.\n"
    "- Produce a structured `plan_summary` (including key findings and rationale), ordered `steps` (explicitly noting any "
    "proposed code changes, artifacts to generate, or cloud deployments), `files_to_modify`, and `questions` "
    "(highlighting architectural trade-offs or approach options so the user can iterate on the plan with you before execution)."
)

EXECUTE_SYSTEM_PROMPT_TEMPLATE = (
    "You are an autonomous software engineering, research, and workspace harness operating inside an isolated "
    "git worktree ({worktree_path}) on branch '{branch}' for workspace/repository '{repo}'.\n"
    "CURRENT PHASE: PHASE 2 — AUTONOMOUS UNATTENDED EXECUTION.\n"
    "- The user has reviewed and locked the plan and provided any final steering direction.\n"
    "- Execute the implementation, research synthesis, document generation, evaluation (`agents-cli eval`), or approved deployment "
    "(`agents-cli deploy` against `$GOOGLE_CLOUD_PROJECT`) end-to-end autonomously.\n"
    "- Leverage pre-installed Agent Skills (`executing-plans`, `test-driven-development`, `systematic-debugging`, "
    "`verification-before-completion`, `doc-coauthoring`, `pdf`/`docx`/`xlsx`/`pptx`) to verify your work and produce "
    "a clear summary of all changes applied or deliverables created."
)


def build_harness_system_prompt(
    *,
    mode: str,
    repo: str,
    worktree_path: str = "/workspace",
    branch: str = "main",
) -> str:
    """Return the phase-specific system instruction injected into the coding harness."""
    template = (
        PLAN_SYSTEM_PROMPT_TEMPLATE
        if mode == "plan"
        else EXECUTE_SYSTEM_PROMPT_TEMPLATE
    )
    return template.format(
        repo=repo,
        worktree_path=worktree_path,
        branch=branch,
    )


def build_harness_user_prompt(
    *,
    goal: str,
    repo: str,
    mode: str = "execute",
    worktree_path: str = "/workspace",
    branch: str = "main",
    include_system_preamble: bool = False,
) -> str:
    """Build the user prompt passed to `-p` or A2A for either planning or autonomous execution."""
    sys_prompt = build_harness_system_prompt(
        mode=mode,
        repo=repo,
        worktree_path=worktree_path,
        branch=branch,
    )
    if include_system_preamble:
        return f"{sys_prompt}\n\nTASK GOAL: {goal}"
    return goal


def build_cross_harness_handoff(
    *,
    previous_harness: str,
    new_harness: str,
    previous_summary: str | None,
    files_changed: list[str] | None,
    worktree_path: str | None,
    branch: str | None,
) -> str:
    """Construct a structured state-transfer preamble when a user switches harnesses mid-task on the same worktree."""
    files_str = ", ".join(files_changed) if files_changed else "none yet"
    summary_str = previous_summary or "Initial inspection completed."
    wt_str = worktree_path or "/workspace"
    br_str = branch or "main"
    return (
        f"[CROSS-HARNESS HANDOFF: {previous_harness} -> {new_harness}]\n"
        f"- Shared Worktree Path: {wt_str} (branch: {br_str})\n"
        f"- Prior Harness Summary: {summary_str}\n"
        f"- Files Touched in Shared Worktree: {files_str}\n"
        f"- All repositories, installed packages, .mcp.json, and AGENTS.md/CLAUDE.md/GEMINI.md context files "
        f"are already synchronized in this worktree. Continue directly from this state."
    )


def extract_plan_from_json_output(
    raw_stdout: str,
    *,
    fallback_session_id: str,
    goal: str,
    repo: str,
    display_name: str,
) -> tuple[str, str, list[str], list[str]]:
    """Extract `(session_id, plan_summary, questions, files_to_modify)` from Claude or Antigravity JSON output.

    Supports:
    - Claude Code JSON envelope (`session_id`, `structured_output`, `result`)
    - Antigravity JSON envelope (`conversation_id`, `structured_output`, `response`)
    - Streaming NDJSON where the terminal line is a `result` object
    """
    default_summary = f"Proposed plan from {display_name} for {goal} in {repo}."
    default_questions = [
        f"Should the implementation for '{goal}' in {repo} include automated unit tests?",
        "Do you prefer a standalone module or integrating into existing utilities?",
    ]

    if not raw_stdout or not raw_stdout.strip():
        return fallback_session_id, default_summary, default_questions, []

    lines = [line.strip() for line in raw_stdout.strip().splitlines() if line.strip()]
    payload: dict[str, Any] = {}
    for line in reversed(lines):
        try:
            candidate = json.loads(line)
            if isinstance(candidate, dict):
                payload = candidate
                break
        except json.JSONDecodeError:
            continue

    if not payload:
        return fallback_session_id, default_summary, default_questions, []

    # Unwrap Antigravity stream-json `{"event": "result", "result": {...}}` envelope if present
    inner = (
        payload.get("result")
        if payload.get("event") == "result" and isinstance(payload.get("result"), dict)
        else payload
    )

    session_id = (
        inner.get("session_id")
        or inner.get("conversation_id")
        or payload.get("session_id")
        or payload.get("conversation_id")
        or fallback_session_id
    )

    structured = inner.get("structured_output")
    if not isinstance(structured, dict):
        # Also check if `response` or `result` is itself a serialized JSON string matching PLAN_OUTPUT_SCHEMA
        raw_resp = inner.get("response") or inner.get("result")
        if isinstance(raw_resp, str):
            try:
                parsed_resp = json.loads(raw_resp)
                if isinstance(parsed_resp, dict):
                    structured = parsed_resp
            except json.JSONDecodeError:
                pass

    if isinstance(structured, dict):
        plan_summary = str(structured.get("plan_summary") or default_summary)
        raw_questions = structured.get("questions")
        questions = (
            [str(q) for q in raw_questions if q]
            if isinstance(raw_questions, list) and raw_questions
            else default_questions
        )
        raw_files = structured.get("files_to_modify")
        files = (
            [str(f) for f in raw_files if f]
            if isinstance(raw_files, list)
            else []
        )
        return str(session_id), plan_summary, questions, files

    text_summary = str(
        inner.get("response") or inner.get("result") or default_summary
    ).strip()
    return str(session_id), text_summary or default_summary, default_questions, []


def extract_plan_steps_from_json_output(raw_stdout: str) -> list[str]:
    """Extract the ordered `steps` array from a harness JSON planning output envelope."""
    if not raw_stdout or not raw_stdout.strip():
        return []
    for line in reversed([ln.strip() for ln in raw_stdout.strip().splitlines() if ln.strip()]):
        try:
            payload = json.loads(line)
            if not isinstance(payload, dict):
                continue
            inner = (
                payload.get("result")
                if payload.get("event") == "result" and isinstance(payload.get("result"), dict)
                else payload
            )
            structured = inner.get("structured_output")
            if not isinstance(structured, dict):
                raw_resp = inner.get("response") or inner.get("result")
                if isinstance(raw_resp, str):
                    try:
                        parsed_resp = json.loads(raw_resp)
                        if isinstance(parsed_resp, dict):
                            structured = parsed_resp
                    except json.JSONDecodeError:
                        pass
            if isinstance(structured, dict):
                raw_steps = structured.get("steps")
                if isinstance(raw_steps, list):
                    return [str(s).strip() for s in raw_steps if str(s).strip()]
            break
        except json.JSONDecodeError:
            continue
    return []
