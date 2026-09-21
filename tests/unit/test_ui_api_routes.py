"""Unit tests for the /api/v1 mobile UI control-plane and Scoped A2UI surface builder."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.fast_api_app import app
from app.integrations import reset_integration_registry
from app.tasks import reset_task_registry
from app.workers import reset_harness_registry

client = TestClient(app)


def setup_function() -> None:
    reset_task_registry()
    reset_integration_registry()
    reset_harness_registry()


def test_get_orchestrator_state_and_demo_a2ui_surfaces() -> None:
    res = client.get("/api/v1/state")
    assert res.status_code == 200
    data = res.json()
    assert "default_harness" in data
    assert "integrations" in data
    assert "workspace_connection" in data
    assert "secrets" in data

    # Seed demo tasks and verify A2UI Plan Approval surface is synthesized
    seed_res = client.post("/api/v1/tasks/demo")
    assert seed_res.status_code == 200

    state_after = client.get("/api/v1/state").json()
    assert state_after["task_counts"]["awaiting_input"] >= 1
    assert len(state_after["a2ui_surfaces"]) >= 1
    plan_surface = state_after["a2ui_surfaces"][0]
    assert plan_surface["kind"] == "plan_approval"
    assert plan_surface["version"] == "v0.9.1"


def test_unified_single_toggle_integration_and_secrets_vault() -> None:
    # Verify cross-port Origin header (http://localhost:3000) is permitted by ADK _OriginCheckMiddleware
    res_origin = client.patch(
        "/api/v1/integrations/google_search",
        json={"enabled": False},
        headers={"Origin": "http://localhost:3000"},
    )
    assert res_origin.status_code == 200
    assert res_origin.json()["enabled"] is False

    # Toggle slack off and back on via the unified endpoint
    res_off = client.patch("/api/v1/integrations/slack", json={"enabled": False})
    assert res_off.status_code == 200
    assert res_off.json()["enabled"] is False

    res_on = client.patch("/api/v1/integrations/slack", json={"enabled": True})
    assert res_on.status_code == 200
    assert res_on.json()["enabled"] is True

    # Import a .env payload and verify secrets + integration status become 'ready'
    dotenv_res = client.post(
        "/api/v1/secrets/dotenv",
        json={
            "content": "SLACK_BOT_TOKEN=xoxb-test-token-1234\nGITHUB_PERSONAL_ACCESS_TOKEN=ghp_test9999\n",
            "filename": ".env.test",
        },
    )
    assert dotenv_res.status_code == 200
    assert "SLACK_BOT_TOKEN" in dotenv_res.json()["imported_keys"]

    state = client.get("/api/v1/state").json()
    slack_item = next(i for i in state["integrations"] if i["name"] == "slack")
    assert slack_item["has_credentials"] is True
    assert slack_item["status"] == "ready"


def test_integration_ordering_and_oauth_flows(monkeypatch) -> None:
    state = client.get("/api/v1/state").json()
    names = [i["name"] for i in state["integrations"]]
    # Verify Workspace -> Google Search -> Google Maps -> GitHub -> Spotify -> Slack order
    assert names.index("google_calendar") < names.index("google_search")
    assert names.index("google_search") < names.index("google_maps")
    assert names.index("google_maps") < names.index("github")
    assert names.index("github") < names.index("spotify")
    assert names.index("spotify") < names.index("slack")

    # Verify Spotify OAuth authorize endpoint saves Client ID & Secret and returns 127.0.0.1 callback locally
    auth_res = client.post(
        "/api/v1/auth/spotify/authorize",
        json={
            "client_id": "spotify-cid-123",
            "client_secret": "spotify-csec-456",
        },
        headers={"Host": "localhost:8000"},
    )
    assert auth_res.status_code == 200
    auth_url = auth_res.json()["authorize_url"]
    assert "accounts.spotify.com/authorize" in auth_url
    assert "127.0.0.1" in auth_url
    assert "code_challenge=" in auth_url

    # Verify Cloud Run APP_URL dynamically switches Redirect URI to the HTTPS remote service URL
    monkeypatch.setenv("APP_URL", "https://voice-orchestrator-prod.a.run.app")
    remote_state = client.get("/api/v1/state").json()
    spotify_item = next(i for i in remote_state["integrations"] if i["name"] == "spotify")
    assert (
        spotify_item["auth"]["redirect_uri"]
        == "https://voice-orchestrator-prod.a.run.app/api/v1/auth/spotify/callback"
    )

    # Verify that having SPOTIFY_REFRESH_TOKEN + Client ID/Secret marks Spotify as ready on remote startup
    monkeypatch.delenv("SPOTIFY_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("SPOTIFY_REFRESH_TOKEN", "refresh-token-xyz")
    ready_state = client.get("/api/v1/state").json()
    spotify_ready = next(i for i in ready_state["integrations"] if i["name"] == "spotify")
    assert spotify_ready["has_credentials"] is True
    assert spotify_ready["status"] == "ready"

    # Verify Cloud Run .env export includes the refresh token and client credentials
    export_res = client.get("/api/v1/auth/export-env")
    assert export_res.status_code == 200
    assert 'SPOTIFY_REFRESH_TOKEN="refresh-token-xyz"' in export_res.json()["dotenv"]

