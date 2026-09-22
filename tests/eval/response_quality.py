"""Local LLM-as-judge for `custom_response_quality` (see eval_config.yaml)."""

import threading

from google import genai
from google.genai import types
from pydantic import BaseModel

_local = threading.local()


class _Verdict(BaseModel):
    score: int  # 1-5
    explanation: str


def _client() -> genai.Client:
    """One client per grading thread.

    The eval SDK grades cases on its own thread pool; this initialization runs once
    per thread. Avoids creating a new client for each eval case, which would re-do
    ADC and the TLS handshake every time. Each thread gets its own client, because
    google-auth freezes the SSL context after the first connection when a client
    certificate is present.
    """
    client = getattr(_local, "client", None)
    if client is None:
        # AI Studio (GEMINI_API_KEY) or Agent Platform (ADC) with automatic retry on transient 503s.
        client = _local.client = genai.Client(
            http_options=types.HttpOptions(
                retry_options=types.HttpRetryOptions(attempts=6, initial_delay=1.5)
            )
        )
    return client


def evaluate(instance):
    reference = instance.get("reference")
    rubric = (
        "Grade the voice agent's response on a 1-5 scale (1 poor, 5 excellent) for "
        "accuracy, relevance, and clarity. "
        "IMPORTANT: For asynchronous NON_BLOCKING tools (`dispatch_task`, `steer_task`, "
        "`approve_task`, `search_web_grounded`, `search_maps_grounded`), the voice agent is "
        "explicitly designed to immediately speak a brief acknowledgment (e.g. 'Let me check "
        "Google Search for that' or 'Starting the Claude Code planning phase now') while the "
        "background tool executes asynchronously (`status: The function is running asynchronously "
        "and the results are pending`). Award a full 5/5 when the agent invokes the correct tool "
        "with appropriate arguments and speaks a clear, natural acknowledgment or grounded answer."
    )
    if reference:
        rubric += (
            " The response should agree with the expected answer below; penalize "
            "factual disagreement with it."
        )
    prompt = (
        f"You are an expert QA evaluator for an enterprise AI assistant. {rubric}\n"
        f"User Prompt: {instance.get('prompt', '')}\n"
        f"Final Response: {instance.get('response', '')}\n"
    )
    if reference:
        prompt += f"Expected Answer (ground truth): {reference}\n"
    prompt += f"Full Agent Trace: {instance.get('agent_data', '')}\n"

    response = _client().models.generate_content(
        model="gemini-3.8-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0,  # deterministic grading
            response_mime_type="application/json",
            response_schema=_Verdict,  # guaranteed schema-valid JSON
        ),
    )
    verdict = response.parsed
    if verdict is None:  # model returned nothing usable
        return {"score": 0, "explanation": response.text or ""}
    return {"score": max(1, min(5, verdict.score)), "explanation": verdict.explanation}
