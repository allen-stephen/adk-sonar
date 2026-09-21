# ADK Sonar — Voice Orchestrator

Voice-driven engineering and workspace orchestrator built on Google ADK Live (`gemini-3.8-live`). It supervises concurrent coding agents inside a **shared persistent Vertex Agent Platform Sandbox** using isolated per-task git worktrees across three interchangeable coding harnesses:

* **ADK Long Horizon** (`horizon`) — In-sandbox A2A server (`uvicorn horizon.fast_api_app:app --port 8081`) consumed via `RemoteA2aAgent`
* **Antigravity** (`antigravity`) — DeepMind agentic harness (`agy -p`) via Sandbox `/exec`
* **Claude Code** (`claude`) — Anthropic headless CLI (`claude -p`) via Sandbox `/exec`

---

## The 3-Command Workflow (`onboard` → `dev` → `deploy`)

| Step | Command | Time | What It Does |
| :--- | :--- | :--- | :--- |
| **1. Onboard** | `make onboard` | ~3 min | Installs Python + Web deps, enables GCP APIs (`aiplatform`, `calendar`, `gmail`, `drive`, `iamcredentials`), authenticates **Google Workspace** + **GitHub** + optional **Spotify**/**Slack**, configures your personal GitHub forks in `config/workspaces.local.yaml`, provisions the remote **Vertex Agent Engine Sandbox**, and runs the live API verification scorecard. |
| **2. Test Locally** | `make dev` | ~5 sec | Starts the FastAPI control plane (`http://127.0.0.1:8000`) and Mobile Gemini Live + A2UI Web UI (`http://localhost:3000`). |
| **3. Deploy Remotely** | `make deploy` | ~3 min | Deploys the container to **Cloud Run** with WebSocket session affinity (`timeout=3600s`, `min-instances=1`) **and automatically syncs** your `.env` OAuth refresh tokens (`SPOTIFY_REFRESH_TOKEN`, `GOOGLE_WORKSPACE_REFRESH_TOKEN`, `GITHUB_PERSONAL_ACCESS_TOKEN`, `SLACK_BOT_TOKEN`) to the remote service. |

*(At any time, run **`make check`** to run the live diagnostic scorecard across your GCP project, remote Vertex Sandbox, seeded repositories, and all connected apps.)*

---

## Step 1: First-Time Local Setup (5 Minutes)

### Prerequisites
* Python 3.12+ and [`uv`](https://docs.astral.sh/uv/)
* Node.js 20+ (`npm`)
* Google Cloud SDK (`gcloud auth login`) and GitHub CLI (`gh auth login`)

### Run Interactive Onboarding
```bash
make onboard
```

During `make onboard`, the wizard automatically handles four things that normally trip up developers:
1. **GCP API & IAM Auto-Configuration:** Enables `aiplatform.googleapis.com`, `iamcredentials.googleapis.com`, `calendar-json.googleapis.com`, `gmail.googleapis.com`, and `drive.googleapis.com` on your project, and binds `roles/serviceusage.serviceUsageConsumer` (required for `X-Goog-User-Project` quota headers on Workspace APIs) and `roles/iam.serviceAccountTokenCreator` (required to mint JWTs for the Vertex Sandbox load balancer).
2. **Zero-Console Google Workspace Auth (`Calendar`, `Gmail`, `Drive`):** Launches `gcloud auth application-default login` with Calendar, Gmail, and Drive scopes, extracts the resulting OAuth `client_id`, `client_secret`, and `refresh_token` into `.env`, and verifies `200 OK` against Google Calendar and Gmail—no manual OAuth Client ID creation in GCP Console required.
3. **GitHub Profile & Personal Fork Discovery:** Queries your `gh` profile to discover your personal forks (`adk-python`, `adk-samples`, `adk-docs`, `adk-web`, etc.), writes `config/workspaces.local.yaml` (gitignored) with your fork as `origin` and `google/*` as `upstream`, and seeds those repositories both locally and inside `/workspace` on the remote Vertex Agent Engine Sandbox.
4. **Optional Spotify & Slack Setup:**
   * **Spotify (`OAuth 2.0 + PKCE`):** If you want voice music playback control, create an app at [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard) (requires **Spotify Premium** on the app owner account), add Redirect URI `http://127.0.0.1:8000/api/v1/auth/spotify/callback`, and enter your `Client ID` & `Client Secret` when prompted (or later in the UI's **Configuration** sheet).
   * **Slack (`Bot Token`):** Paste a `xoxb-...` Bot User OAuth Token from [api.slack.com/apps](https://api.slack.com/apps) (the backend automatically calls `slack.com/api/auth.test` to verify and populate `SLACK_TEAM_ID`).

---

## Step 2: Launch & Validate Locally

```bash
make dev
```

Open **[http://localhost:3000](http://localhost:3000)** and try these end-to-end validation prompts over voice (or starter chips):
* **Google Workspace:** *"What's on my Google Calendar tomorrow?"* / *"Check my unread Gmail messages."* / *"Search my Google Drive for recent docs."*
* **GitHub (Personal Forks & Upstream):** *"What open pull requests do I have on GitHub?"* / *"What feature branches are on my fork of adk-python?"*
* **Coding Harnesses & Shared Sandbox:** *"Have Claude plan an upgrade to JWT token rotation in auth-svc."* → Approve the plan card on the A2UI stage or switch harnesses (`Horizon` / `Antigravity` / `Claude Code`) in the **Configuration** sheet.
* **Spotify:** *"What song is currently playing on Spotify?"* / *"Play Tycho on Spotify."*

---

## Step 3: Deploy to Cloud Run (Remote Access)

Because a remote Cloud Run container cannot access your laptop's local `gcloud` or `gh` CLI sessions, ADK Sonar uses **persisted OAuth Refresh Tokens** (`GOOGLE_WORKSPACE_REFRESH_TOKEN`, `SPOTIFY_REFRESH_TOKEN`) and token credentials synced from `.env`:

```bash
make deploy
```

What `make deploy` does:
1. Builds and deploys the service to Cloud Run (`agents-cli deploy --timeout 3600 --min-instances 1`).
2. Automatically runs `uv run python scripts/provision_sandbox.py --sync-cloud-run` (`make sync-secrets`), which pushes your verified `.env` refresh tokens, quota project (`GOOGLE_WORKSPACE_QUOTA_PROJECT`), and API tokens to the Cloud Run revision.
3. On Cloud Run startup (`app/fast_api_app.py`), the container uses `GOOGLE_WORKSPACE_REFRESH_TOKEN` and `SPOTIFY_REFRESH_TOKEN` to mint fresh access tokens automatically so your remote deployment stays authenticated continuously without expiring after 60 minutes.

*(If you ever rotate a token or connect a new app locally, run **`make sync-secrets`** to push the updated `.env` secrets to Cloud Run in ~10 seconds without rebuilding the image.)*

---

## Maintenance & Troubleshooting Guide

Run **`make check`** at any time to execute live API probes against every integration:

```bash
make check
```

| Symptom / Error | Root Cause | Resolution |
| :--- | :--- | :--- |
| **Google Calendar / Gmail / Drive returns `403 Insufficient Permission` or `Quota project required`** | Default `gcloud` credentials only carry `cloud-platform` scope, or `X-Goog-User-Project` lacks `serviceUsageConsumer` / enabled APIs. | Toggle **Google Workspace** in the UI (or run `make onboard`), which runs `gcloud auth application-default login` with Calendar/Gmail/Drive scopes and sets `GOOGLE_WORKSPACE_QUOTA_PROJECT`. |
| **Spotify returns `403 Forbidden: Active premium subscription required`** | Spotify's Web API requires the Spotify account that created the Client ID in `developer.spotify.com/dashboard` to have an active **Spotify Premium** subscription. | Ensure the app-owner Spotify account has Premium, and ensure Redirect URI is `http://127.0.0.1:8000/api/v1/auth/spotify/callback` (Spotify rejects `localhost` for new loopback apps). |
| **Spotify returns `404 No active device found` when playing music** | Spotify's Web API requires at least one open Spotify client (phone, desktop, or web player) to transfer or start playback. | Open the Spotify app on your phone or computer and play/pause any track once so Spotify registers an active device. |
| **Vertex Sandbox `/exec` returns `502 Bad Gateway` or `403 Forbidden on signJwt`** | `502` means a prior sandbox container reached its idle/TTL timeout; `403` on `signJwt` means your user lacks `roles/iam.serviceAccountTokenCreator` on `SANDBOX_CALLER_SA`. | `SandboxWorker` automatically evicts `502` sandboxes and creates a fresh container. Run `make provision` to auto-grant `roles/iam.serviceAccountTokenCreator` and seed a fresh sandbox. |
| **Remote Cloud Run deployment is missing an integration you connected locally** | New tokens saved to local `.env` haven't been pushed to Cloud Run env vars yet. | Run `make sync-secrets` (or click **Copy Cloud Run .env** in the Configuration sheet). |

---

## Command Reference

| Command | Purpose |
| :--- | :--- |
| `make onboard` | Interactive 1-command onboarding (deps, GCP APIs/IAM, Workspace/GitHub/Spotify/Slack auth, forks, Vertex Sandbox) |
| `make check` | Run the live API & Vertex Sandbox verification scorecard |
| `make dev` | Start local FastAPI backend (`:8000`) + Mobile Gemini Live Web UI (`:3000`) |
| `make deploy` | Deploy to Cloud Run and automatically sync `.env` OAuth refresh tokens & secrets |
| `make sync-secrets` | Push updated `.env` credentials/refresh tokens to Cloud Run without redeploying code |
| `make provision` | Non-interactive sandbox provisioning and harness warm-up |
| `make test` | Run unit and integration test suites (`uv run pytest tests/unit tests/integration`) |
| `make eval` | Run the ADK Live multi-turn evaluation suite (`agents-cli eval run --mode adk_live`) |
