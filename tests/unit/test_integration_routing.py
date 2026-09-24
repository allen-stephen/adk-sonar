"""Unit tests for the Voice Orchestrator downstream integration routing layer and eval metric."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from app.api_routes import WORKSPACE_SURFACE_MAP
from app.integrations import configure_integration, reset_integration_registry
from app.tools.integration_tools import (
    calendar_events,
    drive_files,
    github_operations,
    gmail_messages,
    slack_messages,
    spotify_playback,
)


@pytest.fixture(autouse=True)
def _reset_registry():
    reset_integration_registry()
    yield
    reset_integration_registry()


def test_workspace_surfaces_removed_search_and_directory():
    """Verify 'Search' (universal_search) and 'Directory' (google_people) were removed from Workspace options."""
    assert "universal_search" not in WORKSPACE_SURFACE_MAP
    assert "google_people" not in WORKSPACE_SURFACE_MAP
    assert set(WORKSPACE_SURFACE_MAP.keys()) == {
        "google_calendar",
        "gmail",
        "google_drive",
        "google_chat",
    }


@pytest.mark.asyncio
async def test_downstream_integration_tools_and_runtime_toggle(
    monkeypatch: pytest.MonkeyPatch,
):
    """Verify Spotify, Calendar, Gmail, Drive, Slack, and GitHub routing tools fail closed when unauthenticated and execute real HTTP routes when authenticated."""
    # 1. Fail-closed when unauthenticated
    for key in (
        "SPOTIFY_ACCESS_TOKEN",
        "SPOTIFY_REFRESH_TOKEN",
        "GOOGLE_WORKSPACE_ACCESS_TOKEN",
        "GOOGLE_WORKSPACE_REFRESH_TOKEN",
        "SLACK_BOT_TOKEN",
        "GITHUB_PERSONAL_ACCESS_TOKEN",
        "GH_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)

    assert "not authenticated" in await spotify_playback(action="play", query="Deep Focus Electronic")
    assert "not authenticated" in await calendar_events(action="list", time_window="today")
    assert "not authenticated" in await gmail_messages(action="search", query="auth service")
    assert "not authenticated" in await drive_files(action="search", query="token rotation PRD")
    assert "not configured" in await slack_messages(action="read", channel="eng-alerts")
    assert "not authenticated" in await github_operations(action="ci_status", repo="auth-svc")

    # 2. Authenticated execution via integrations_transport
    monkeypatch.setenv("SPOTIFY_ACCESS_TOKEN", "spotify-token-123")
    monkeypatch.setenv("GOOGLE_WORKSPACE_ACCESS_TOKEN", "workspace-token-123")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "slack-token-123")
    monkeypatch.setenv("GITHUB_PERSONAL_ACCESS_TOKEN", "github-token-123")

    # Spotify
    out_spotify = await spotify_playback(action="play", query="Deep Focus Electronic")
    assert "Deep Focus Electronic" in out_spotify
    await configure_integration("spotify", False)
    out_disabled = await spotify_playback(action="play")
    assert "disabled" in out_disabled
    await configure_integration("spotify", True)

    # Calendar
    out_cal = await calendar_events(action="list", time_window="today")
    assert "Google Calendar" in out_cal

    # Gmail
    out_gmail = await gmail_messages(action="search", query="auth service")
    assert "Gmail" in out_gmail

    # Drive
    out_drive = await drive_files(action="search", query="token rotation PRD")
    assert "Google Drive" in out_drive

    # Slack
    out_slack = await slack_messages(action="read", channel="eng-alerts")
    assert "Slack" in out_slack

    # GitHub
    out_gh = await github_operations(action="ci_status", repo="auth-svc", number=18)
    assert "GitHub Actions CI" in out_gh


def test_routing_dataset_and_eval_metric_alignment():
    """Verify every case in routing-dataset.json is graded by expected_tool_called in eval_config.yaml."""
    project_root = Path(__file__).resolve().parent.parent.parent
    dataset_path = project_root / "tests" / "eval" / "datasets" / "routing-dataset.json"
    config_path = project_root / "tests" / "eval" / "eval_config.yaml"

    dataset = json.loads(dataset_path.read_text())
    config = yaml.safe_load(config_path.read_text())

    metric_spec = next(
        m for m in config["custom_metrics"] if m["name"] == "expected_tool_called"
    )
    ns: dict = {}
    exec(metric_spec["custom_function"], ns)
    evaluate_fn = ns["evaluate"]

    expected_tool_for_case = {
        "route_spotify_music": "spotify_playback",
        "route_google_search_realtime": "search_web_grounded",
        "route_google_maps_places": "search_maps_grounded",
        "route_calendar_schedule": "calendar_events",
        "route_gmail_inbox": "gmail_messages",
        "route_drive_docs": "drive_files",
        "route_slack_channel": "slack_messages",
        "route_github_pr_status": "github_operations",
        "route_coding_harness_dispatch": "dispatch_task",
    }

    case_ids = [c["eval_case_id"] for c in dataset["eval_cases"]]
    assert set(case_ids) == set(expected_tool_for_case.keys())

    for cid, correct_tool in expected_tool_for_case.items():
        # Positive trace -> score 1.0
        pos_instance = {
            "eval_case_id": cid,
            "agent_data": {
                "turns": [
                    {
                        "events": [
                            {
                                "content": {
                                    "parts": [{"function_call": {"name": correct_tool}}]
                                }
                            }
                        ]
                    }
                ]
            },
        }
        assert evaluate_fn(pos_instance)["score"] == 1.0

        # Negative trace (wrong tool called) -> score 0.0
        neg_instance = {
            "eval_case_id": cid,
            "agent_data": {
                "turns": [
                    {
                        "events": [
                            {
                                "content": {
                                    "parts": [{"function_call": {"name": "list_harnesses"}}]
                                }
                            }
                        ]
                    }
                ]
            },
        }
        assert evaluate_fn(neg_instance)["score"] == 0.0


def test_calendar_timezone_bounds_and_event_filtering():
    """Verify Google Calendar filtering drops working locations, tasks/free reminders, declined invites, and ended meetings."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from app.tools.integration_tools import (
        _filter_and_rank_calendar_events,
        _resolve_calendar_time_bounds,
    )

    tz = ZoneInfo("America/Chicago")
    now_local = datetime(2026, 9, 23, 14, 0, 0, tzinfo=tz)  # 2:00 PM CDT

    t_min, t_max, label = _resolve_calendar_time_bounds("today", now_local)
    assert t_min == now_local
    assert t_max.hour == 23 and t_max.minute == 59
    assert label == "the rest of today"

    tmr_min, tmr_max, tmr_label = _resolve_calendar_time_bounds("tomorrow", now_local)
    assert tmr_min.day == 24 and tmr_min.hour == 0
    assert tmr_max.day == 24 and tmr_max.hour == 23
    assert tmr_label == "tomorrow"

    raw_items = [
        # 1. Working location banner -> dropped
        {
            "summary": "Home",
            "eventType": "workingLocation",
            "start": {"date": "2026-09-23"},
            "end": {"date": "2026-09-24"},
        },
        # 2. All-day task/reminder hold (transparent/Free) -> dropped
        {
            "summary": "Submit Q3 expense report",
            "transparency": "transparent",
            "start": {"dateTime": "2026-09-23T15:00:00-05:00"},
            "end": {"dateTime": "2026-09-23T15:30:00-05:00"},
        },
        # 3. Declined meeting -> dropped
        {
            "summary": "Optional Vendor Sync",
            "start": {"dateTime": "2026-09-23T15:30:00-05:00"},
            "end": {"dateTime": "2026-09-23T16:00:00-05:00"},
            "attendees": [{"email": "me@example.com", "self": True, "responseStatus": "declined"}],
        },
        # 4. Already-ended morning meeting (10:00 AM - 10:30 AM CDT) -> dropped
        {
            "summary": "Morning Standup",
            "start": {"dateTime": "2026-09-23T10:00:00-05:00"},
            "end": {"dateTime": "2026-09-23T10:30:00-05:00"},
        },
        # 5. Unresponded pending invite -> deprioritized when confirmed meetings exist
        {
            "summary": "Cold Sales Intro",
            "start": {"dateTime": "2026-09-23T16:00:00-05:00"},
            "end": {"dateTime": "2026-09-23T16:30:00-05:00"},
            "attendees": [{"email": "me@example.com", "self": True, "responseStatus": "needsAction"}],
        },
        # 6. Confirmed upcoming meeting (3:00 PM - 3:45 PM CDT) -> kept!
        {
            "summary": "Architecture Design Review",
            "start": {"dateTime": "2026-09-23T15:00:00-05:00"},
            "end": {"dateTime": "2026-09-23T15:45:00-05:00"},
            "attendees": [{"email": "me@example.com", "self": True, "responseStatus": "accepted"}],
        },
    ]

    filtered = _filter_and_rank_calendar_events(raw_items, now_local=now_local)
    assert [ev["summary"] for ev in filtered] == ["Architecture Design Review"]

    # If the user explicitly asks for pending invites, pending invites are included
    pending_filtered = _filter_and_rank_calendar_events(
        raw_items, now_local=now_local, query="pending invites"
    )
    assert "Cold Sales Intro" in [ev["summary"] for ev in pending_filtered]

