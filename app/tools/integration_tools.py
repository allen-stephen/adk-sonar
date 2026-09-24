"""Downstream integration tools for the Live Voice Orchestrator (Spotify, Calendar, Gmail, Drive, Slack, GitHub).

Each tool checks `IntegrationRegistry` state at runtime and executes against live REST APIs
using the active credentials configured in `IntegrationAuthManager`.
"""

from __future__ import annotations

import base64
import os
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.app_utils.http_client import get_http_client
from app.auth import ensure_fresh_access_token, get_workspace_headers
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
    token = await ensure_fresh_access_token("spotify")
    if not token:
        return (
            "Spotify is not authenticated yet. "
            "Connect your Spotify account in the Configuration sheet or run make onboard to control playback."
        )

    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with get_http_client(timeout=6.0) as client:
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
                    album = (item.get("album") or {}).get("name") or ""
                    images = (item.get("album") or {}).get("images") or []
                    album_art = images[0].get("url") if images else None
                    ext_url = (item.get("external_urls") or {}).get("spotify")
                    device_name = (data.get("device") or {}).get("name") or "Spotify"
                    is_playing = data.get("is_playing", False)
                    if track_name:
                        state_word = "Currently playing" if is_playing else "Paused on"
                        try:
                            from app.api_routes import record_context_surface

                            record_context_surface(
                                kind="spotify",
                                title=f"Spotify · {'Now Playing' if is_playing else 'Paused'}",
                                subtitle=f"{track_name} — {artists}",
                                brand_icon="spotify",
                                badge=device_name,
                                bullets=[f"{track_name} by {artists}" + (f" ({album})" if album else "")],
                                meta={
                                    "track": track_name,
                                    "artist": artists,
                                    "album": album,
                                    "album_art_url": album_art,
                                    "is_playing": is_playing,
                                    "device_name": device_name,
                                    "external_url": ext_url,
                                    "status_label": "Now Playing" if is_playing else "Paused",
                                },
                                surface_id="a2ui-ctx-spotify",
                            )
                        except Exception:
                            pass
                        return f"{state_word} {track_name} by {artists} on Spotify."
                    try:
                        from app.api_routes import record_context_surface

                        record_context_surface(
                            kind="spotify",
                            title="Spotify · Idle",
                            subtitle="Connected — no active track loaded",
                            brand_icon="spotify",
                            badge="Idle",
                            bullets=["Open Spotify on your phone or laptop to start playback."],
                            meta={"status_label": "Idle · No Active Track", "empty_state": True},
                            surface_id="a2ui-ctx-spotify",
                        )
                    except Exception:
                        pass
                    return "Connected to Spotify, but no active track is loaded."
                return f"Spotify API error ({r.status_code}): {r.text.strip()}"

            if act in {"pause", "stop"}:
                r = await client.put(
                    "https://api.spotify.com/v1/me/player/pause",
                    headers=headers,
                )
                if r.status_code in {200, 204}:
                    try:
                        from app.api_routes import record_context_surface

                        record_context_surface(
                            kind="spotify",
                            title="Spotify · Playback Paused",
                            subtitle="Paused on active device",
                            brand_icon="spotify",
                            badge="Paused",
                            bullets=["Spotify playback paused on your active device."],
                            meta={"status_label": "Playback Paused", "is_playing": False},
                            surface_id="a2ui-ctx-spotify",
                        )
                    except Exception:
                        pass
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
                    artists = ", ".join(
                        a.get("name", "") for a in (tracks[0].get("artists") or [])
                    )
                    album = (tracks[0].get("album") or {}).get("name") or ""
                    images = (tracks[0].get("album") or {}).get("images") or []
                    album_art = images[0].get("url") if images else None
                    ext_url = (tracks[0].get("external_urls") or {}).get("spotify")
                    qr = await client.post(
                        "https://api.spotify.com/v1/me/player/queue",
                        params={"uri": uri},
                        headers=headers,
                    )
                    if qr.status_code in {200, 204}:
                        try:
                            from app.api_routes import record_context_surface

                            record_context_surface(
                                kind="spotify",
                                title="Spotify · Added to Queue",
                                subtitle=f"{tname} — {artists}",
                                brand_icon="spotify",
                                badge="Queued",
                                bullets=[f"Queued {tname} by {artists}"],
                                meta={
                                    "track": tname,
                                    "artist": artists,
                                    "album": album,
                                    "album_art_url": album_art,
                                    "is_playing": True,
                                    "external_url": ext_url,
                                    "status_label": "Added to Queue",
                                },
                                surface_id="a2ui-ctx-spotify",
                            )
                        except Exception:
                            pass
                        return f"Added {tname} to your Spotify playback queue."
                    return f"Spotify queue failed ({qr.status_code}): {qr.text.strip()}"
                if tracks:
                    uri = tracks[0]["uri"]
                    tname = tracks[0]["name"]
                    artists = ", ".join(
                        a.get("name", "") for a in (tracks[0].get("artists") or [])
                    )
                    album = (tracks[0].get("album") or {}).get("name") or ""
                    images = (tracks[0].get("album") or {}).get("images") or []
                    album_art = images[0].get("url") if images else None
                    ext_url = (tracks[0].get("external_urls") or {}).get("spotify")
                    pr = await client.put(
                        "https://api.spotify.com/v1/me/player/play",
                        json={"uris": [uri]},
                        headers=headers,
                    )
                    is_ok = pr.status_code in {200, 204}
                    try:
                        from app.api_routes import record_context_surface

                        record_context_surface(
                            kind="spotify",
                            title=f"Spotify · {'Now Playing' if is_ok else 'Track Matched'}",
                            subtitle=f"{tname} — {artists}",
                            brand_icon="spotify",
                            badge="Playing" if is_ok else "Matched",
                            bullets=[f"{tname} by {artists}" + (f" ({album})" if album else "")],
                            meta={
                                "track": tname,
                                "artist": artists,
                                "album": album,
                                "album_art_url": album_art,
                                "is_playing": is_ok,
                                "external_url": ext_url,
                                "status_label": "Now Playing" if is_ok else "Open Spotify Device to Play",
                            },
                            surface_id="a2ui-ctx-spotify",
                        )
                    except Exception:
                        pass
                    if is_ok:
                        return f"Now playing {tname} by {artists} on Spotify."
                    return f"Found {tname} by {artists}, but Spotify could not start playback ({pr.status_code}): {pr.text.strip()}"
                if playlists:
                    uri = playlists[0]["uri"]
                    pname = playlists[0]["name"]
                    images = playlists[0].get("images") or []
                    album_art = images[0].get("url") if images else None
                    ext_url = (playlists[0].get("external_urls") or {}).get("spotify")
                    pr = await client.put(
                        "https://api.spotify.com/v1/me/player/play",
                        json={"context_uri": uri},
                        headers=headers,
                    )
                    is_ok = pr.status_code in {200, 204}
                    try:
                        from app.api_routes import record_context_surface

                        record_context_surface(
                            kind="spotify",
                            title=f"Spotify · {'Playlist Playing' if is_ok else 'Playlist Matched'}",
                            subtitle=pname,
                            brand_icon="spotify",
                            badge="Playlist",
                            bullets=[f"Playlist: {pname}"],
                            meta={
                                "track": pname,
                                "artist": "Spotify Playlist",
                                "album_art_url": album_art,
                                "is_playing": is_ok,
                                "external_url": ext_url,
                                "status_label": "Playing Playlist" if is_ok else "Open Spotify Device to Play",
                            },
                            surface_id="a2ui-ctx-spotify",
                        )
                    except Exception:
                        pass
                    if is_ok:
                        return f"Now playing playlist {pname} on Spotify."
                    return f"Found playlist {pname}, but Spotify could not start playback ({pr.status_code}): {pr.text.strip()}"
                return f"No matching tracks or playlists found on Spotify for {query.strip()}."
            return f"Unsupported Spotify action '{action}'."
    except Exception as exc:
        return f"Error communicating with Spotify API: {exc}"


