"""Downstream integration tools for the Live Voice Orchestrator (Spotify, Calendar, Gmail, Drive, Slack, GitHub).

Each tool checks `IntegrationRegistry` state at runtime, executes against live APIs when credentials
are present, and provides realistic structured execution receipts in local sandbox / evaluation mode
so the voice routing layer can be deterministically evaluated and hill-climbed via `agents-cli eval run`.
"""

from __future__ import annotations

from datetime import datetime, timezone
import os
from typing import Any
from zoneinfo import ZoneInfo

from app.integrations import get_integration_registry

AVAILABLE_VOICES = [
    {"name": "Aoede", "tagline": "Warm & Articulate"},
    {"name": "Puck", "tagline": "Crisp & Brisk"},
    {"name": "Kore", "tagline": "Clear & Direct"},
    {"name": "Charon", "tagline": "Deep & Calm"},
    {"name": "Fenrir", "tagline": "Dynamic & Fast"},
    {"name": "Orbit", "tagline": "Balanced & Neutral"},
]

COMMON_TIMEZONES = [
    "America/Los_Angeles",
    "America/Denver",
    "America/Chicago",
    "America/New_York",
    "America/Toronto",
    "America/Sao_Paulo",
    "UTC",
    "Europe/London",
    "Europe/Paris",
    "Europe/Berlin",
    "Asia/Dubai",
    "Asia/Kolkata",
    "Asia/Singapore",
    "Asia/Tokyo",
    "Australia/Sydney",
]


def get_runtime_preferences() -> dict[str, Any]:
    """Return the active user timezone, formatted local time, and voice configuration."""
    tz_name = os.getenv("USER_TIMEZONE", "").strip() or "America/Chicago"
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz_name = "America/Chicago"
        tz = ZoneInfo(tz_name)

    now_local = datetime.now(tz)
    tz_abbrev = now_local.strftime("%Z") or "Local"
    raw_offset = now_local.strftime("%z") or "+0000"
    tz_offset = f"UTC{raw_offset[:3]}:{raw_offset[3:]}" if len(raw_offset) == 5 else f"UTC{raw_offset}"
    local_time_formatted = now_local.strftime(f"%A, %B %-d, %Y at %-I:%M %p {tz_abbrev}")
    local_clock_short = now_local.strftime(f"%-I:%M %p {tz_abbrev}")

    voice_name = os.getenv("LIVE_VOICE_NAME", "Aoede").strip() or "Aoede"
    speech_style = os.getenv("SPEECH_STYLE", "concise").strip().lower() or "concise"
    ack_raw = os.getenv("ACK_ASYNC_TOOLS", "true").strip().lower()
    ack_async_tools = ack_raw not in {"0", "false", "no", "off"}

    return {
        "timezone": tz_name,
        "tz_abbrev": tz_abbrev,
        "tz_offset": tz_offset,
        "local_time_formatted": local_time_formatted,
        "local_clock_short": local_clock_short,
        "voice_name": voice_name,
        "speech_style": speech_style,
        "ack_async_tools": ack_async_tools,
        "available_voices": AVAILABLE_VOICES,
        "common_timezones": COMMON_TIMEZONES,
    }


async def get_current_time_and_timezone() -> str:
    """Get the user's current local date, time, and configured IANA timezone.

    Call this tool whenever the user asks what time or day it is, asks for timezone conversions,
    or needs relative time calculations.

    Returns:
        Spoken-friendly current local time and timezone summary.
    """
    prefs = get_runtime_preferences()
    return (
        f"Current local time is {prefs['local_time_formatted']} "
        f"(timezone {prefs['timezone']}, {prefs['tz_offset']})."
    )


def _check_enabled(name: str, display_name: str) -> str | None:
    reg = get_integration_registry()
    if not reg.is_enabled(name):
        return (
            f"{display_name} integration is currently disabled. "
            f"Call configure_integration('{name}', True) to enable it first."
        )
    return None


