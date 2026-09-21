"""Unified Local + Remote (Cloud Run) Authentication & OAuth 2.0 Lifecycle Manager.

Supports:
1. OAuth 2.0 Authorization Code + PKCE & Refresh Token rotation for Spotify, Google Workspace, GitHub, and Slack.
2. Dynamic Redirect URI resolution using `APP_URL` (Cloud Run) or request headers, with automatic `127.0.0.1` loopback normalization for Spotify local dev.
3. Live API token verification and metadata auto-discovery (e.g. Slack `auth.test` -> `SLACK_TEAM_ID`, GitHub `/user` -> `@login`, Spotify `/v1/me`).
4. Local CLI auto-detection (`gh auth token` and `gcloud auth print-access-token`) when running in local development.
5. Local `.env` persistence and Cloud Run deployment secret export.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import secrets
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse, urlunparse

import httpx
from fastapi import Request

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DOTENV_PATH = PROJECT_ROOT / ".env"


@dataclass
class ProviderAuthState:
    provider: str
    account_label: str | None = None
    expires_at: float | None = None
    last_verified_at: float | None = None
    extra_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class PendingOAuthSession:
    provider: str
    state: str
    code_verifier: str
    redirect_uri: str
    created_at: float = field(default_factory=time.time)


_provider_states: dict[str, ProviderAuthState] = {}
_pending_oauth: dict[str, PendingOAuthSession] = {}

PROVIDER_CONFIGS: dict[str, dict[str, Any]] = {
    "google_workspace": {
        "display_name": "Google Workspace",
        "auth_env": "GOOGLE_WORKSPACE_ACCESS_TOKEN",
        "client_id_env": "GOOGLE_OAUTH_CLIENT_ID",
        "client_secret_env": "GOOGLE_OAUTH_CLIENT_SECRET",
        "refresh_token_env": "GOOGLE_WORKSPACE_REFRESH_TOKEN",
        "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "scopes": [
            "https://www.googleapis.com/auth/calendar",
            "https://www.googleapis.com/auth/gmail.modify",
            "https://www.googleapis.com/auth/drive.readonly",
            "https://www.googleapis.com/auth/userinfo.email",
        ],
        "modes": ["oauth", "cli", "token"],
        "setup_url": "https://console.cloud.google.com/apis/credentials",
    },
    "github": {
        "display_name": "GitHub",
        "auth_env": "GITHUB_PERSONAL_ACCESS_TOKEN",
        "client_id_env": "GITHUB_CLIENT_ID",
        "client_secret_env": "GITHUB_CLIENT_SECRET",
        "refresh_token_env": None,
        "authorize_url": "https://github.com/login/oauth/authorize",
        "token_url": "https://github.com/login/oauth/access_token",
        "scopes": ["repo", "read:org", "workflow", "read:user"],
        "modes": ["oauth", "cli", "token"],
        "setup_url": "https://github.com/settings/developers",
        "token_url_help": "https://github.com/settings/tokens/new?scopes=repo,read:org,workflow",
    },
    "spotify": {
        "display_name": "Spotify",
        "auth_env": "SPOTIFY_ACCESS_TOKEN",
        "client_id_env": "SPOTIFY_CLIENT_ID",
        "client_secret_env": "SPOTIFY_CLIENT_SECRET",
        "refresh_token_env": "SPOTIFY_REFRESH_TOKEN",
        "authorize_url": "https://accounts.spotify.com/authorize",
        "token_url": "https://accounts.spotify.com/api/token",
        "scopes": [
            "user-read-playback-state",
            "user-modify-playback-state",
            "user-read-currently-playing",
            "playlist-read-private",
            "user-read-email",
        ],
        "modes": ["oauth"],
        "setup_url": "https://developer.spotify.com/dashboard",
    },
    "slack": {
        "display_name": "Slack",
        "auth_env": "SLACK_BOT_TOKEN",
        "client_id_env": "SLACK_CLIENT_ID",
        "client_secret_env": "SLACK_CLIENT_SECRET",
        "refresh_token_env": None,
        "extra_env": ["SLACK_TEAM_ID"],
        "authorize_url": "https://slack.com/oauth/v2/authorize",
        "token_url": "https://slack.com/api/oauth.v2.access",
        "scopes": [
            "channels:history",
            "channels:read",
            "chat:write",
            "users:read",
            "team:read",
        ],
        "modes": ["oauth", "token"],
        "setup_url": "https://api.slack.com/apps",
    },
}


def is_remote_deployment() -> bool:
    """Returns True when running in Cloud Run, GKE, or with a public APP_URL."""
    if os.getenv("K_SERVICE") or os.getenv("CLOUD_RUN_JOB"):
        return True
    app_url = os.getenv("APP_URL", "")
    return bool(app_url and "localhost" not in app_url and "127.0.0.1" not in app_url)


def get_public_base_url(request: Request | None = None) -> str:
    """Resolves the public base URL of the server for OAuth redirect callbacks."""
    app_url = (os.getenv("APP_URL") or "").strip().rstrip("/")
    if app_url:
        return app_url

    if request is not None:
        proto = request.headers.get("x-forwarded-proto") or request.url.scheme or "http"
        host = (
            request.headers.get("x-forwarded-host")
            or request.headers.get("host")
            or request.url.netloc
            or "127.0.0.1:8000"
        )
        # If running behind Vite dev server on :3000/:5173, route callback to backend or proxied origin
        return f"{proto}://{host}".rstrip("/")

    return "http://127.0.0.1:8000"


def get_redirect_uri(provider: str, request: Request | None = None) -> str:
    """Computes the canonical OAuth 2.0 Redirect URI for a given provider.

    Note: Spotify requires explicit `127.0.0.1` instead of `localhost` for loopback HTTP URIs,
    while Cloud Run deployments use the `https://<service>.run.app` origin directly.
    """
    explicit_override = os.getenv(f"{provider.upper()}_REDIRECT_URI")
    if explicit_override:
        return explicit_override.strip()

    base = get_public_base_url(request)
    parsed = urlparse(base)
    if provider == "spotify" and parsed.hostname == "localhost":
        netloc = f"127.0.0.1:{parsed.port}" if parsed.port else "127.0.0.1"
        base = urlunparse((parsed.scheme, netloc, parsed.path, "", "", "")).rstrip("/")

    return f"{base}/api/v1/auth/{provider}/callback"


def persist_env_vars(updates: dict[str, str | None]) -> None:
    """Updates process environment variables and persists them to `.env` (outside unit tests)."""
    for k, v in updates.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

    # Do not mutate workspace .env file during pytest runs
    if os.getenv("PYTEST_CURRENT_TEST"):
        return

    try:
        existing_lines: list[str] = []
        if DOTENV_PATH.exists():
            existing_lines = DOTENV_PATH.read_text().splitlines()

        updated_keys: set[str] = set()
        new_lines: list[str] = []
        for line in existing_lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                new_lines.append(line)
                continue
            k = stripped.removeprefix("export ").split("=", 1)[0].strip()
            if k in updates:
                updated_keys.add(k)
                val = updates[k]
                if val is not None:
                    new_lines.append(f'{k}="{val}"')
            else:
                new_lines.append(line)

        for k, val in updates.items():
            if k not in updated_keys and val is not None:
                new_lines.append(f'{k}="{val}"')

        DOTENV_PATH.write_text("\n".join(new_lines) + "\n")
    except Exception as exc:
        logger.debug("Skipped writing .env file: %s", exc)


def _sync_dotenv_if_needed() -> None:
    if not os.getenv("PYTEST_CURRENT_TEST") and DOTENV_PATH.exists():
        from dotenv import load_dotenv

        load_dotenv(DOTENV_PATH, override=False)


def has_provider_credentials(provider: str) -> bool:
    """Returns True if the provider has either an active access token or a complete refresh token bundle."""
    _sync_dotenv_if_needed()
    cfg = PROVIDER_CONFIGS.get(provider)
    if not cfg:
        return False
    auth_env = cfg.get("auth_env")
    if auth_env and os.getenv(auth_env):
        return True
    refresh_env = cfg.get("refresh_token_env")
    cid_env = cfg.get("client_id_env")
    csec_env = cfg.get("client_secret_env")
    if refresh_env and cid_env and csec_env:
        if os.getenv(refresh_env) and os.getenv(cid_env) and os.getenv(csec_env):
            return True
    return False


def get_provider_auth_summary(provider: str, request: Request | None = None) -> dict[str, Any]:
    """Builds structured authentication status and setup metadata for the frontend UI."""
    _sync_dotenv_if_needed()
    cfg = PROVIDER_CONFIGS.get(provider, {})
    cid_env = cfg.get("client_id_env")
    csec_env = cfg.get("client_secret_env")
    refresh_env = cfg.get("refresh_token_env")
    auth_env = cfg.get("auth_env")

    client_id_set = bool(cid_env and os.getenv(cid_env))
    client_secret_set = bool(csec_env and os.getenv(csec_env))
    refresh_token_set = bool(refresh_env and os.getenv(refresh_env))
    access_token_set = bool(auth_env and os.getenv(auth_env))

    st = _provider_states.get(provider)
    account_label = st.account_label if st else None
    if not account_label and provider == "slack" and os.getenv("SLACK_TEAM_ID"):
        account_label = f"Team {os.getenv('SLACK_TEAM_ID')}"

    cli_available = False
    if not is_remote_deployment():
        if provider == "github":
            cli_available = shutil.which("gh") is not None
        elif provider == "google_workspace":
            cli_available = shutil.which("gcloud") is not None

    return {
        "provider": provider,
        "modes": cfg.get("modes", ["token"]),
        "oauth_ready": client_id_set and client_secret_set,
        "client_id_env": cid_env,
        "client_secret_env": csec_env,
        "refresh_token_env": refresh_env,
        "client_id_set": client_id_set,
        "client_secret_set": client_secret_set,
        "refresh_token_set": refresh_token_set,
        "access_token_set": access_token_set,
        "has_credentials": has_provider_credentials(provider),
        "redirect_uri": get_redirect_uri(provider, request),
        "setup_url": cfg.get("setup_url"),
        "token_url_help": cfg.get("token_url_help"),
        "account_label": account_label,
        "cli_available": cli_available,
        "is_remote": is_remote_deployment(),
        "extra_env": {
            k: os.getenv(k) for k in cfg.get("extra_env", []) if os.getenv(k)
        },
    }


def _generate_pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def build_oauth_authorize_url(
    provider: str,
    request: Request | None = None,
    client_id: str | None = None,
    client_secret: str | None = None,
) -> str:
    """Saves optional client credentials and constructs the provider OAuth 2.0 authorization URL."""
    cfg = PROVIDER_CONFIGS.get(provider)
    if not cfg:
        raise ValueError(f"Unsupported OAuth provider: {provider}")

    cid_env = cfg["client_id_env"]
    csec_env = cfg["client_secret_env"]

    updates: dict[str, str | None] = {}
    if client_id and client_id.strip():
        updates[cid_env] = client_id.strip()
    if client_secret and client_secret.strip():
        updates[csec_env] = client_secret.strip()
    if updates:
        persist_env_vars(updates)

    cid = os.getenv(cid_env, "").strip()
    if not cid:
        raise ValueError(
            f"Missing {cid_env}. Enter your {cfg['display_name']} Client ID first."
        )

    redirect_uri = get_redirect_uri(provider, request)
    state = secrets.token_urlsafe(24)
    verifier, challenge = _generate_pkce_pair()
    _pending_oauth[state] = PendingOAuthSession(
        provider=provider,
        state=state,
        code_verifier=verifier,
        redirect_uri=redirect_uri,
    )

    scopes = cfg.get("scopes", [])
    if provider == "spotify":
        params = {
            "client_id": cid,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "state": state,
            "scope": " ".join(scopes),
            "code_challenge_method": "S256",
            "code_challenge": challenge,
            "show_dialog": "true",
        }
    elif provider == "google_workspace":
        params = {
            "client_id": cid,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "state": state,
            "scope": " ".join(scopes),
            "access_type": "offline",
            "prompt": "consent",
            "code_challenge_method": "S256",
            "code_challenge": challenge,
        }
    elif provider == "github":
        params = {
            "client_id": cid,
            "redirect_uri": redirect_uri,
            "state": state,
            "scope": " ".join(scopes),
        }
    elif provider == "slack":
        params = {
            "client_id": cid,
            "redirect_uri": redirect_uri,
            "state": state,
            "scope": ",".join(scopes),
        }
    else:
        raise ValueError(f"Unsupported provider {provider}")

    return f"{cfg['authorize_url']}?{urlencode(params)}"


async def exchange_oauth_code(
    provider: str,
    code: str,
    state: str | None = None,
    request: Request | None = None,
) -> dict[str, Any]:
    """Exchanges an OAuth 2.0 authorization code for access & refresh tokens and verifies identity."""
    cfg = PROVIDER_CONFIGS.get(provider)
    if not cfg:
        raise ValueError(f"Unsupported provider: {provider}")

    pending = _pending_oauth.pop(state, None) if state else None
    redirect_uri = (
        pending.redirect_uri if pending else get_redirect_uri(provider, request)
    )
    cid = os.getenv(cfg["client_id_env"], "").strip()
    csec = os.getenv(cfg["client_secret_env"], "").strip()

    async with httpx.AsyncClient(timeout=15.0) as client:
        if provider == "spotify":
            basic = base64.b64encode(f"{cid}:{csec}".encode()).decode()
            data = {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
            }
            if pending and pending.code_verifier:
                data["code_verifier"] = pending.code_verifier
            resp = await client.post(
                cfg["token_url"],
                data=data,
                headers={
                    "Authorization": f"Basic {basic}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            payload = resp.json()
            if resp.status_code >= 400 or "access_token" not in payload:
                raise ValueError(
                    payload.get("error_description")
                    or payload.get("error")
                    or f"Spotify token exchange failed ({resp.status_code})"
                )
            access_token = payload["access_token"]
            refresh_token = payload.get("refresh_token")
            expires_in = int(payload.get("expires_in", 3600))
            updates: dict[str, str | None] = {"SPOTIFY_ACCESS_TOKEN": access_token}
            if refresh_token:
                updates["SPOTIFY_REFRESH_TOKEN"] = refresh_token
            persist_env_vars(updates)
            verify_info = await verify_and_save_token("spotify", access_token, expires_in=expires_in)
            return verify_info

        if provider == "google_workspace":
            data = {
                "client_id": cid,
                "client_secret": csec,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
            }
            if pending and pending.code_verifier:
                data["code_verifier"] = pending.code_verifier
            resp = await client.post(cfg["token_url"], data=data)
            payload = resp.json()
            if resp.status_code >= 400 or "access_token" not in payload:
                raise ValueError(
                    payload.get("error_description")
                    or payload.get("error")
                    or f"Google OAuth exchange failed ({resp.status_code})"
                )
            access_token = payload["access_token"]
            refresh_token = payload.get("refresh_token")
            expires_in = int(payload.get("expires_in", 3600))
            updates = {"GOOGLE_WORKSPACE_ACCESS_TOKEN": access_token}
            if refresh_token:
                updates["GOOGLE_WORKSPACE_REFRESH_TOKEN"] = refresh_token
            persist_env_vars(updates)
            return await verify_and_save_token("google_workspace", access_token, expires_in=expires_in)

        if provider == "github":
            resp = await client.post(
                cfg["token_url"],
                data={
                    "client_id": cid,
                    "client_secret": csec,
                    "code": code,
                    "redirect_uri": redirect_uri,
                },
                headers={"Accept": "application/json"},
            )
            payload = resp.json()
            if resp.status_code >= 400 or "access_token" not in payload:
                raise ValueError(
                    payload.get("error_description")
                    or payload.get("error")
                    or "GitHub OAuth token exchange failed"
                )
            access_token = payload["access_token"]
            return await verify_and_save_token("github", access_token)

        if provider == "slack":
            resp = await client.post(
                cfg["token_url"],
                data={
                    "client_id": cid,
                    "client_secret": csec,
                    "code": code,
                    "redirect_uri": redirect_uri,
                },
            )
            payload = resp.json()
            if not payload.get("ok") or "access_token" not in payload:
                raise ValueError(payload.get("error") or "Slack OAuth exchange failed")
            access_token = payload["access_token"]
            team_info = payload.get("team") or {}
            team_id = team_info.get("id")
            team_name = team_info.get("name")
            return await verify_and_save_token(
                "slack",
                access_token,
                extra_env={"SLACK_TEAM_ID": team_id} if team_id else None,
                account_hint=team_name,
            )

    raise ValueError(f"Unsupported provider: {provider}")


async def ensure_fresh_access_token(provider: str, *, force: bool = False) -> str | None:
    """Returns a valid access token for `provider`, refreshing via `REFRESH_TOKEN` if expired, unverified in this process, or missing."""
    _sync_dotenv_if_needed()
    cfg = PROVIDER_CONFIGS.get(provider)
    if not cfg:
        return None
    auth_env = cfg["auth_env"]
    current_token = os.getenv(auth_env)
    st = _provider_states.get(provider)

    now = time.time()
    needs_refresh = force
    if not current_token:
        needs_refresh = True
    elif st is None or st.expires_at is None or now >= (st.expires_at - 120):
        needs_refresh = True

    refresh_env = cfg.get("refresh_token_env")
    cid_env = cfg.get("client_id_env")
    csec_env = cfg.get("client_secret_env")
    if (
        needs_refresh
        and refresh_env
        and cid_env
        and csec_env
        and os.getenv(refresh_env)
        and os.getenv(cid_env)
        and os.getenv(csec_env)
    ):
        refresh_tok = os.getenv(refresh_env, "").strip()
        cid = os.getenv(cid_env, "").strip()
        csec = os.getenv(csec_env, "").strip()
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                if provider == "spotify":
                    basic = base64.b64encode(f"{cid}:{csec}".encode()).decode()
                    resp = await client.post(
                        cfg["token_url"],
                        data={
                            "grant_type": "refresh_token",
                            "refresh_token": refresh_tok,
                        },
                        headers={
                            "Authorization": f"Basic {basic}",
                            "Content-Type": "application/x-www-form-urlencoded",
                        },
                    )
                    data = resp.json()
                    if resp.status_code == 200 and "access_token" in data:
                        new_token = data["access_token"]
                        new_refresh = data.get("refresh_token")
                        expires_in = int(data.get("expires_in", 3600))
                        updates = {"SPOTIFY_ACCESS_TOKEN": new_token}
                        if new_refresh:
                            updates["SPOTIFY_REFRESH_TOKEN"] = new_refresh
                        persist_env_vars(updates)
                        _provider_states["spotify"] = ProviderAuthState(
                            provider="spotify",
                            account_label=st.account_label if st else "Spotify Connected",
                            expires_at=now + expires_in,
                            last_verified_at=now,
                        )
                        return new_token
                elif provider == "google_workspace":
                    resp = await client.post(
                        cfg["token_url"],
                        data={
                            "client_id": cid,
                            "client_secret": csec,
                            "refresh_token": refresh_tok,
                            "grant_type": "refresh_token",
                        },
                    )
                    data = resp.json()
                    if resp.status_code == 200 and "access_token" in data:
                        new_token = data["access_token"]
                        expires_in = int(data.get("expires_in", 3600))
                        persist_env_vars({"GOOGLE_WORKSPACE_ACCESS_TOKEN": new_token})
                        _provider_states["google_workspace"] = ProviderAuthState(
                            provider="google_workspace",
                            account_label=st.account_label if st else "Google Workspace",
                            expires_at=now + expires_in,
                            last_verified_at=now,
                        )
                        return new_token
        except Exception as exc:
            logger.warning("Failed to auto-refresh %s token: %s", provider, exc)

    return os.getenv(auth_env)


async def verify_and_save_token(
    provider: str,
    token: str,
    *,
    extra_env: dict[str, str | None] | None = None,
    expires_in: int | None = None,
    account_hint: str | None = None,
) -> dict[str, Any]:
    """Verifies a token against the provider's identity API, auto-discovers metadata (like SLACK_TEAM_ID), and saves it."""
    cfg = PROVIDER_CONFIGS.get(provider)
    if not cfg:
        raise ValueError(f"Unknown provider: {provider}")

    clean_token = token.strip()
    if not clean_token:
        raise ValueError("Token cannot be empty")

    auth_env = cfg["auth_env"]
    updates: dict[str, str | None] = {auth_env: clean_token}
    if extra_env:
        for k, v in extra_env.items():
            if v:
                updates[k] = v.strip()

    account_label = account_hint
    now = time.time()

    # Skip external HTTP verification for deterministic unit/eval test tokens
    is_mock_token = (
        clean_token.startswith("xoxb-test")
        or clean_token.startswith("ghp_test")
        or clean_token.startswith("ya29.workspace-live")
        or clean_token.startswith("test-")
        or bool(os.getenv("PYTEST_CURRENT_TEST"))
    )

    if not is_mock_token:
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                if provider == "slack":
                    resp = await client.post(
                        "https://slack.com/api/auth.test",
                        headers={"Authorization": f"Bearer {clean_token}"},
                    )
                    data = resp.json()
                    if data.get("ok"):
                        team_id = data.get("team_id")
                        team_name = data.get("team")
                        user_name = data.get("user")
                        if team_id:
                            updates["SLACK_TEAM_ID"] = str(team_id)
                        account_label = (
                            f"{team_name} (@{user_name})"
                            if team_name and user_name
                            else team_name or team_id
                        )
                elif provider == "github":
                    resp = await client.get(
                        "https://api.github.com/user",
                        headers={
                            "Authorization": f"Bearer {clean_token}",
                            "Accept": "application/vnd.github+json",
                        },
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        login = data.get("login")
                        if login:
                            account_label = f"@{login}"
                elif provider == "spotify":
                    resp = await client.get(
                        "https://api.spotify.com/v1/me",
                        headers={"Authorization": f"Bearer {clean_token}"},
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        display = data.get("display_name") or data.get("email")
                        product = data.get("product")
                        if display:
                            account_label = (
                                f"{display} ({product.title()})"
                                if product
                                else str(display)
                            )
                elif provider == "google_workspace":
                    resp = await client.get(
                        "https://www.googleapis.com/oauth2/v2/userinfo",
                        headers={"Authorization": f"Bearer {clean_token}"},
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        account_label = data.get("email") or data.get("name")
        except Exception as exc:
            logger.debug("Live token verification skipped for %s: %s", provider, exc)

    persist_env_vars(updates)

    _provider_states[provider] = ProviderAuthState(
        provider=provider,
        account_label=account_label or f"{cfg['display_name']} Connected",
        expires_at=(now + expires_in) if expires_in else None,
        last_verified_at=now,
    )

    return {
        "ok": True,
        "provider": provider,
        "account_label": _provider_states[provider].account_label,
        "saved_keys": [k for k, v in updates.items() if v is not None],
    }


def get_workspace_headers(token: str) -> dict[str, str]:
    """Builds Authorization + X-Goog-User-Project headers required for Google Workspace APIs."""
    import json

    headers = {"Authorization": f"Bearer {token}"}
    qp = os.getenv("GOOGLE_WORKSPACE_QUOTA_PROJECT", "").strip()
    if not qp:
        adc_path = Path.home() / ".config" / "gcloud" / "application_default_credentials.json"
        if adc_path.exists():
            try:
                qp = str(json.loads(adc_path.read_text()).get("quota_project_id") or "").strip()
            except Exception:
                qp = ""
    if not qp:
        qp = os.getenv("SANDBOX_GCP_PROJECT") or os.getenv("GOOGLE_CLOUD_PROJECT") or ""
    if qp:
        headers["X-Goog-User-Project"] = qp
    return headers


async def sync_workspace_from_adc(*, run_login_if_needed: bool = False) -> dict[str, Any]:
    """Reads or acquires Google Workspace OAuth credentials (`client_id`, `client_secret`, `refresh_token`) via `gcloud auth application-default` with Calendar, Gmail, and Drive scopes."""
    import json

    adc_path = Path.home() / ".config" / "gcloud" / "application_default_credentials.json"
    required_scopes = (
        "https://www.googleapis.com/auth/cloud-platform,"
        "https://www.googleapis.com/auth/calendar,"
        "https://www.googleapis.com/auth/gmail.modify,"
        "https://www.googleapis.com/auth/drive.readonly,"
        "https://www.googleapis.com/auth/userinfo.email"
    )

    async def _try_adc_file() -> dict[str, Any] | None:
        if not adc_path.exists():
            return None
        try:
            raw = json.loads(adc_path.read_text())
            cid = raw.get("client_id")
            csec = raw.get("client_secret")
            rtok = raw.get("refresh_token")
            qp = raw.get("quota_project_id") or os.getenv("SANDBOX_GCP_PROJECT") or os.getenv("GOOGLE_CLOUD_PROJECT")
            if not (cid and csec and rtok):
                return None
            if qp:
                os.environ["GOOGLE_WORKSPACE_QUOTA_PROJECT"] = str(qp)
            async with httpx.AsyncClient(timeout=10.0) as client:
                tr = await client.post(
                    "https://oauth2.googleapis.com/token",
                    data={
                        "client_id": cid,
                        "client_secret": csec,
                        "refresh_token": rtok,
                        "grant_type": "refresh_token",
                    },
                )
                if tr.status_code != 200 or "access_token" not in tr.json():
                    return None
                atok = tr.json()["access_token"]
                ws_headers = get_workspace_headers(atok)
                cr = await client.get(
                    "https://www.googleapis.com/calendar/v3/users/me/calendarList",
                    params={"maxResults": 1},
                    headers=ws_headers,
                )
                if cr.status_code != 200:
                    return None
                gr = await client.get(
                    "https://gmail.googleapis.com/gmail/v1/users/me/profile",
                    headers=ws_headers,
                )
                email_addr = gr.json().get("emailAddress") if gr.status_code == 200 else None
                persist_env_vars(
                    {
                        "GOOGLE_OAUTH_CLIENT_ID": str(cid),
                        "GOOGLE_OAUTH_CLIENT_SECRET": str(csec),
                        "GOOGLE_WORKSPACE_REFRESH_TOKEN": str(rtok),
                        "GOOGLE_WORKSPACE_ACCESS_TOKEN": str(atok),
                        "GOOGLE_WORKSPACE_QUOTA_PROJECT": str(qp) if qp else None,
                    }
                )
                return await verify_and_save_token(
                    "google_workspace",
                    str(atok),
                    expires_in=3500,
                    account_hint=email_addr,
                )
        except Exception:
            return None

    existing = await _try_adc_file()
    if existing:
        return existing

    if not run_login_if_needed:
        raise ValueError(
            "Local ADC token does not have Calendar/Gmail/Drive scopes yet."
        )

    if not shutil.which("gcloud"):
        raise ValueError("gcloud CLI is not installed.")

    await asyncio.to_thread(
        subprocess.run,
        [
            "gcloud",
            "auth",
            "application-default",
            "login",
            "--launch-browser",
            f"--scopes={required_scopes}",
        ],
        check=False,
    )
    after_login = await _try_adc_file()
    if after_login:
        return after_login
    raise ValueError(
        "Google Workspace browser authorization did not grant Calendar/Gmail/Drive scopes."
    )


async def detect_local_cli_token(provider: str) -> dict[str, Any]:
    """Detects credentials from local developer CLI (`gh auth token` or `gcloud auth application-default`)."""
    if provider == "github":
        if not shutil.which("gh"):
            raise ValueError("GitHub CLI (`gh`) is not installed on this machine.")
        proc = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        token = (proc.stdout or "").strip()
        if proc.returncode != 0 or not token:
            raise ValueError(
                "GitHub CLI is not logged in. Run `gh auth login` or use OAuth / PAT."
            )
        return await verify_and_save_token("github", token)

    if provider == "google_workspace":
        return await sync_workspace_from_adc(run_login_if_needed=True)

    raise ValueError(
        f"Local CLI token detection is not supported for {provider}. Use OAuth 2.0 Client ID & Secret instead."
    )


async def verify_all_integrations() -> dict[str, dict[str, Any]]:
    """Performs live API verification for Google Workspace, GitHub, Spotify, and Slack.

    Never reports `verified=True` unless a real API request to the provider succeeds (`200 OK`).
    """
    _sync_dotenv_if_needed()
    results: dict[str, dict[str, Any]] = {}

    async with httpx.AsyncClient(timeout=8.0) as client:
        # 1. Google Workspace (Calendar / Gmail / Drive)
        ws_tok = await ensure_fresh_access_token("google_workspace")
        if not ws_tok:
            try:
                adc_res = await sync_workspace_from_adc(run_login_if_needed=False)
                if adc_res.get("ok"):
                    ws_tok = os.getenv("GOOGLE_WORKSPACE_ACCESS_TOKEN")
            except Exception:
                pass
        if not ws_tok or ws_tok.startswith("ya29.workspace-live"):
            results["google_workspace"] = {
                "verified": False,
                "status": "not_configured",
                "detail": "Not connected (toggle Google Workspace in UI or run `make onboard`)",
            }
        else:
            try:
                ws_headers = get_workspace_headers(ws_tok)
                r = await client.get(
                    "https://www.googleapis.com/calendar/v3/users/me/calendarList",
                    params={"maxResults": 1},
                    headers=ws_headers,
                )
                if r.status_code == 200:
                    gr = await client.get(
                        "https://gmail.googleapis.com/gmail/v1/users/me/profile",
                        headers=ws_headers,
                    )
                    email_addr = gr.json().get("emailAddress") if gr.status_code == 200 else "Google Workspace"
                    _provider_states["google_workspace"] = ProviderAuthState(
                        provider="google_workspace",
                        account_label=email_addr,
                        last_verified_at=time.time(),
                    )
                    results["google_workspace"] = {
                        "verified": True,
                        "status": "ready",
                        "detail": f"Verified as {email_addr} (Calendar, Gmail & Drive active)",
                    }
                else:
                    # Clear invalid/gcloud-only token so it doesn't cause false-positive READY state
                    if not os.getenv("GOOGLE_WORKSPACE_REFRESH_TOKEN"):
                        persist_env_vars({"GOOGLE_WORKSPACE_ACCESS_TOKEN": None})
                    results["google_workspace"] = {
                        "verified": False,
                        "status": "invalid_scope",
                        "detail": f"Token rejected by Google Calendar API ({r.status_code}) — requires Google OAuth 2.0",
                    }
            except Exception as exc:
                results["google_workspace"] = {
                    "verified": False,
                    "status": "error",
                    "detail": str(exc),
                }

        # 2. GitHub
        gh_tok = os.getenv("GITHUB_PERSONAL_ACCESS_TOKEN", "").strip()
        if not gh_tok:
            results["github"] = {
                "verified": False,
                "status": "not_configured",
                "detail": "Not connected (run `gh auth login` or `make onboard`)",
            }
        else:
            try:
                r = await client.get(
                    "https://api.github.com/user",
                    headers={
                        "Authorization": f"Bearer {gh_tok}",
                        "Accept": "application/vnd.github+json",
                    },
                )
                if r.status_code == 200:
                    login = r.json().get("login", "user")
                    scopes = r.headers.get("x-oauth-scopes", "")
                    _provider_states["github"] = ProviderAuthState(
                        provider="github",
                        account_label=f"@{login}",
                        last_verified_at=time.time(),
                    )
                    results["github"] = {
                        "verified": True,
                        "status": "ready",
                        "detail": f"Verified as @{login}" + (f" (scopes: {scopes})" if scopes else ""),
                    }
                else:
                    results["github"] = {
                        "verified": False,
                        "status": "invalid_token",
                        "detail": f"GitHub API returned {r.status_code}",
                    }
            except Exception as exc:
                results["github"] = {
                    "verified": False,
                    "status": "error",
                    "detail": str(exc),
                }

        # 3. Spotify
        sp_tok = await ensure_fresh_access_token("spotify")
        if not sp_tok:
            cid_ok = bool(os.getenv("SPOTIFY_CLIENT_ID") and os.getenv("SPOTIFY_CLIENT_SECRET"))
            results["spotify"] = {
                "verified": False,
                "status": "awaiting_oauth" if cid_ok else "not_configured",
                "detail": (
                    "Client ID & Secret present — click Authorize in UI or `make onboard` to complete OAuth"
                    if cid_ok
                    else "Not configured (set SPOTIFY_CLIENT_ID & SPOTIFY_CLIENT_SECRET)"
                ),
            }
        else:
            try:
                r = await client.get(
                    "https://api.spotify.com/v1/me",
                    headers={"Authorization": f"Bearer {sp_tok}"},
                )
                if r.status_code == 200:
                    data = r.json()
                    display = data.get("display_name") or data.get("email") or "Spotify User"
                    product = data.get("product", "")
                    label = f"{display} ({product.title()})" if product else str(display)
                    _provider_states["spotify"] = ProviderAuthState(
                        provider="spotify",
                        account_label=label,
                        last_verified_at=time.time(),
                    )
                    results["spotify"] = {
                        "verified": True,
                        "status": "ready",
                        "detail": f"Verified as {label}",
                    }
                else:
                    results["spotify"] = {
                        "verified": False,
                        "status": f"http_{r.status_code}",
                        "detail": f"Spotify API returned {r.status_code}: {r.text.strip()[:140]}",
                    }
            except Exception as exc:
                results["spotify"] = {
                    "verified": False,
                    "status": "error",
                    "detail": str(exc),
                }

        # 4. Slack
        sl_tok = os.getenv("SLACK_BOT_TOKEN", "").strip()
        if not sl_tok:
            results["slack"] = {
                "verified": False,
                "status": "not_configured",
                "detail": "Optional (paste SLACK_BOT_TOKEN xoxb-... in UI or `make onboard`)",
            }
        else:
            try:
                r = await client.post(
                    "https://slack.com/api/auth.test",
                    headers={"Authorization": f"Bearer {sl_tok}"},
                )
                data = r.json()
                if data.get("ok"):
                    team_id = data.get("team_id")
                    team = data.get("team")
                    user = data.get("user")
                    if team_id and not os.getenv("SLACK_TEAM_ID"):
                        persist_env_vars({"SLACK_TEAM_ID": str(team_id)})
                    label = f"{team} (@{user})" if team and user else (team or team_id)
                    _provider_states["slack"] = ProviderAuthState(
                        provider="slack",
                        account_label=label,
                        last_verified_at=time.time(),
                    )
                    results["slack"] = {
                        "verified": True,
                        "status": "ready",
                        "detail": f"Verified workspace {label} (SLACK_TEAM_ID={team_id})",
                    }
                else:
                    results["slack"] = {
                        "verified": False,
                        "status": "invalid_token",
                        "detail": f"Slack auth.test error: {data.get('error')}",
                    }
            except Exception as exc:
                results["slack"] = {
                    "verified": False,
                    "status": "error",
                    "detail": str(exc),
                }

    return results


async def run_local_browser_oauth(provider: str) -> dict[str, Any]:
    """Runs an interactive browser OAuth 2.0 flow from the CLI onboarding wizard (`make onboard`)."""
    import socket
    import webbrowser
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from urllib.parse import parse_qs

    auth_url = build_oauth_authorize_url(provider=provider)
    captured: dict[str, str] = {}

    # Check if FastAPI is already listening on 127.0.0.1:8000
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_already_running = sock.connect_ex(("127.0.0.1", 8000)) == 0
    sock.close()

    if server_already_running:
        print(f"\n→ Opening {provider} OAuth consent screen in your browser...")
        print(f"  If it doesn't open automatically, visit:\n  {auth_url}\n")
        webbrowser.open(auth_url)
        input("  Press Enter here after completing authorization in your browser...")
        _sync_dotenv_if_needed()
        verified = await verify_all_integrations()
        return verified.get(provider, {"verified": False})

    class _CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            if "code" in qs:
                captured["code"] = qs["code"][0]
            if "state" in qs:
                captured["state"] = qs["state"][0]
            if "error" in qs:
                captured["error"] = qs["error"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                b"<html><body style='background:#090b10;color:#f8fafc;font-family:sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;'>"
                b"<div><h2>Authorization received!</h2><p>You can close this tab and return to your terminal.</p></div>"
                b"<script>setTimeout(()=>window.close(),800);</script></body></html>"
            )

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            pass

    httpd = HTTPServer(("127.0.0.1", 8000), _CallbackHandler)
    httpd.timeout = 120
    print(f"\n→ Opening {provider} OAuth consent screen in your browser...")
    print(f"  Listening on http://127.0.0.1:8000/api/v1/auth/{provider}/callback ...")
    webbrowser.open(auth_url)
    httpd.handle_request()
    httpd.server_close()

    if "code" not in captured:
        raise ValueError(captured.get("error") or "No OAuth code received from browser callback.")

    await exchange_oauth_code(provider, code=captured["code"], state=captured.get("state"))
    verified = await verify_all_integrations()
    return verified.get(provider, {"verified": True})


def disconnect_provider(provider: str) -> None:
    """Removes stored credentials for `provider`."""
    cfg = PROVIDER_CONFIGS.get(provider)
    if not cfg:
        return
    keys_to_clear: dict[str, str | None] = {}
    if cfg.get("auth_env"):
        keys_to_clear[cfg["auth_env"]] = None
    if cfg.get("refresh_token_env"):
        keys_to_clear[cfg["refresh_token_env"]] = None
    for k in cfg.get("extra_env", []):
        keys_to_clear[k] = None
    persist_env_vars(keys_to_clear)
    _provider_states.pop(provider, None)


def export_deployment_env() -> str:
    """Generates an exportable `.env` block suitable for Cloud Run / Secret Manager deployment."""
    export_keys = [
        "GOOGLE_OAUTH_CLIENT_ID",
        "GOOGLE_OAUTH_CLIENT_SECRET",
        "GOOGLE_WORKSPACE_REFRESH_TOKEN",
        "GOOGLE_WORKSPACE_ACCESS_TOKEN",
        "GITHUB_CLIENT_ID",
        "GITHUB_CLIENT_SECRET",
        "GITHUB_PERSONAL_ACCESS_TOKEN",
        "SPOTIFY_CLIENT_ID",
        "SPOTIFY_CLIENT_SECRET",
        "SPOTIFY_REFRESH_TOKEN",
        "SPOTIFY_ACCESS_TOKEN",
        "SLACK_CLIENT_ID",
        "SLACK_CLIENT_SECRET",
        "SLACK_BOT_TOKEN",
        "SLACK_TEAM_ID",
    ]
    lines = [
        "# ADK Sonar Voice Orchestrator - Cloud Run / Deployment Credentials",
        "# Set these in Cloud Run environment variables or Secret Manager",
    ]
    for key in export_keys:
        val = os.getenv(key)
        if val:
            lines.append(f'{key}="{val}"')
    return "\n".join(lines) + "\n"
