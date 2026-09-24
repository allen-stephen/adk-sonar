"""LLM-as-judge metric for L1 sandbox CUJ evaluations (`cuj_goal_achievement`).

Grades the full multi-step CUJ trajectory (`plan` -> `steer` / `execute`) produced by a
sandbox coding harness (`claude`, `antigravity`, `horizon`) against the case's reference.
"""

from __future__ import annotations

import json
import os
import threading

from dotenv import load_dotenv
from google import genai
from google.genai import types
from pydantic import BaseModel

load_dotenv(".env")

_local = threading.local()


class _Verdict(BaseModel):
    score: int  # 1-5
    explanation: str


def _client() -> genai.Client:
    client = getattr(_local, "client", None)
    if client is None:
        project = (
            os.getenv("SANDBOX_GCP_PROJECT")
            or os.getenv("GOOGLE_CLOUD_PROJECT")
            or ""
        ).strip()
        location = os.getenv("SANDBOX_GCP_LOCATION", "us-central1").strip()
        client = _local.client = genai.Client(
            vertexai=True,
            project=project,
            location=location,
            http_options=types.HttpOptions(
                retry_options=types.HttpRetryOptions(attempts=3, initial_delay=1.0)
            ),
        )
    return client


def _extract_sandbox_meta(instance: dict) -> dict:
    agent_data = instance.get("agent_data") or {}
    for turn in agent_data.get("turns") or []:
        for ev in turn.get("events") or []:
            for part in (ev.get("content") or {}).get("parts") or []:
                fr = part.get("function_response") if isinstance(part, dict) else None
                if isinstance(fr, dict) and isinstance(fr.get("response"), dict):
                    return fr["response"]
    return {}


def evaluate(instance: dict) -> dict:
    ctx_obj = _extract_sandbox_meta(instance)
    reference = ctx_obj.get("reference") or instance.get("reference") or ""
    prompt_obj = instance.get("prompt", "")
    response_obj = instance.get("response", "")

    rubric = (
        "You are an expert evaluator grading a multi-step Customer User Journey (CUJ) "
        "executed inside an isolated sandbox by an autonomous harness (Claude Code, Antigravity, or ADK Long Horizon).\n"
        "Grade on a 1-5 scale (1 = failed/incomplete, 5 = complete, accurate, high-quality deliverable):\n"
        "1. Did the harness follow the multi-step trajectory (collaborative read-only planning first, "
        "then incorporating the user's steering feedback in execution)?\n"
        "2. Did the final output and workspace deliverables satisfy the Expected Reference criteria "
        "(whether a generalist menu & categorized shopping list, a greenfield ADK agent created with "
        "`agents-cli`, or a tested code change in an existing repository)?\n"
        "3. Is the final summary clear and well-structured for the voice orchestrator to convey to the user?"
    )

    judge_prompt = (
        f"{rubric}\n\n"
        f"Initial User Goal: {prompt_obj}\n"
        f"Expected Reference Criteria: {reference}\n"
        f"Trajectory & Worktree Verification Summary:\n{response_obj}\n"
        f"Full Structured Telemetry:\n{json.dumps(ctx_obj, indent=2)[:6000]}\n"
    )

    response = _client().models.generate_content(
        model="gemini-2.5-flash",
        contents=judge_prompt,
        config=types.GenerateContentConfig(
            temperature=0,
            response_mime_type="application/json",
            response_schema=_Verdict,
        ),
    )
    verdict = response.parsed
    if verdict is None:
        return {"score": 0, "explanation": response.text or "Empty judge verdict"}
    return {
        "score": max(1, min(5, verdict.score)),
        "explanation": verdict.explanation,
    }