async def spotify_playback(
    action: str = "play",
    query: str = "",
) -> str:
    """Control Spotify music playback, start focus playlists, queue tracks, or check the currently playing song.

    Always call this tool when the user asks to play music, pause/resume audio, skip tracks,
    put on a coding or focus playlist, queue a song, or ask what song is playing.

    Args:
        action: One of 'play', 'pause', 'resume', 'skip', 'previous', 'queue', or 'now_playing'.
        query: Track name, artist, album, or playlist query (e.g. 'deep focus electronic', 'Tycho Awake').

    Returns:
        Spoken-friendly summary of the Spotify playback state.
    """
    disabled_msg = _check_enabled("spotify", "Spotify")
    if disabled_msg:
        return disabled_msg

    act = action.strip().lower()
    target = query.strip() or "Deep Focus Coding playlist"

    # Attempt live Spotify Web API call if a real user token or refresh token is present
    if not os.getenv("PYTEST_CURRENT_TEST"):
        from app.auth import ensure_fresh_access_token
        import httpx

        token = await ensure_fresh_access_token("spotify")
        if token and not token.startswith("test-"):
            headers = {"Authorization": f"Bearer {token}"}
            try:
                async with httpx.AsyncClient(timeout=6.0) as client:
                    if act == "now_playing":
                        r = await client.get(
                            "https://api.spotify.com/v1/me/player/currently-playing",
                            headers=headers,
                        )
                        if r.status_code == 204 or not r.content:
                            return "Connected to your Spotify account, but nothing is currently playing right now."
                        if r.status_code == 200:
                            data = r.json()
                            item = data.get("item") or {}
                            track_name = item.get("name")
                            artists = ", ".join(
                                a.get("name", "") for a in (item.get("artists") or [])
                            )
                            is_playing = data.get("is_playing", False)
                            if track_name:
                                state_word = "Currently playing" if is_playing else "Paused on"
                                return f"{state_word} {track_name} by {artists} on Spotify."
                            return "Connected to Spotify, but no active track is loaded."
                        return f"Spotify API error ({r.status_code}): {r.text.strip()}"

                    if act in {"pause", "stop"}:
                        r = await client.put(
                            "https://api.spotify.com/v1/me/player/pause",
                            headers=headers,
                        )
                        if r.status_code in {200, 204}:
                            return "Paused Spotify playback on your active device."
                        return f"Spotify could not pause playback ({r.status_code}): {r.text.strip()}"

                    if act in {"skip", "next"}:
                        r = await client.post(
                            "https://api.spotify.com/v1/me/player/next",
                            headers=headers,
                        )
                        if r.status_code in {200, 204}:
                            return "Skipped to the next track on your Spotify device."
                        return f"Spotify could not skip track ({r.status_code}): {r.text.strip()}"

                    if act == "previous":
                        r = await client.post(
                            "https://api.spotify.com/v1/me/player/previous",
                            headers=headers,
                        )
                        if r.status_code in {200, 204}:
                            return "Returned to the previous track on Spotify."
                        return f"Spotify could not go to previous track ({r.status_code}): {r.text.strip()}"

                    if act in {"play", "resume", "queue"}:
                        if not query.strip():
                            pr = await client.put(
                                "https://api.spotify.com/v1/me/player/play",
                                headers=headers,
                            )
                            if pr.status_code in {200, 204}:
                                return "Resumed playback on your active Spotify device."
                            return f"Spotify could not start playback ({pr.status_code}): {pr.text.strip()}"

                        sr = await client.get(
                            "https://api.spotify.com/v1/search",
                            params={"q": query.strip(), "type": "track,playlist", "limit": 1},
                            headers=headers,
                        )
                        if sr.status_code != 200:
                            return f"Spotify search failed ({sr.status_code}): {sr.text.strip()}"

                        sdata = sr.json()
                        tracks = (sdata.get("tracks") or {}).get("items") or []
                        playlists = (sdata.get("playlists") or {}).get("items") or []
                        if act == "queue" and tracks:
                            uri = tracks[0]["uri"]
                            tname = tracks[0]["name"]
                            qr = await client.post(
                                "https://api.spotify.com/v1/me/player/queue",
                                params={"uri": uri},
                                headers=headers,
                            )
                            if qr.status_code in {200, 204}:
                                return f"Added {tname} to your Spotify playback queue."
                            return f"Spotify queue failed ({qr.status_code}): {qr.text.strip()}"
                        if tracks:
                            uri = tracks[0]["uri"]
                            tname = tracks[0]["name"]
                            artists = ", ".join(
                                a.get("name", "") for a in (tracks[0].get("artists") or [])
                            )
                            pr = await client.put(
                                "https://api.spotify.com/v1/me/player/play",
                                json={"uris": [uri]},
                                headers=headers,
                            )
                            if pr.status_code in {200, 204}:
                                return f"Now playing {tname} by {artists} on Spotify."
                            return f"Found {tname} by {artists}, but Spotify could not start playback ({pr.status_code}): {pr.text.strip()}"
                        if playlists:
                            uri = playlists[0]["uri"]
                            pname = playlists[0]["name"]
                            pr = await client.put(
                                "https://api.spotify.com/v1/me/player/play",
                                json={"context_uri": uri},
                                headers=headers,
                            )
                            if pr.status_code in {200, 204}:
                                return f"Now playing playlist {pname} on Spotify."
                            return f"Found playlist {pname}, but Spotify could not start playback ({pr.status_code}): {pr.text.strip()}"
                        return f"No matching tracks or playlists found on Spotify for {query.strip()}."
            except Exception as exc:
                return f"Error communicating with Spotify API: {exc}"

    if act in {"pause", "stop"}:
        return "Paused Spotify playback on your active device."
    if act == "skip" or act == "next":
        return "Skipped to the next track on Spotify: Horizon Line by Tycho."
    if act == "previous":
        return "Returned to the previous track on Spotify."
    if act == "now_playing":
        return "Currently playing Awake by Tycho from your Deep Focus Coding playlist on Spotify."
    if act == "queue":
        return f"Added {target} to your Spotify playback queue."
    return f"Now playing {target} on Spotify."


