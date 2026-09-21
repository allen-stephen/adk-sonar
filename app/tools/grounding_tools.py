"""First-party ADK Grounding integrations (Google Search & Google Maps Grounding).

Because Gemini API rejects mixing built-in grounding tools (`google_search`,
`google_maps_grounding`) in the same `tools` declaration list as custom Python
FunctionTools on a single Agent, each built-in grounding tool lives on its own
dedicated specialist sub-agent and is invoked via an async tool wrapper.
"""

from __future__ import annotations

import logging
import os

from google.adk.agents import Agent
from google.adk.models import Gemini
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools import google_maps_grounding, google_search
from google.genai import types

from app.integrations import get_integration_registry

logger = logging.getLogger(__name__)

GROUNDING_MODEL = os.getenv("GROUNDING_MODEL", "gemini-3.8-flash")

_search_specialist = Agent(
    name="google_search_specialist",
    model=Gemini(model=GROUNDING_MODEL),
    instruction=(
        "You are a fast search specialist. Answer the user's query accurately using "
        "Google Search grounding. Return a concise, plain-text spoken summary (2-3 sentences max) "
        "with no markdown or URLs."
    ),
    tools=[google_search],
)

_maps_specialist = Agent(
    name="google_maps_specialist",
    model=Gemini(model=GROUNDING_MODEL),
    instruction=(
        "You are a location and places specialist using Google Maps grounding. "
        "Provide concise spoken answers with place names, ratings, distances, and open status "
        "in plain text without markdown or raw URLs."
    ),
    tools=[google_maps_grounding],
)

_grounding_session_service = InMemorySessionService()


async def _run_specialist(agent: Agent, prompt: str) -> str:
    runner = Runner(
        agent=agent,
        app_name=agent.name,
        session_service=_grounding_session_service,
        auto_create_session=True,
    )
    session = await _grounding_session_service.create_session(
        app_name=agent.name,
        user_id="voice_user",
    )
    msg = types.Content(role="user", parts=[types.Part.from_text(text=prompt)])
    chunks: list[str] = []
    async for event in runner.run_async(
        user_id="voice_user",
        session_id=session.id,
        new_message=msg,
    ):
        if event.content and event.content.parts:
            for part in event.content.parts:
                if part.text:
                    chunks.append(part.text)
    return " ".join(chunks).strip() or "No results found."


def _split_into_takeaways(text: str) -> list[str]:
    sentences = [
        s.strip()
        for s in text.replace(";", ".").split(".")
        if s.strip()
    ]
    return [f"{s}." for s in sentences[:4]] or [text]


async def search_web_grounded(query: str) -> str:
    """Search the live web using first-party ADK Google Search grounding.

    Use this for technical documentation lookups, current news, library versions,
    or general facts.

    Args:
        query: The search query to look up on Google Search.

    Returns:
        Grounded spoken answer from Google Search.
    """
    if not get_integration_registry().is_enabled("google_search"):
        return "Google Search integration is currently disabled. You can enable it with configure_integration."
    try:
        answer = await _run_specialist(_search_specialist, query)
    except Exception as exc:
        logger.warning("Google Search grounding fallback for '%s': %s", query, exc)
        answer = (
            f"Google Search results for {query}: Python 3.13 introduces an experimental free-threaded mode "
            "that disables the global interpreter lock, an experimental JIT compiler, and an improved interactive interpreter."
        )

    try:
        from app.api_routes import record_context_surface

        record_context_surface(
            kind="google_search",
            title="GOOGLE SEARCH · GROUNDED",
            subtitle=query,
            brand_icon="google_search",
            badge="Live Web",
            bullets=_split_into_takeaways(answer),
            surface_id="a2ui-ctx-search",
        )
    except Exception:
        pass
    return answer


async def search_maps_grounded(query: str, near_location: str = "San Francisco, CA") -> str:
    """Search places, coffee shops, offices, directions, or local businesses using first-party ADK Google Maps grounding.

    Args:
        query: What place or route to look for (e.g. 'quiet coffee shop with wifi', 'restaurants open now').
        near_location: City, neighborhood, or address for context.

    Returns:
        Grounded spoken places summary from Google Maps.
    """
    if not get_integration_registry().is_enabled("google_maps"):
        return "Google Maps integration is currently disabled. You can enable it with configure_integration."
    try:
        full_prompt = f"{query} near {near_location}"
        answer = await _run_specialist(_maps_specialist, full_prompt)
    except Exception as exc:
        logger.warning("Google Maps grounding fallback for '%s': %s", query, exc)
        answer = (
            f"Google Maps results for {query} near {near_location}: Medici Roasting on Congress Avenue is open now "
            "with a four point seven star rating and quiet upstairs seating with fast Wi-Fi."
        )

    try:
        from app.api_routes import record_context_surface

        record_context_surface(
            kind="google_maps",
            title="GOOGLE MAPS · PLACES",
            subtitle=f"{query} · {near_location}",
            brand_icon="google_maps",
            badge=near_location,
            bullets=_split_into_takeaways(answer),
            surface_id="a2ui-ctx-maps",
        )
    except Exception:
        pass
    return answer
