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
    "- When the goal involves creating or building a new AI agent project, ALWAYS plan around scaffolding it with "
    "the pre-installed `agents-cli` (`agents-cli create <name> --adk --prototype -y` or `agents-cli scaffold create`).\n"
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
    "- When creating a new AI agent project, ALWAYS scaffold it using `agents-cli create <name> --adk --prototype -y --deployment-target none` "
    "(or `agents-cli scaffold create --adk -p -y`) rather than writing boilerplate from scratch, then implement the requested domain tools and unit tests.\n"
    "- For software engineering tasks, write or update unit tests and run `pytest` to verify your changes before finishing.\n"
    "- For generalist, research, or planning deliverables (such as menus, shopping lists, or briefs), save the deliverable as a Markdown file "
    "in the workspace AND include the complete deliverable summary in your final response so the voice orchestrator can narrate it or email it directly.\n"
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


def _extract_last_json_dict(raw_stdout: str) -> dict[str, Any]:
    """Extract the terminal JSON dictionary from single-line NDJSON, multi-line JSON, or ```json fences."""
    import re

    stripped = (raw_stdout or "").strip()
    if not stripped:
        return {}

    # 1. Fast path: single-line NDJSON (Claude Code / Antigravity stream-json or json)
    lines = [line.strip() for line in stripped.splitlines() if line.strip()]
    for line in reversed(lines):
        try:
            candidate = json.loads(line)
            if isinstance(candidate, dict):
                return candidate
        except json.JSONDecodeError:
            continue

    # 2. Fenced ```json ... ``` block or raw multi-line JSON object (Horizon A2A)
    fence_matches = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, flags=re.DOTALL)
    for block in reversed(fence_matches):
        try:
            candidate = json.loads(block)
            if isinstance(candidate, dict):
                return candidate
        except json.JSONDecodeError:
            continue

    try:
        candidate = json.loads(stripped)
        if isinstance(candidate, dict):
            return candidate
    except json.JSONDecodeError:
        pass

    # 3. Last-resort brace slice for prose followed by a multi-line JSON object
    first_brace = stripped.find("{")
    last_brace = stripped.rfind("}")
    if 0 <= first_brace < last_brace:
        try:
            candidate = json.loads(stripped[first_brace : last_brace + 1])
            if isinstance(candidate, dict):
                return candidate
        except json.JSONDecodeError:
            pass

    return {}


def extract_plan_from_json_output(
    raw_stdout: str,
    *,
    fallback_session_id: str,
) -> tuple[str, str, list[str], list[str]] | None:
    """Extract `(session_id, plan_summary, questions, files_to_modify)` from Claude, Antigravity, or Horizon JSON output.

    Supports:
    - Claude Code JSON envelope (`session_id`, `structured_output`, `result`)
    - Antigravity JSON envelope (`conversation_id`, `structured_output`, `response`)
    - Horizon A2A top-level `PLAN_OUTPUT_SCHEMA` JSON (including ```json fenced blocks)
    - Streaming NDJSON where the terminal line is a `result` object

    Returns `None` when the harness produced nothing usable (empty stdout, no JSON
    object, or a JSON object carrying neither `structured_output` nor response text).
    """
    payload = _extract_last_json_dict(raw_stdout)
    if not payload:
        return None

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
        # Check if `inner` itself is a top-level PLAN_OUTPUT_SCHEMA object (Horizon A2A)
        if "plan_summary" in inner or "steps" in inner:
            structured = inner
        else:
            # Also check if `response` or `result` is itself a serialized JSON string matching PLAN_OUTPUT_SCHEMA
            raw_resp = inner.get("response") or inner.get("result")
            if isinstance(raw_resp, str):
                nested = _extract_last_json_dict(raw_resp)
                if nested and ("plan_summary" in nested or "steps" in nested):
                    structured = nested

    if isinstance(structured, dict):
        raw_questions = structured.get("questions")
        questions = (
            [str(q) for q in raw_questions if q]
            if isinstance(raw_questions, list)
            else []
        )
        raw_files = structured.get("files_to_modify")
        files = (
            [str(f) for f in raw_files if f]
            if isinstance(raw_files, list)
            else []
        )
        plan_summary = str(
            structured.get("plan_summary")
            or inner.get("response")
            or inner.get("result")
            or ""
        ).strip()
        if not plan_summary and not questions and not files:
            return None
        return str(session_id), plan_summary, questions, files

    text_summary = str(inner.get("response") or inner.get("result") or "").strip()
    if not text_summary:
        return None
    return str(session_id), text_summary, [], []


def extract_plan_steps_from_json_output(raw_stdout: str) -> list[str]:
    """Extract the ordered `steps` array from a harness JSON planning output envelope."""
    payload = _extract_last_json_dict(raw_stdout)
    if not payload:
        return []
    inner = (
        payload.get("result")
        if payload.get("event") == "result" and isinstance(payload.get("result"), dict)
        else payload
    )
    structured = inner.get("structured_output")
    if not isinstance(structured, dict):
        if "steps" in inner or "plan_summary" in inner:
            structured = inner
        else:
            raw_resp = inner.get("response") or inner.get("result")
            if isinstance(raw_resp, str):
                nested = _extract_last_json_dict(raw_resp)
                if nested:
                    structured = nested
    if isinstance(structured, dict):
        raw_steps = structured.get("steps")
        if isinstance(raw_steps, list):
            return [str(s).strip() for s in raw_steps if str(s).strip()]
    return []