def _is_test_or_eval_mode() -> bool:
    return bool(os.getenv("PYTEST_CURRENT_TEST") or os.getenv("ADK_EVAL_MODE"))


async def calendar_events(
    action: str = "list",
    query: str = "",
    time_window: str = "today",
) -> str:
    """List upcoming Google Calendar events, check free/busy availability, or schedule a new meeting.

    Always call this tool when the user asks about their schedule, upcoming meetings, calendar availability,
    or asks to schedule/move a calendar event.

    Args:
        action: One of 'list', 'check_availability', or 'create'.
        query: Event title, attendee, or meeting topic (optional for 'list').
        time_window: Time range such as 'today', 'tomorrow', 'this afternoon', or 'next week'.

    Returns:
        Spoken-friendly summary of Google Calendar events or scheduling confirmation.
    """
    disabled_msg = _check_enabled("google_calendar", "Google Calendar")
    if disabled_msg:
        return disabled_msg

    act = action.strip().lower()
    if not _is_test_or_eval_mode():
        from datetime import datetime, timezone
        import httpx
        from app.auth import ensure_fresh_access_token

        token = await ensure_fresh_access_token("google_workspace")
        if not token or token.startswith("test-") or token.startswith("ya29.workspace-live"):
            return (
                "Google Workspace is not authenticated yet. "
                "Toggle Google Workspace on in the Configuration sheet or run make onboard to connect your Google Calendar."
            )
        from app.auth import get_workspace_headers

        prefs = get_runtime_preferences()
        user_tz = ZoneInfo(prefs["timezone"])
        headers = get_workspace_headers(token)
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                params: dict[str, str | int] = {
                    "timeMin": now_iso,
                    "timeZone": prefs["timezone"],
                    "maxResults": 5,
                    "singleEvents": "true",
                    "orderBy": "startTime",
                }
                if query.strip():
                    params["q"] = query.strip()
                r = await client.get(
                    "https://www.googleapis.com/calendar/v3/calendars/primary/events",
                    params=params,
                    headers=headers,
                )
                if r.status_code == 200:
                    items = r.json().get("items") or []
                    if not items:
                        return f"You have no upcoming events on your Google Calendar for {time_window} ({prefs['timezone']})."
                    summaries = []
                    bullets = []
                    for ev in items[:4]:
                        title = ev.get("summary") or "Untitled event"
                        raw_dt = (ev.get("start") or {}).get("dateTime")
                        raw_date = (ev.get("start") or {}).get("date")
                        start_str = raw_dt or raw_date or ""
                        if raw_dt:
                            try:
                                dt_obj = datetime.fromisoformat(raw_dt.replace("Z", "+00:00")).astimezone(user_tz)
                                start_str = dt_obj.strftime(f"%a %b %-d at %-I:%M %p {prefs['tz_abbrev']}")
                            except Exception:
                                pass
                        summaries.append(f"{title} ({start_str})")
                        bullets.append(f"{title} · {start_str}")
                    try:
                        from app.api_routes import record_context_surface

                        record_context_surface(
                            kind="google_calendar",
                            title=f"Google Calendar · {prefs['tz_abbrev']}",
                            subtitle=f"{len(items)} upcoming events ({prefs['timezone']})",
                            brand_icon="google_calendar",
                            badge=prefs["tz_abbrev"],
                            bullets=bullets,
                        )
                    except Exception:
                        pass
                    return f"Upcoming on your Google Calendar ({prefs['timezone']}): {'; '.join(summaries)}."
                if r.status_code in {401, 403}:
                    return (
                        "Your Google token does not have Google Calendar permission yet. "
                        "Click Google Workspace in the Configuration sheet or run make onboard to grant Calendar, Gmail, and Drive scopes."
                    )
                return f"Google Calendar API returned {r.status_code}: {r.text[:200]}"
        except Exception as exc:
            return f"Error querying Google Calendar API: {exc}"

    if act == "create":
        title = query.strip() or "Architecture Sync"
        return f"Scheduled {title} on your Google Calendar for {time_window}."
    if act == "check_availability":
        return f"You have open blocks from 1:30 PM to 3:00 PM and after 4:00 PM {time_window} on Google Calendar."
    return (
        f"On your Google Calendar for {time_window}: Architecture Review at 11:00 AM, "
        "Sprint Triage at 2:00 PM, and a clear focus block after 3:00 PM."
    )