_WEEKDAY_MAP = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


def _resolve_calendar_time_bounds(
    time_window: str,
    now_local: datetime,
    *,
    has_search_query: bool = False,
) -> tuple[datetime, datetime, str]:
    """Resolve (timeMin, timeMax, window_label) anchored to `now_local` in the user's selected timezone."""
    from datetime import timedelta

    tw = (time_window or "today").strip().lower()
    user_tz = now_local.tzinfo
    end_of_today = now_local.replace(hour=23, minute=59, second=59, microsecond=999999)

    if tw in {"today", "rest of today", "now", "tonight", "this afternoon", "this evening"}:
        if has_search_query and tw == "today":
            # When searching for a specific named meeting with the default 'today' arg, look ahead 14 days
            return now_local, now_local + timedelta(days=14), "the next 2 weeks"
        return now_local, end_of_today, "the rest of today"

    if tw == "this morning":
        morning_end = now_local.replace(hour=12, minute=0, second=0, microsecond=0)
        return now_local, (morning_end if now_local < morning_end else end_of_today), "this morning"

    if "tomorrow" in tw:
        tmr = (now_local + timedelta(days=1)).date()
        t_min = datetime(tmr.year, tmr.month, tmr.day, 0, 0, 0, tzinfo=user_tz)
        t_max = datetime(tmr.year, tmr.month, tmr.day, 23, 59, 59, tzinfo=user_tz)
        return t_min, t_max, "tomorrow"

    if tw in {"this week", "this_week", "week"}:
        days_left = max(6 - now_local.weekday(), 1)
        end_d = (now_local + timedelta(days=days_left)).date()
        t_max = datetime(end_d.year, end_d.month, end_d.day, 23, 59, 59, tzinfo=user_tz)
        return now_local, t_max, "this week"

    if tw in {"next week", "next_week"}:
        days_to_mon = (7 - now_local.weekday()) % 7 or 7
        next_mon = (now_local + timedelta(days=days_to_mon)).date()
        next_sun = next_mon + timedelta(days=6)
        t_min = datetime(next_mon.year, next_mon.month, next_mon.day, 0, 0, 0, tzinfo=user_tz)
        t_max = datetime(next_sun.year, next_sun.month, next_sun.day, 23, 59, 59, tzinfo=user_tz)
        return t_min, t_max, "next week"

    for day_name, weekday_idx in _WEEKDAY_MAP.items():
        if day_name in tw:
            delta_days = (weekday_idx - now_local.weekday()) % 7
            if delta_days == 0 and "next" in tw:
                delta_days = 7
            target_d = (now_local + timedelta(days=delta_days)).date()
            t_min = (
                now_local
                if delta_days == 0
                else datetime(target_d.year, target_d.month, target_d.day, 0, 0, 0, tzinfo=user_tz)
            )
            t_max = datetime(target_d.year, target_d.month, target_d.day, 23, 59, 59, tzinfo=user_tz)
            return t_min, t_max, day_name.capitalize()

    return now_local, now_local + timedelta(days=7), time_window or "the upcoming week"