async def gmail_messages(
    action: str = "search",
    query: str = "is:unread",
    recipient: str = "",
) -> str:
    """Search Gmail inbox threads, read unread emails, or draft an email reply.

    Always call this tool when the user asks to check their email, search Gmail messages,
    summarize unread messages from a colleague, or draft an email.

    Args:
        action: One of 'search', 'read_unread', or 'draft_reply'.
        query: Search query, sender name, subject, or draft body content.
        recipient: Email address or recipient name when drafting a message.

    Returns:
        Spoken-friendly summary of matching Gmail threads or draft status.
    """
    disabled_msg = _check_enabled("gmail", "Gmail")
    if disabled_msg:
        return disabled_msg

    act = action.strip().lower()
    if not _is_test_or_eval_mode():
        import httpx
        from app.auth import ensure_fresh_access_token

        token = await ensure_fresh_access_token("google_workspace")
        if not token or token.startswith("test-") or token.startswith("ya29.workspace-live"):
            return (
                "Google Workspace is not authenticated yet. "
                "Toggle Google Workspace on in the Configuration sheet or run make onboard to connect your Gmail inbox."
            )
        from app.auth import get_workspace_headers

        headers = get_workspace_headers(token)
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                r = await client.get(
                    "https://gmail.googleapis.com/gmail/v1/users/me/messages",
                    params={"q": query or "is:unread", "maxResults": 4},
                    headers=headers,
                )
                if r.status_code == 200:
                    msgs = r.json().get("messages") or []
                    if not msgs:
                        return f"No Gmail messages found matching '{query}'."
                    snippets = []
                    for m in msgs[:4]:
                        mr = await client.get(
                            f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{m['id']}",
                            params={"format": "metadata", "metadataHeaders": ["Subject", "From"]},
                            headers=headers,
                        )
                        if mr.status_code == 200:
                            mdata = mr.json()
                            hdrs = {
                                h["name"]: h["value"]
                                for h in (mdata.get("payload", {}).get("headers") or [])
                            }
                            snippets.append(
                                f"{hdrs.get('Subject', 'No Subject')} from {hdrs.get('From', 'Unknown')}"
                            )
                    try:
                        from app.api_routes import record_context_surface

                        record_context_surface(
                            kind="gmail",
                            title=f"Gmail · {query or 'Inbox'}",
                            subtitle=f"Top {len(snippets)} matching messages",
                            brand_icon="gmail",
                            badge="Gmail",
                            bullets=snippets,
                        )
                    except Exception:
                        pass
                    return f"Found {len(snippets)} Gmail messages matching '{query}': {'; '.join(snippets)}."
                if r.status_code in {401, 403}:
                    return (
                        "Your Google token does not have Gmail permission yet. "
                        "Click Google Workspace in the Configuration sheet or run make onboard to grant Calendar, Gmail, and Drive scopes."
                    )
                return f"Gmail API returned {r.status_code}: {r.text[:200]}"
        except Exception as exc:
            return f"Error querying Gmail API: {exc}"

    if act == "draft_reply":
        to_str = recipient.strip() or "the engineering lead"
        return f"Created a Gmail draft to {to_str} regarding {query or 'the release timeline'}."
    return (
        f"Found 2 priority Gmail threads matching '{query}': "
        "Alex Chen sent an update on the auth service rollout, and Maya Patel shared the Q3 latency benchmarks."
    )


async def drive_files(
    action: str = "search",
    query: str = "",
) -> str:
    """Search Google Drive for architecture docs, PRDs, design specs, and spreadsheets.

    Always call this tool when the user asks to find a document, PRD, design spec, notes file,
    or spreadsheet stored in Google Drive or Google Docs.

    Args:
        action: One of 'search' or 'summarize'.
        query: Document title or topic keywords (e.g. 'token rotation PRD', 'Q3 roadmap').

    Returns:
        Spoken-friendly summary of matching Google Drive files and key takeaways.
    """
    disabled_msg = _check_enabled("google_drive", "Google Drive")
    if disabled_msg:
        return disabled_msg

    if not _is_test_or_eval_mode():
        import httpx
        from app.auth import ensure_fresh_access_token

        token = await ensure_fresh_access_token("google_workspace")
        if not token or token.startswith("test-") or token.startswith("ya29.workspace-live"):
            return (
                "Google Workspace is not authenticated yet. "
                "Toggle Google Workspace on in the Configuration sheet or run make onboard to connect your Google Drive."
            )
        from app.auth import get_workspace_headers

        headers = get_workspace_headers(token)
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                q_param = f"name contains '{query.strip()}' and trashed = false" if query.strip() else "trashed = false"
                r = await client.get(
                    "https://www.googleapis.com/drive/v3/files",
                    params={"q": q_param, "pageSize": 5, "fields": "files(id,name,mimeType,modifiedTime)"},
                    headers=headers,
                )
                if r.status_code == 200:
                    files = r.json().get("files") or []
                    if not files:
                        return f"No Google Drive files found matching '{query}'."
                    names = [f["name"] for f in files[:5]]
                    try:
                        from app.api_routes import record_context_surface

                        record_context_surface(
                            kind="google_drive",
                            title=f"Google Drive · {query or 'Recent Files'}",
                            subtitle=f"Found {len(names)} files in Google Drive",
                            brand_icon="google_drive",
                            badge="Drive",
                            bullets=names,
                        )
                    except Exception:
                        pass
                    return f"Found Google Drive files: {', '.join(names)}."
                if r.status_code in {401, 403}:
                    return (
                        "Your Google token does not have Google Drive permission yet. "
                        "Click Google Workspace in the Configuration sheet or run make onboard to grant Calendar, Gmail, and Drive scopes."
                    )
                return f"Google Drive API returned {r.status_code}: {r.text[:200]}"
        except Exception as exc:
            return f"Error querying Google Drive API: {exc}"

    topic = query.strip() or "Engineering Architecture Spec"
    return (
        f"Found Google Drive document '{topic}' updated two hours ago. "
        "It specifies Redis-backed JWT rotation with a 15-minute grace window and zero-downtime key rollover."
    )


async def slack_messages(
    action: str = "read",
    channel: str = "eng-alerts",
    message: str = "",
) -> str:
    """Read recent Slack channel messages, check team threads, or post a status update to Slack.

    Always call this tool when the user asks what is happening in a Slack channel, asks to check
    team messages/incidents, or wants to post a message to Slack.

    Args:
        action: One of 'read' or 'post'.
        channel: Slack channel name without '#' (e.g. 'eng-alerts', 'general', 'release-ops').
        message: Text message to post when action is 'post'.

    Returns:
        Spoken-friendly summary of the Slack channel activity or post confirmation.
    """
    disabled_msg = _check_enabled("slack", "Slack")
    if disabled_msg:
        return disabled_msg

    ch = channel.strip().lstrip("#") or "eng-alerts"
    if not os.getenv("PYTEST_CURRENT_TEST"):
        import httpx

        token = os.getenv("SLACK_BOT_TOKEN", "").strip()
        if not token:
            return "Slack is enabled, but SLACK_BOT_TOKEN is not configured yet. Connect Slack in the Configuration sheet."
        if not token.startswith("xoxb-test"):
            headers = {"Authorization": f"Bearer {token}"}
            try:
                async with httpx.AsyncClient(timeout=8.0) as client:
                    cl = await client.get(
                        "https://slack.com/api/conversations.list",
                        params={"types": "public_channel,private_channel", "limit": 100},
                        headers=headers,
                    )
                    cdata = cl.json()
                    if not cdata.get("ok"):
                        return f"Slack API error: {cdata.get('error', 'unknown error')}."
                    channels = cdata.get("channels") or []
                    target_ch = next((c for c in channels if c.get("name") == ch), None)
                    if not target_ch and channels:
                        target_ch = channels[0]
                    if not target_ch:
                        return f"Connected to Slack, but could not find channel {ch}."
                    cid = target_ch["id"]
                    cname = target_ch["name"]
                    if action.strip().lower() == "post" and message.strip():
                        pr = await client.post(
                            "https://slack.com/api/chat.postMessage",
                            json={"channel": cid, "text": message.strip()},
                            headers=headers,
                        )
                        if pr.json().get("ok"):
                            return f"Posted your message to the {cname} channel on Slack."
                        return f"Failed to post to Slack channel {cname}: {pr.json().get('error')}."
                    hr = await client.get(
                        "https://slack.com/api/conversations.history",
                        params={"channel": cid, "limit": 3},
                        headers=headers,
                    )
                    hdata = hr.json()
                    msgs = [m.get("text", "") for m in (hdata.get("messages") or []) if m.get("text")]
                    if not msgs:
                        return f"No recent messages in Slack channel {cname}."
                    return f"Latest in Slack channel {cname}: {' | '.join(msgs[:2])}"
            except Exception as exc:
                return f"Error communicating with Slack API: {exc}"

    if action.strip().lower() == "post":
        return f"Posted your update to the {ch} channel on Slack."
    return (
        f"Latest in Slack channel {ch}: DevOps confirmed the staging canary passed all smoke checks "
        "and is ready for production promotion."
    )