def _self_rsvp_status(ev: dict[str, Any]) -> str:
    """Return the authenticated user's RSVP status ('accepted', 'declined', 'tentative', 'needsAction')."""
    attendees = ev.get("attendees") or []
    for att in attendees:
        if isinstance(att, dict) and att.get("self") is True:
            return str(att.get("responseStatus") or "accepted")
    return "accepted"


def _filter_and_rank_calendar_events(
    items: list[dict[str, Any]],
    *,
    now_local: datetime,
    query: str = "",
) -> list[dict[str, Any]]:
    """Filter out working locations, tasks/free reminders, declined invites, and already-ended events."""
    q_lower = (query or "").strip().lower()
    wants_pending = any(w in q_lower for w in ("pending", "invite", "tentative", "unresponded", "rsvp"))
    wants_all_day = any(w in q_lower for w in ("all day", "all-day", "holiday", "ooo", "out of office", "task", "reminder"))
    user_tz = now_local.tzinfo

    confirmed: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []

    for ev in items:
        if not isinstance(ev, dict):
            continue
        if ev.get("status") == "cancelled":
            continue
        ev_type = str(ev.get("eventType") or "default")
        if ev_type in {"workingLocation", "birthday", "focusTime", "fromGmail"} and not wants_all_day:
            continue

        # Exclude "Show as: Free" (transparent) reminder/task blocks unless specifically queried
        if str(ev.get("transparency") or "opaque").lower() == "transparent" and not wants_all_day:
            continue

        rsvp = _self_rsvp_status(ev)
        if rsvp == "declined" and "declined" not in q_lower:
            continue

        start_obj = ev.get("start") or {}
        end_obj = ev.get("end") or {}
        raw_dt = start_obj.get("dateTime")
        raw_end_dt = end_obj.get("dateTime")

        # Skip all-day banners/tasks unless explicitly asked for
        if not raw_dt and not wants_all_day:
            continue

        # Drop timed events that have already ended relative to the user's current local clock
        if raw_end_dt:
            try:
                end_dt = datetime.fromisoformat(raw_end_dt.replace("Z", "+00:00")).astimezone(user_tz)
                if end_dt <= now_local:
                    continue
            except Exception:
                pass

        if rsvp in {"needsAction", "tentative"}:
            if wants_pending:
                confirmed.append(ev)
            else:
                pending.append(ev)
        else:
            confirmed.append(ev)

    if confirmed:
        return confirmed
    # Only fall back to pending invites if there are zero confirmed meetings (or user asked for invites)
    return pending


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

    token = await ensure_fresh_access_token("google_workspace")
    if not token:
        return (
            "Google Workspace is not authenticated yet. "
            "Toggle Google Workspace on in the Configuration sheet or run make onboard to connect your Google Calendar."
        )

    act = action.strip().lower()
    prefs = get_runtime_preferences()
    user_tz = ZoneInfo(prefs["timezone"])
    headers = get_workspace_headers(token)
    now_local = datetime.now(user_tz)
    now_clock_str = now_local.strftime(f"%-I:%M %p {prefs['tz_abbrev']}")

    try:
        async with get_http_client(timeout=8.0) as client:
            if act == "create":
                title = query.strip() or "Meeting"
                r = await client.post(
                    "https://www.googleapis.com/calendar/v3/calendars/primary/events/quickAdd",
                    params={"text": f"{title} {time_window}".strip()},
                    headers=headers,
                )
                if r.status_code in {200, 201}:
                    ev_json = r.json() or {}
                    created_summary = ev_json.get("summary") or title
                    try:
                        from app.api_routes import record_context_surface

                        record_context_surface(
                            kind="google_calendar",
                            title="Google Calendar · Scheduled",
                            subtitle=created_summary,
                            brand_icon="google_calendar",
                            badge=prefs["tz_abbrev"],
                            bullets=[f"{created_summary} · {time_window}"],
                            items=[
                                {
                                    "title": created_summary,
                                    "start_time": time_window,
                                    "status": "Confirmed",
                                    "meet_url": ev_json.get("hangoutLink") or ev_json.get("htmlLink"),
                                }
                            ],
                            meta={"status_label": "Event Scheduled", "time_window": time_window},
                            surface_id="a2ui-ctx-calendar",
                        )
                    except Exception:
                        pass
                    return f"Scheduled {created_summary} on your Google Calendar for {time_window}."
                if r.status_code in {401, 403}:
                    return (
                        "Your Google token does not have Google Calendar permission yet. "
                        "Click Google Workspace in the Configuration sheet or run make onboard to grant Calendar, Gmail, and Drive scopes."
                    )
                return f"Google Calendar API returned {r.status_code}: {r.text[:200]}"

            time_min_dt, time_max_dt, window_label = _resolve_calendar_time_bounds(
                time_window,
                now_local,
                has_search_query=bool(query.strip() and act != "check_availability"),
            )
            params: dict[str, Any] = {
                "timeMin": time_min_dt.isoformat(),
                "timeMax": time_max_dt.isoformat(),
                "timeZone": prefs["timezone"],
                "maxResults": 25,
                "singleEvents": "true",
                "orderBy": "startTime",
                "eventTypes": "default",
            }
            if query.strip() and act != "check_availability":
                params["q"] = query.strip()
            r = await client.get(
                "https://www.googleapis.com/calendar/v3/calendars/primary/events",
                params=params,
                headers=headers,
            )
            if r.status_code == 200:
                raw_items = r.json().get("items") or []
                items = _filter_and_rank_calendar_events(
                    raw_items,
                    now_local=now_local,
                    query=query,
                )
                if not items:
                    try:
                        from app.api_routes import record_context_surface

                        record_context_surface(
                            kind="google_calendar",
                            title=f"Google Calendar · {prefs['tz_abbrev']}",
                            subtitle=f"Schedule clear for {window_label}",
                            brand_icon="google_calendar",
                            badge="Clear",
                            bullets=[
                                f"As of {now_clock_str}, no confirmed meetings for {window_label} ({prefs['timezone']})."
                            ],
                            meta={
                                "empty_state": True,
                                "status_label": "Schedule Clear",
                                "time_window": window_label,
                                "tz_abbrev": prefs["tz_abbrev"],
                            },
                            surface_id="a2ui-ctx-calendar",
                        )
                    except Exception:
                        pass
                    return (
                        f"As of {now_clock_str}, you have no confirmed upcoming meetings on your Google Calendar "
                        f"for {window_label} ({prefs['timezone']})."
                    )
                summaries = []
                bullets = []
                cal_items = []
                for ev in items[:5]:
                    title = ev.get("summary") or "Untitled event"
                    rsvp = _self_rsvp_status(ev)
                    rsvp_tag = " [Pending invite]" if rsvp in {"needsAction", "tentative"} else ""
                    raw_dt = (ev.get("start") or {}).get("dateTime")
                    raw_end_dt = (ev.get("end") or {}).get("dateTime")
                    raw_date = (ev.get("start") or {}).get("date")
                    start_str = raw_dt or raw_date or ""
                    start_short = raw_date or "All day"
                    end_short = ""
                    date_label = ""
                    relative_hint = ""
                    if raw_dt:
                        try:
                            dt_obj = datetime.fromisoformat(raw_dt.replace("Z", "+00:00")).astimezone(user_tz)
                            start_str = dt_obj.strftime(f"%a %b %-d at %-I:%M %p {prefs['tz_abbrev']}")
                            start_short = dt_obj.strftime("%-I:%M %p")
                            date_label = dt_obj.strftime("%a, %b %-d")
                            mins_away = int((dt_obj - now_local).total_seconds() // 60)
                            if mins_away <= 0:
                                relative_hint = "in progress now, "
                            elif mins_away <= 90:
                                relative_hint = f"in {mins_away} min, "
                        except Exception:
                            pass
                    if raw_end_dt:
                        try:
                            end_obj = datetime.fromisoformat(raw_end_dt.replace("Z", "+00:00")).astimezone(user_tz)
                            end_short = end_obj.strftime("%-I:%M %p")
                        except Exception:
                            pass
                    summaries.append(f"{title}{rsvp_tag} ({relative_hint}{start_str})")
                    bullets.append(f"{title}{rsvp_tag} · {start_str}")
                    cal_items.append(
                        {
                            "title": f"{title}{rsvp_tag}",
                            "start_time": start_short,
                            "end_time": end_short,
                            "date_label": date_label,
                            "location": ev.get("location"),
                            "meet_url": ev.get("hangoutLink") or ev_json.get("htmlLink") if "ev_json" in locals() else ev.get("htmlLink"),
                            "attendees_count": len(ev.get("attendees") or []),
                        }
                    )
                try:
                    from app.api_routes import record_context_surface

                    record_context_surface(
                        kind="google_calendar",
                        title=f"Google Calendar · {prefs['tz_abbrev']}",
                        subtitle=f"{len(items)} upcoming {'meeting' if len(items) == 1 else 'meetings'} ({window_label})",
                        brand_icon="google_calendar",
                        badge=prefs["tz_abbrev"],
                        bullets=bullets,
                        items=cal_items,
                        meta={"tz_abbrev": prefs["tz_abbrev"], "timezone": prefs["timezone"]},
                        surface_id="a2ui-ctx-calendar",
                    )
                except Exception:
                    pass
                return (
                    f"As of {now_clock_str} ({prefs['timezone']}), upcoming on your Google Calendar for {window_label}: "
                    f"{'; '.join(summaries)}."
                )
            if r.status_code in {401, 403}:
                return (
                    "Your Google token does not have Google Calendar permission yet. "
                    "Click Google Workspace in the Configuration sheet or run make onboard to grant Calendar, Gmail, and Drive scopes."
                )
            return f"Google Calendar API returned {r.status_code}: {r.text[:200]}"
    except Exception as exc:
        return f"Error querying Google Calendar API: {exc}"


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

    token = await ensure_fresh_access_token("google_workspace")
    if not token:
        return (
            "Google Workspace is not authenticated yet. "
            "Toggle Google Workspace on in the Configuration sheet or run make onboard to connect your Gmail inbox."
        )

    act = action.strip().lower()
    headers = get_workspace_headers(token)
    try:
        async with get_http_client(timeout=8.0) as client:
            if act == "draft_reply":
                to_str = recipient.strip() or "recipient@example.com"
                subject_str = query.strip() or "Follow-up"
                raw_mime = f"To: {to_str}\r\nSubject: {subject_str}\r\n\r\n{subject_str}"
                encoded = base64.urlsafe_b64encode(raw_mime.encode("utf-8")).decode("ascii")
                dr = await client.post(
                    "https://gmail.googleapis.com/gmail/v1/users/me/drafts",
                    json={"message": {"raw": encoded}},
                    headers=headers,
                )
                if dr.status_code in {200, 201}:
                    try:
                        from app.api_routes import record_context_surface

                        record_context_surface(
                            kind="gmail",
                            title="Gmail · Draft Saved",
                            subtitle=f"To: {to_str}",
                            brand_icon="gmail",
                            badge="Draft",
                            bullets=[f"To: {to_str}", f"Subject: {subject_str}"],
                            meta={
                                "is_draft": True,
                                "recipient": to_str,
                                "subject": subject_str,
                                "body": subject_str,
                                "status_label": "Draft Saved",
                            },
                            surface_id="a2ui-ctx-gmail",
                        )
                    except Exception:
                        pass
                    return f"Created a Gmail draft to {to_str} regarding {subject_str}."
                return f"Gmail draft creation returned {dr.status_code}: {dr.text[:200]}"

            r = await client.get(
                "https://gmail.googleapis.com/gmail/v1/users/me/messages",
                params={"q": query or "is:unread", "maxResults": 4},
                headers=headers,
            )
            if r.status_code == 200:
                msgs = r.json().get("messages") or []
                if not msgs:
                    try:
                        from app.api_routes import record_context_surface

                        record_context_surface(
                            kind="gmail",
                            title=f"Gmail · {query or 'Inbox'}",
                            subtitle="No matching threads found",
                            brand_icon="gmail",
                            badge="Inbox Clear",
                            bullets=[f"No Gmail messages matching '{query}'."],
                            meta={"empty_state": True, "status_label": "No Matching Messages", "query": query},
                            surface_id="a2ui-ctx-gmail",
                        )
                    except Exception:
                        pass
                    return f"No Gmail messages found matching '{query}'."
                snippets = []
                mail_items = []
                for m in msgs[:4]:
                    mr = await client.get(
                        f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{m['id']}",
                        params={"format": "metadata", "metadataHeaders": ["Subject", "From", "Date"]},
                        headers=headers,
                    )
                    if mr.status_code == 200:
                        mdata = mr.json()
                        hdrs = {
                            h["name"]: h["value"]
                            for h in (mdata.get("payload", {}).get("headers") or [])
                        }
                        subj_val = hdrs.get("Subject", "No Subject")
                        from_raw = hdrs.get("From", "Unknown")
                        from_clean = from_raw.split("<")[0].strip().strip('"') or from_raw
                        date_raw = hdrs.get("Date", "")
                        snippets.append(f"{subj_val} from {from_raw}")
                        mail_items.append(
                            {
                                "id": m["id"],
                                "sender": from_clean,
                                "from_raw": from_raw,
                                "subject": subj_val,
                                "snippet": str(mdata.get("snippet") or "").strip(),
                                "date": date_raw[:16] if date_raw else "",
                                "unread": "UNREAD" in (mdata.get("labelIds") or []),
                            }
                        )
                try:
                    from app.api_routes import record_context_surface

                    record_context_surface(
                        kind="gmail",
                        title=f"Gmail · {query or 'Inbox'}",
                        subtitle=f"Top {len(snippets)} matching {'message' if len(snippets) == 1 else 'messages'}",
                        brand_icon="gmail",
                        badge="Gmail",
                        bullets=snippets,
                        items=mail_items,
                        meta={"query": query},
                        surface_id="a2ui-ctx-gmail",
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

    token = await ensure_fresh_access_token("google_workspace")
    if not token:
        return (
            "Google Workspace is not authenticated yet. "
            "Toggle Google Workspace on in the Configuration sheet or run make onboard to connect your Google Drive."
        )

    headers = get_workspace_headers(token)
    try:
        async with get_http_client(timeout=8.0) as client:
            q_param = (
                f"name contains '{query.strip()}' and trashed = false"
                if query.strip()
                else "trashed = false"
            )
            r = await client.get(
                "https://www.googleapis.com/drive/v3/files",
                params={
                    "q": q_param,
                    "pageSize": 5,
                    "fields": "files(id,name,mimeType,modifiedTime,description,webViewLink)",
                },
                headers=headers,
            )
            if r.status_code == 200:
                files = r.json().get("files") or []
                if not files:
                    try:
                        from app.api_routes import record_context_surface

                        record_context_surface(
                            kind="google_drive",
                            title=f"Google Drive · {query or 'Files'}",
                            subtitle="No matching files found",
                            brand_icon="google_drive",
                            badge="Drive",
                            bullets=[f"No Google Drive files found matching '{query}'."],
                            meta={"empty_state": True, "status_label": "No Matching Files"},
                            surface_id="a2ui-ctx-drive",
                        )
                    except Exception:
                        pass
                    return f"No Google Drive files found matching '{query}'."
                names = [f["name"] for f in files[:5]]
                descriptions = [
                    str(f.get("description") or "").strip()
                    for f in files[:2]
                    if f.get("description")
                ]
                drive_items = []
                for f in files[:5]:
                    mime = str(f.get("mimeType") or "")
                    if "spreadsheet" in mime:
                        kind_label = "Sheet"
                    elif "presentation" in mime:
                        kind_label = "Slides"
                    elif "document" in mime:
                        kind_label = "Doc"
                    elif "pdf" in mime:
                        kind_label = "PDF"
                    elif "folder" in mime:
                        kind_label = "Folder"
                    else:
                        kind_label = "File"
                    fid = f.get("id")
                    drive_items.append(
                        {
                            "id": fid,
                            "name": f.get("name"),
                            "mime_type": mime,
                            "kind_label": kind_label,
                            "modified_time": str(f.get("modifiedTime") or "")[:10],
                            "description": str(f.get("description") or "").strip(),
                            "url": f.get("webViewLink") or (f"https://drive.google.com/file/d/{fid}/view" if fid else None),
                        }
                    )
                try:
                    from app.api_routes import record_context_surface

                    record_context_surface(
                        kind="google_drive",
                        title=f"Google Drive · {query or 'Recent Files'}",
                        subtitle=f"Found {len(names)} {'file' if len(names) == 1 else 'files'} in Google Drive",
                        brand_icon="google_drive",
                        badge="Drive",
                        bullets=names,
                        items=drive_items,
                        surface_id="a2ui-ctx-drive",
                    )
                except Exception:
                    pass
                summary = f"Found Google Drive files: {', '.join(names)}."
                if descriptions:
                    summary += f" {' '.join(descriptions)}"
                return summary
            if r.status_code in {401, 403}:
                return (
                    "Your Google token does not have Google Drive permission yet. "
                    "Click Google Workspace in the Configuration sheet or run make onboard to grant Calendar, Gmail, and Drive scopes."
                )
            return f"Google Drive API returned {r.status_code}: {r.text[:200]}"
    except Exception as exc:
        return f"Error querying Google Drive API: {exc}"


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
    token = os.getenv("SLACK_BOT_TOKEN", "").strip()
    if not token:
        return "Slack is enabled, but SLACK_BOT_TOKEN is not configured yet. Connect Slack in the Configuration sheet."

    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with get_http_client(timeout=8.0) as client:
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
            if action.strip().lower() == "post":
                pr = await client.post(
                    "https://slack.com/api/chat.postMessage",
                    json={"channel": cid, "text": message.strip() or "Status update"},
                    headers=headers,
                )
                if pr.json().get("ok"):
                    try:
                        from app.api_routes import record_context_surface

                        record_context_surface(
                            kind="slack",
                            title=f"Slack · #{cname}",
                            subtitle="Message posted",
                            brand_icon="slack",
                            badge=f"#{cname}",
                            bullets=[message.strip() or "Status update"],
                            meta={"status_label": f"Posted to #{cname}", "channel": f"#{cname}"},
                            surface_id="a2ui-ctx-slack",
                        )
                    except Exception:
                        pass
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
            try:
                from app.api_routes import record_context_surface

                record_context_surface(
                    kind="slack",
                    title=f"Slack · #{cname}",
                    subtitle=f"Latest {len(msgs)} messages in #{cname}",
                    brand_icon="slack",
                    badge=f"#{cname}",
                    bullets=msgs[:3],
                    items=[{"channel": f"#{cname}", "text": t} for t in msgs[:3]],
                    surface_id="a2ui-ctx-slack",
                )
            except Exception:
                pass
            return f"Latest in Slack channel {cname}: {' | '.join(msgs[:2])}"
    except Exception as exc:
        return f"Error communicating with Slack API: {exc}"


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


def _extract_github_slug_from_git_url(git_url: str) -> str | None:
    """Extract 'owner/repo' from a GitHub HTTPS or SSH URL."""
    url = git_url.strip().removesuffix(".git").rstrip("/")
    for prefix in ("https://github.com/", "http://github.com/", "git@github.com:"):
        if url.startswith(prefix):
            slug = url[len(prefix) :].strip("/")
            if slug.count("/") == 1:
                return slug.lower()
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
    try:
        from app.workers.harnesses.base import load_workspaces_manifest

        manifest = load_workspaces_manifest()
        for entry in manifest.get("repositories", []):
            if isinstance(entry, dict) and str(entry.get("name") or "").strip().lower() == cleaned:
                slug = _extract_github_slug_from_git_url(str(entry.get("git_url") or ""))
                if slug:
                    return cleaned, fork_slug, slug
    except Exception:
        pass
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
    token = (
        await ensure_fresh_access_token("github")
        or os.getenv("GITHUB_PERSONAL_ACCESS_TOKEN", "").strip()
        or os.getenv("GH_TOKEN", "").strip()
    )
    if not token:
        return (
            "GitHub is not authenticated yet. "
            "Configure GITHUB_PERSONAL_ACCESS_TOKEN in the Configuration sheet or run make onboard to inspect pull requests and CI runs."
        )

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        async with get_http_client(timeout=10.0) as client:
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
                    branch_items = [
                        {
                            "kind": "branch",
                            "title": b,
                            "repo": fork_slug,
                            "state": "active",
                            "url": f"https://github.com/{fork_slug}/tree/{b}",
                        }
                        for b in (feature_branches or branches)[:5]
                    ]
                    try:
                        from app.api_routes import record_context_surface

                        record_context_surface(
                            kind="github_fork",
                            title=f"{fork_slug} · Personal Fork",
                            subtitle=f"{len(feature_branches)} active feature branches on your fork",
                            brand_icon="github",
                            badge=fork_slug,
                            bullets=bullets,
                            items=branch_items,
                            meta={"fork": fork_slug, "branches": (feature_branches or branches)[:6]},
                            surface_id="a2ui-ctx-github",
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
                        ci_items = [
                            {
                                "kind": "ci",
                                "title": w.get("name") or "Workflow",
                                "repo": slug,
                                "state": w.get("conclusion") or w.get("status") or "queued",
                                "author": w.get("head_branch"),
                                "url": w.get("html_url"),
                            }
                            for w in runs[:3]
                        ]
                        try:
                            from app.api_routes import record_context_surface

                            record_context_surface(
                                kind="github_ci",
                                title=f"GitHub Actions · {slug}",
                                subtitle=f"Latest {len(ci_items)} CI workflow runs",
                                brand_icon="github",
                                badge=slug,
                                bullets=summaries,
                                items=ci_items,
                                surface_id="a2ui-ctx-github",
                            )
                        except Exception:
                            pass
                        return f"Latest GitHub Actions CI runs on {slug}: {'; '.join(summaries)}."
                return f"No recent GitHub Actions CI runs found on {upstream_slug}."

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
                        issue_items = [
                            {
                                "kind": "issue",
                                "number": i.get("number"),
                                "title": i.get("title"),
                                "repo": upstream_slug,
                                "state": i.get("state", "open"),
                                "author": (i.get("user") or {}).get("login"),
                                "comments": i.get("comments", 0),
                                "url": i.get("html_url"),
                            }
                            for i in items[:4]
                        ]
                        try:
                            from app.api_routes import record_context_surface

                            record_context_surface(
                                kind="github_issues",
                                title=f"GitHub Issues · {upstream_slug}",
                                subtitle=f"{len(issue_items)} open issues",
                                brand_icon="github",
                                badge=upstream_slug,
                                bullets=[f"#{i['number']}: {i['title']}" for i in items[:4]],
                                items=issue_items,
                                surface_id="a2ui-ctx-github",
                            )
                        except Exception:
                            pass
                        return f"Open GitHub issues on {upstream_slug}: {spoken}."
                return f"No open GitHub issues found on {upstream_slug}."

            # 4. Pull Requests (`list_prs` / `my_prs`):
            if number > 0:
                for slug in [s for s in (upstream_slug, fork_slug) if s]:
                    r = await client.get(
                        f"https://api.github.com/repos/{slug}/pulls/{number}",
                        headers=headers,
                    )
                    if r.status_code == 200:
                        pr = r.json()
                        author_login = (pr.get("user") or {}).get("login")
                        head_label = (pr.get("head") or {}).get("label") or ""
                        try:
                            from app.api_routes import record_context_surface

                            record_context_surface(
                                kind="github_prs",
                                title=f"GitHub PR #{pr['number']} · {slug}",
                                subtitle=pr.get("title") or f"Pull Request #{pr['number']}",
                                brand_icon="github",
                                badge=slug,
                                bullets=[
                                    f"#{pr['number']}: {pr['title']} (@{author_login})",
                                    f"State: {pr.get('state')} · Branch: {head_label}",
                                ],
                                items=[
                                    {
                                        "kind": "pr",
                                        "number": pr.get("number"),
                                        "title": pr.get("title"),
                                        "repo": slug,
                                        "state": "merged" if pr.get("merged") else pr.get("state", "open"),
                                        "author": author_login,
                                        "branch": head_label,
                                        "comments": pr.get("comments", 0),
                                        "url": pr.get("html_url"),
                                    }
                                ],
                                surface_id="a2ui-ctx-github",
                            )
                        except Exception:
                            pass
                        return (
                            f"Pull request {pr['number']} on {slug} by {author_login}: "
                            f"{pr['title']} (state: {pr['state']}, branch: {head_label})."
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
            if not up_prs and fork_slug and fork_slug != upstream_slug:
                r_fork_prs = await client.get(
                    f"https://api.github.com/repos/{fork_slug}/pulls",
                    params={"state": "open", "per_page": 4},
                    headers=headers,
                )
                if r_fork_prs.status_code == 200:
                    up_prs = r_fork_prs.json() or []

            parts: list[str] = []
            bullets: list[str] = []
            gh_items: list[dict[str, Any]] = []

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
                        r_short = p["repository_url"].split("/")[-1]
                        bullets.append(
                            f"[Open · @{gh_user}] {r_short}#{p['number']}: {p['title']}"
                        )
                        gh_items.append(
                            {
                                "kind": "pr",
                                "number": p.get("number"),
                                "title": p.get("title"),
                                "repo": r_short,
                                "state": "open",
                                "author": gh_user,
                                "comments": p.get("comments", 0),
                                "url": p.get("html_url"),
                            }
                        )
                if closed_mine:
                    recent_str = "; ".join(
                        f"PR {p['number']} on {p['repository_url'].split('/')[-1]} ({p['title']}, {p['state']})"
                        for p in closed_mine[:2]
                    )
                    parts.append(f"Your recent pull requests: {recent_str}")
                    for p in closed_mine[:2]:
                        r_short = p["repository_url"].split("/")[-1]
                        bullets.append(
                            f"[Merged/Closed · @{gh_user}] {r_short}#{p['number']}: {p['title']}"
                        )
                        gh_items.append(
                            {
                                "kind": "pr",
                                "number": p.get("number"),
                                "title": p.get("title"),
                                "repo": r_short,
                                "state": "merged" if (p.get("pull_request") or {}).get("merged_at") else p.get("state", "closed"),
                                "author": gh_user,
                                "comments": p.get("comments", 0),
                                "url": p.get("html_url"),
                            }
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
                    u_login = (p.get("user") or {}).get("login")
                    bullets.append(
                        f"[{upstream_slug}#{p['number']}] {p['title']} (@{u_login})"
                    )
                    if len(gh_items) < 5:
                        gh_items.append(
                            {
                                "kind": "pr",
                                "number": p.get("number"),
                                "title": p.get("title"),
                                "repo": upstream_slug.split("/")[-1],
                                "state": p.get("state", "open"),
                                "author": u_login,
                                "comments": p.get("comments", 0),
                                "url": p.get("html_url"),
                            }
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
                        items=gh_items[:5],
                        meta={
                            "user": gh_user,
                            "fork": fork_slug,
                            "upstream": upstream_slug,
                            "branches": fork_branches[:5],
                        },
                        surface_id="a2ui-ctx-github",
                    )
                except Exception:
                    pass

            if parts:
                return ". ".join(parts) + "."
            return f"Checked {fork_slug or upstream_slug}, and found no matching pull requests."
    except Exception as exc:
        return f"Error querying GitHub API for {target_repo}: {exc}"