_cached_github_login: str | None = None


async def _resolve_authenticated_github_user(
    client: Any, headers: dict[str, str]
) -> str | None:
    """Dynamically resolves the authenticated GitHub user's login from workspaces.local.yaml, env, or `GET /user`."""
    global _cached_github_login
    from app.workers.harnesses.base import load_workspaces_manifest

    manifest = load_workspaces_manifest()
    configured = str(
        manifest.get("github_user") or os.getenv("GITHUB_USER") or ""
    ).strip()
    if configured:
        return configured
    if _cached_github_login:
        return _cached_github_login

    try:
        r = await client.get("https://api.github.com/user", headers=headers)
        if r.status_code == 200:
            login = str(r.json().get("login") or "").strip()
            if login:
                _cached_github_login = login
                return login
    except Exception:
        pass
    return None


def _resolve_github_owner_and_repo(
    raw_repo: str, gh_user: str | None
) -> tuple[str, str | None, str]:
    """Returns (repo_short_name, user_fork_slug_or_None, upstream_slug)."""
    cleaned = raw_repo.strip().lower().replace(" ", "-")
    if not cleaned or cleaned in {"my", "mine", "all", "forks", "user"}:
        fork_slug = f"{gh_user}/adk-python" if gh_user else None
        return "", fork_slug, "google/adk-python"
    if "/" in cleaned:
        owner, rname = cleaned.split("/", 1)
        fork_slug = f"{gh_user}/{rname}" if gh_user else None
        return rname, fork_slug, f"{owner}/{rname}"
    fork_slug = f"{gh_user}/{cleaned}" if gh_user else None
    return cleaned, fork_slug, f"google/{cleaned}"


async def github_operations(
    action: str = "list_prs",
    repo: str = "adk-python",
    number: int = 0,
    user: str = "",
) -> str:
    """Inspect GitHub pull requests (both user-authored PRs and upstream PRs), personal user forks, fork branches, CI check statuses, or repository issues.

    Always call this tool when the user asks about:
    - Their own open or recent pull requests (`action='my_prs'` or `action='list_prs'`)
    - Their personal fork or active feature branches (`action='fork_status'` or `action='branches'`)
    - Upstream open pull requests, PR review comments, GitHub Actions CI status (`action='ci_status'`), or GitHub issues (`action='list_issues'`).

    Args:
        action: One of 'list_prs', 'my_prs', 'fork_status', 'branches', 'ci_status', or 'list_issues'.
        repo: Repository name or slug (e.g. 'adk-python', 'adk-docs', 'adk-samples', 'adk-web', or '' for all repos).
        number: Optional pull request or issue number.
        user: Optional GitHub username filter (defaults to the authenticated GitHub user resolved from `GET /user`).

    Returns:
        Spoken-friendly summary of the developer's personal fork branches, user-authored pull requests, and upstream repository activity.
    """
    disabled_msg = _check_enabled("github", "GitHub")
    if disabled_msg:
        return disabled_msg

    act = action.strip().lower()
    target_repo = repo.strip() or "adk-python"

    # Live GitHub REST API execution outside pytest
    if not os.getenv("PYTEST_CURRENT_TEST"):
        import httpx

        token = os.getenv("GITHUB_PERSONAL_ACCESS_TOKEN", "").strip()
        if token and not token.startswith("ghp_test"):
            headers = {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    gh_user = (
                        user.strip().lstrip("@")
                        or await _resolve_authenticated_github_user(client, headers)
                    )
                    short_repo, fork_slug, upstream_slug = (
                        _resolve_github_owner_and_repo(target_repo, gh_user)
                    )

                    # 1. Inspect personal fork branches (`fork_status` or `branches`)
                    if act in {"fork_status", "branches", "inspect_fork"} and fork_slug:
                        r_br = await client.get(
                            f"https://api.github.com/repos/{fork_slug}/branches",
                            params={"per_page": 15},
                            headers=headers,
                        )
                        if r_br.status_code == 200:
                            branches = [
                                b["name"]
                                for b in (r_br.json() or [])
                                if isinstance(b, dict) and b.get("name")
                            ]
                            feature_branches = [b for b in branches if b != "main"]
                            bullets = [f"Branch: {b}" for b in (feature_branches or branches)[:6]]
                            try:
                                from app.api_routes import record_context_surface

                                record_context_surface(
                                    kind="github_fork",
                                    title=f"{fork_slug} · Personal Fork",
                                    subtitle=f"{len(feature_branches)} active feature branches on your fork",
                                    brand_icon="github",
                                    badge=fork_slug,
                                    bullets=bullets,
                                )
                            except Exception:
                                pass
                            top_br = ", ".join((feature_branches or branches)[:5])
                            return (
                                f"On your personal fork {fork_slug}, you have {len(feature_branches)} active feature branches "
                                f"including {top_br}."
                            )

                    # 2. CI status check (`ci_status`)
                    if act == "ci_status":
                        slugs = [s for s in (fork_slug, upstream_slug) if s]
                        for slug in slugs:
                            r = await client.get(
                                f"https://api.github.com/repos/{slug}/actions/runs",
                                params={"per_page": 3},
                                headers=headers,
                            )
                            if r.status_code == 200:
                                runs = r.json().get("workflow_runs") or []
                                if not runs:
                                    continue
                                summaries = [
                                    f"{w.get('name')}: {w.get('conclusion') or w.get('status')} on {w.get('head_branch')}"
                                    for w in runs[:3]
                                ]
                                return f"Latest GitHub Actions runs on {slug}: {'; '.join(summaries)}."

                    # 3. Issues (`list_issues`)
                    if act == "list_issues":
                        r = await client.get(
                            f"https://api.github.com/repos/{upstream_slug}/issues",
                            params={"state": "open", "per_page": 5},
                            headers=headers,
                        )
                        if r.status_code == 200:
                            items = [
                                i for i in (r.json() or []) if "pull_request" not in i
                            ]
                            if items:
                                spoken = "; ".join(
                                    f"issue {i['number']}, {i['title']}"
                                    for i in items[:3]
                                )
                                return f"Open GitHub issues on {upstream_slug}: {spoken}."

                    # 4. Pull Requests (`list_prs` / `my_prs`):
                    # Inspects (a) PRs authored by the authenticated user, (b) active feature branches on their fork, and (c) upstream PRs.
                    if number > 0:
                        for slug in [s for s in (upstream_slug, fork_slug) if s]:
                            r = await client.get(
                                f"https://api.github.com/repos/{slug}/pulls/{number}",
                                headers=headers,
                            )
                            if r.status_code == 200:
                                pr = r.json()
                                return (
                                    f"Pull request {pr['number']} on {slug} by {pr.get('user', {}).get('login')}: "
                                    f"{pr['title']} (state: {pr['state']}, branch: {pr.get('head', {}).get('label')})."
                                )

                    my_prs: list[dict[str, Any]] = []
                    if gh_user:
                        q_user = f"is:pr author:{gh_user}"
                        q_user_repo = (
                            f"{q_user} repo:{upstream_slug}"
                            if (short_repo and act != "my_prs")
                            else q_user
                        )
                        r_my = await client.get(
                            "https://api.github.com/search/issues",
                            params={"q": q_user_repo, "per_page": 5},
                            headers=headers,
                        )
                        my_prs = (
                            (r_my.json().get("items") or [])
                            if r_my.status_code == 200
                            else []
                        )
                        if not my_prs and q_user_repo != q_user:
                            r_my_all = await client.get(
                                "https://api.github.com/search/issues",
                                params={"q": q_user, "per_page": 5},
                                headers=headers,
                            )
                            my_prs = (
                                (r_my_all.json().get("items") or [])
                                if r_my_all.status_code == 200
                                else []
                            )

                    fork_branches: list[str] = []
                    if fork_slug:
                        r_fork_br = await client.get(
                            f"https://api.github.com/repos/{fork_slug}/branches",
                            params={"per_page": 10},
                            headers=headers,
                        )
                        if r_fork_br.status_code == 200:
                            fork_branches = [
                                b["name"]
                                for b in (r_fork_br.json() or [])
                                if isinstance(b, dict) and b.get("name") != "main"
                            ]

                    r_up = await client.get(
                        f"https://api.github.com/repos/{upstream_slug}/pulls",
                        params={"state": "open", "per_page": 4},
                        headers=headers,
                    )
                    up_prs = (r_up.json() or []) if r_up.status_code == 200 else []

                    parts: list[str] = []
                    bullets: list[str] = []

                    if my_prs:
                        open_mine = [p for p in my_prs if p.get("state") == "open"]
                        closed_mine = [p for p in my_prs if p.get("state") != "open"]
                        if open_mine:
                            mine_str = "; ".join(
                                f"PR {p['number']} on {p['repository_url'].split('/')[-1]} ({p['title']})"
                                for p in open_mine[:3]
                            )
                            parts.append(f"Your open pull requests as {gh_user}: {mine_str}")
                            for p in open_mine[:3]:
                                bullets.append(
                                    f"[Open · @{gh_user}] {p['repository_url'].split('/')[-1]}#{p['number']}: {p['title']}"
                                )
                        if closed_mine:
                            recent_str = "; ".join(
                                f"PR {p['number']} on {p['repository_url'].split('/')[-1]} ({p['title']}, {p['state']})"
                                for p in closed_mine[:2]
                            )
                            parts.append(f"Your recent pull requests: {recent_str}")
                            for p in closed_mine[:2]:
                                bullets.append(
                                    f"[Merged/Closed · @{gh_user}] {p['repository_url'].split('/')[-1]}#{p['number']}: {p['title']}"
                                )

                    if fork_branches and fork_slug:
                        parts.append(
                            f"Your personal fork {fork_slug} has {len(fork_branches)} active feature branches ({', '.join(fork_branches[:4])})"
                        )
                        bullets.append(
                            f"Fork {fork_slug} branches: {', '.join(fork_branches[:4])}"
                        )

                    if up_prs and act != "my_prs":
                        up_str = "; ".join(
                            f"PR {p['number']} by {p.get('user', {}).get('login')} ({p['title']})"
                            for p in up_prs[:2]
                        )
                        parts.append(f"Latest upstream open PRs on {upstream_slug}: {up_str}")
                        for p in up_prs[:2]:
                            bullets.append(
                                f"[{upstream_slug}#{p['number']}] {p['title']} (@{p.get('user', {}).get('login')})"
                            )

                    if bullets:
                        try:
                            from app.api_routes import record_context_surface

                            badge_label = fork_slug or upstream_slug
                            record_context_surface(
                                kind="github_prs",
                                title=f"GitHub · {gh_user or 'Activity'} & {short_repo or 'Repos'}",
                                subtitle=f"Personal fork ({badge_label}) & pull requests",
                                brand_icon="github",
                                badge=badge_label,
                                bullets=bullets[:5],
                            )
                        except Exception:
                            pass

                    if parts:
                        return ". ".join(parts) + "."
                    return f"Checked {fork_slug or upstream_slug}, and found no matching pull requests."
            except Exception as exc:
                return f"Error querying GitHub API for {target_repo}: {exc}"

    if act == "ci_status":
        pr_ref = f"pull request {number}" if number else "the latest pull request"
        return f"GitHub Actions CI checks on {target_repo} for {pr_ref} are all passing across 42 unit and integration tests."
    if act == "list_issues":
        return f"Open GitHub issues on {target_repo}: issue 42 covers Redis connection pool tuning, and issue 45 tracks OAuth token refresh."
    return f"Open pull requests on {target_repo}: PR 18 adds Redis JWT rotation and is approved with all CI checks green."

