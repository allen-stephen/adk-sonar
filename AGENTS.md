# ADK Sonar — Agent & Developer Guide (`AGENTS.md`)

This guide has two parts:

1. **[Studying the recipe](#studying-the-recipe)** — If you're reading this repo (or pointing a coding agent at it) to understand how **ADK Sonar** combines bidirectional Gemini Live audio (`run_live`), non-blocking background tool scheduling (`WHEN_IDLE`), interchangeable coding harnesses (`horizon`, `antigravity`, `claude`) inside a shared Vertex AI Agent Engine Sandbox, and scoped A2UI v0.9 visual cards, start here.
2. **[Maintaining the code](#maintaining-the-code)** — If you're modifying this repo, troubleshooting OAuth / Vertex Sandbox connections, or running evaluations and deployments, jump to Part 2.

---

# Studying the recipe

## What this recipe teaches

Voice agents break down when asked to perform real engineering or productivity work because **voice is synchronous and ephemeral**, whereas **coding and multi-API orchestration are asynchronous and state-heavy**:

- If a tool call blocks for 45 seconds while running a coding harness or git diff, the Gemini Live audio stream stalls or times out.
- If an agent dumps a 60-line unified diff or 10 pull requests into a voice transcript, the spoken output becomes unusable.
- If a user switches from planning to execution—or switches coding harnesses mid-task—without a shared filesystem contract, context is lost.

**ADK Sonar** solves these challenges with six composable interfaces on top of Google ADK (`Runner.run_live()`):

1. **Non-Blocking Live Voice Tool Wrapper (`@non_blocking_tool`)** — Wraps any async tool (`app/tools/async_wrapper.py`) so it immediately yields `{status: "RUNNING", task_id: ...}` to the Gemini Live stream, registers the coroutine in an `AsyncTaskRegistry`, and injects the completion event alongside a `WHEN_IDLE` scheduling hint so the voice persona narrates results at the next natural pause.
2. **Pluggable Multi-Harness Sandbox Protocol (`CodingHarness` + `SandboxProvisioner`)** — Executes background coding tasks inside a persistent **Vertex AI Agent Engine Sandbox** container (`app/workers/sandbox.py`) through a unified `CodingHarness` interface (`app/workers/harnesses.py`) supporting **ADK Long-Horizon** (`horizon`), **Antigravity CLI** (`antigravity`), and **Claude Code** (`claude`).
3. **Two-Phase Plan → Execute with Git Worktree Isolation & Mid-Task Handoff (`TaskWorker`)** — Isolates every background task in its own git worktree (`workspaces/<repo>/.worktrees/<task_id>`), gates code changes behind a `PLAN.md` approval step (`mode="plan"` → `approve_and_execute`), and allows mid-task harness switching (`switch_harness`) because `PLAN.md`, `PROGRESS.md`, and commits persist in the shared worktree.
4. **Scoped A2UI v0.9 Surface Deck (`a2ui_emitter` + `A2UISurfaceDeck`)** — Maps every tool execution and background task transition to a single active **A2UI v0.9** visual surface (`TaskStatusCard`, `PlanReviewCard`, `GitHubPRCard`, `WorkspaceDigestCard`, `CalendarAgendaCard`, `SpotifyPlayerCard`, `PlaceCard`) pushed over WebSockets alongside PCM audio.
5. **Declarative OAuth 2.0 + ADC Workspace Bridge (`IntegrationAuthManager`)** — Centralizes authentication (`config/integrations.yaml` + `app/auth.py`) with OAuth 2.0 + PKCE + automatic refresh-token rotation (`SPOTIFY_REFRESH_TOKEN`, `GOOGLE_WORKSPACE_REFRESH_TOKEN`), 1-click `gcloud` ADC scope expansion with `X-Goog-User-Project` quota headers, and dynamic GitHub `@me` fork resolution.
6. **Human + AI-Agent Dual-Mode Onboarding (`provision_sandbox.py`)** — Provides both a guided 5-minute interactive CLI wizard (`make onboard`) and a deterministic, zero-prompt `--non-interactive` mode for coding agents to provision Vertex sandboxes, clone forks, and verify integrations (`make check`).

---

## Recipe table — where to study + how to lift

| # | Interface | Start here (file → symbol) | What to look for | How to lift into your own agent |
|---|---|---|---|---|
| 1 | **Non-blocking Live Voice tool dispatch (`WHEN_IDLE`)** | [`app/tools/async_wrapper.py`](app/tools/async_wrapper.py) → `non_blocking_tool()`, `AsyncTaskRegistry` | How the wrapper spawns an `asyncio.Task`, returns `{status: "RUNNING"}` in `<5ms`, and fires `on_task_complete` with `"scheduling": "WHEN_IDLE"` when finished. | Copy `app/tools/async_wrapper.py` and decorate any slow API or sub-agent tool with `@non_blocking_tool` so `Runner.run_live()` never stalls audio. |
| 2 | **Bidirectional WebSocket voice + event multiplexer** | [`app/main.py`](app/main.py) → `websocket_endpoint()`, `_forward_adk_events()` | How binary PCM 16kHz microphone frames feed `LiveRequestQueue.send_realtime()`, while ADK audio chunks, transcripts, and A2UI JSON cards multiplex onto a single WebSocket. | Reuse the `LiveRequestQueue` + `RunConfig(response_modalities=["AUDIO"])` loop in `app/main.py` for any custom web/mobile voice client. |
| 3 | **Vertex AI Agent Engine Sandbox provisioner & executor** | [`app/workers/sandbox.py`](app/workers/sandbox.py) → `SandboxProvisioner.ensure_sandbox()`, `execute_command()` | How `sandboxEnvironments` are created under a parent `ReasoningEngine`, signed with a JWT (`iam.credentials.signJwt`), and sent shell commands over HTTPS (`/execute`). | Copy `SandboxProvisioner` from `app/workers/sandbox.py` and `scripts/provision_sandbox.py` to give any agent isolated remote Linux execution. |
| 4 | **Interchangeable Coding Harnesses (`horizon`, `antigravity`, `claude`)** | [`app/workers/harnesses.py`](app/workers/harnesses.py) → `CodingHarness`, `HorizonHarness`, `AntigravityHarness`, `ClaudeCodeHarness` | How all three harnesses implement `execute(instruction, workspace_path, sandbox_id, mode, prior_context)` and read/write `PLAN.md` and `PROGRESS.md` inside the sandbox worktree. | Implement a new subclass of `CodingHarness` in `app/workers/harnesses.py` and register it in `HARNESS_REGISTRY` to add another CLI agent (e.g., Gemini CLI, Aider, Codex). |
| 5 | **Git worktree isolation, Plan gate & mid-task harness switch** | [`app/workers/task_worker.py`](app/workers/task_worker.py) → `TaskWorker.dispatch_task()`, `approve_and_execute()`, `switch_harness()` | How `git worktree add -B task/<id>` isolates each run, how `mode="plan"` pauses at `awaiting_approval`, and how `switch_harness()` passes `PLAN.md` + `git status` to the new harness. | Lift `TaskWorker` whenever you want human-in-the-loop plan approval and hot-swappable worker backends over a shared git repo. |
| 6 | **Scoped A2UI v0.9 visual surface emitter** | [`app/callbacks/a2ui_emitter.py`](app/callbacks/a2ui_emitter.py) → `emit_surface_for_tool()` & [`web/src/components/a2ui/A2UISurfaceDeck.tsx`](web/src/components/a2ui/A2UISurfaceDeck.tsx) | How tool outputs are translated into A2UI v0.9 payloads (`createSurface`, `updateComponents`, `updateDataModel`) and rendered as interactive mobile cards. | Add a surface builder in `app/callbacks/a2ui_emitter.py` and a matching card component in `A2UISurfaceDeck.tsx` for your own domain entities. |
| 7 | **Declarative OAuth 2.0 + PKCE + Refresh Token & ADC bridge** | [`config/integrations.yaml`](config/integrations.yaml) & [`app/auth.py`](app/auth.py) → `IntegrationAuthManager` | How OAuth flows dynamically resolve local (`127.0.0.1:8000`) vs Cloud Run (`APP_URL`) callbacks, auto-rotate refresh tokens, and attach `X-Goog-User-Project` headers to ADC tokens. | Add a new block to `config/integrations.yaml` and call `auth_manager.get_token("<id>")` inside your tool function. |

---

## Beyond the core interfaces

- **Dynamic personal fork discovery (`@me`)** — [`app/tools/integration_tools.py`](app/tools/integration_tools.py) (`github_operations`) queries `GET /user` with your `GH_TOKEN` at runtime to inspect both upstream repositories and your personal forks (`<authenticated_user>/<repo>`) plus authored PRs (`author:<authenticated_user>`) with zero hardcoded usernames.
- **Strict separation of live vs evaluation fixtures** — All tool functions in [`app/tools/integration_tools.py`](app/tools/integration_tools.py) gate deterministic test fixtures behind `_is_test_or_eval_mode()` (`PYTEST_CURRENT_TEST` or `ADK_EVAL_MODE`), ensuring live voice sessions always hit real APIs or return actionable remediation cards.
- **Context caching & state synchronization** — [`app/callbacks/context_sync.py`](app/callbacks/context_sync.py) injects active task statuses, recent git branches, and connected app readiness into the ADK session state before each model turn without bloating the voice prompt.

---

## Study order

If you are reading the codebase from scratch, follow this path (~20 minutes):

1. **[`app/agent.py`](app/agent.py)** — Read `SYSTEM_INSTRUCTION` and `root_agent` to see the 13 registered tools and how the voice persona (`Charon`) separates spoken brevity from visual A2UI rendering.
2. **[`app/tools/async_wrapper.py`](app/tools/async_wrapper.py)** — See how `@non_blocking_tool` decouples slow tool execution from the real-time Gemini Live audio loop.
3. **[`app/workers/sandbox.py`](app/workers/sandbox.py) & [`app/workers/harnesses.py`](app/workers/harnesses.py)** — See how commands execute remotely inside the Vertex AI Agent Engine Sandbox across `horizon`, `antigravity`, and `claude`.
4. **[`app/workers/task_worker.py`](app/workers/task_worker.py)** — Trace a task from `dispatch_task(mode="plan")` → `awaiting_approval` → `approve_and_execute()` or `switch_harness()`.
5. **[`app/auth.py`](app/auth.py) & [`config/integrations.yaml`](config/integrations.yaml)** — See how OAuth 2.0 PKCE, refresh tokens, and `gcloud` ADC tokens are managed and persisted.
6. **[`web/src/App.tsx`](web/src/App.tsx) & [`web/src/components/a2ui/A2UISurfaceDeck.tsx`](web/src/components/a2ui/A2UISurfaceDeck.tsx)** — See how the React frontend renders the active A2UI surface card alongside the bottom control sheets.

---

# Maintaining the code

## Project Layout

```text
adk-sonar/
├── app/
│   ├── agent.py                 # Root ADK Gemini Live agent (`root_agent`) & voice persona instructions
│   ├── main.py                  # FastAPI entrypoint, WebSocket `/ws/live` audio+A2UI multiplexer, static SPA host
│   ├── api_routes.py            # REST endpoints (`/api/v1/tasks`, `/api/v1/workspaces`, `/api/v1/integrations`, `/api/v1/auth/*`)
│   ├── auth.py                  # Unified OAuth 2.0 + PKCE + Refresh Token + CLI ADC manager (`IntegrationAuthManager`)
│   ├── callbacks/
│   │   ├── a2ui_emitter.py      # Translates tool outputs & task transitions into A2UI v0.9 surface payloads
│   │   ├── context_sync.py      # Synchronizes background task & workspace state into ADK session state
│   │   └── persistence.py       # Session & artifact persistence callbacks
│   ├── tools/
│   │   ├── async_wrapper.py     # `@non_blocking_tool` decorator & `AsyncTaskRegistry` (`WHEN_IDLE` scheduling)
│   │   ├── coding_tools.py      # `dispatch_coding_task`, `switch_task_harness`, `manage_workspace`, `git_operations`
│   │   └── integration_tools.py # Live REST tools: Google Workspace, Search, Maps, GitHub, Spotify, Slack
│   └── workers/
│       ├── sandbox.py           # `SandboxProvisioner` for Vertex AI Agent Engine Sandboxes (`sandboxEnvironments`)
│       ├── harnesses.py         # `HorizonHarness`, `AntigravityHarness`, `ClaudeCodeHarness` implementations
│       └── task_worker.py       # Git worktree isolation, `PLAN.md` approval gate, and mid-task harness switching
├── config/
│   ├── integrations.yaml        # Declarative integration metadata, OAuth endpoints, scopes, and setup steps
│   ├── workspaces.yaml          # Default tracked repository definitions
│   └── settings.yaml            # Runtime model, voice persona, and sandbox timeout configuration
├── scripts/
│   ├── provision_sandbox.py     # Interactive + `--non-interactive` onboarding wizard & `--check-only` verifier
│   └── sync_cloud_run_env.py    # Pushes local `.env` secrets & `APP_URL` to Google Cloud Run
├── web/                         # Mobile-first React + TypeScript + Tailwind + Vite PWA
│   ├── public/assets/           # Branding assets (`adk-sonar.png`)
│   └── src/
│       ├── App.tsx              # Main orchestrator layout, header status pills, and audio visualizer
│       ├── components/a2ui/     # `A2UISurfaceDeck.tsx` rendering scoped A2UI v0.9 visual cards
│       └── components/sheets/   # `TasksSheet.tsx`, `WorkspacesSheet.tsx`, `ConnectionsSheet.tsx`, `TranscriptSheet.tsx`
├── tests/                       # Unit & integration test suites (`pytest`)
├── Makefile                     # Developer workflow shortcuts (`onboard`, `check`, `sandbox`, `dev`, `test`, `deploy`)
└── .env.example                 # Documented environment template
```

---

## Development & Verification Commands

| Command | Purpose |
|---|---|
| `make onboard` | Interactive 5-minute setup wizard: configures GCP project, provisions Vertex Sandbox, clones forks, and connects integrations |
| `make check` | Runs non-interactive health check (`--check-only`) verifying Vertex AI, Sandbox reachability, and all 6 integrations |
| `make sandbox` | Creates or refreshes the remote **Vertex AI Agent Engine Sandbox** container (`VERTEX_SANDBOX_ID`) |
| `make dev` | Starts both the FastAPI WebSocket backend (`:8000`) and Vite React UI (`:5173`) with hot reload |
| `make test` | Runs unit and integration tests (`uv run pytest tests/unit tests/integration`) |
| `make deploy` | Builds the multi-stage Docker image and deploys to **Google Cloud Run** (`us-central1`) |
| `make sync-secrets` | Pushes local `.env` tokens and `APP_URL` to the deployed Cloud Run service |

---

## Non-Interactive AI Agent Onboarding

If you are an AI coding agent (Claude Code, Gemini CLI, Antigravity) setting up or verifying this repository on behalf of a user, **never run interactive prompts that block on stdin**. Use the `--non-interactive` flags built into `scripts/provision_sandbox.py`:

```bash
# 1. Inspect current environment & live API readiness without modifying files
uv run python scripts/provision_sandbox.py --non-interactive --check-only

# 2. Auto-detect gcloud project + gh auth, provision Vertex AI Sandbox, and clone forks without prompts
uv run python scripts/provision_sandbox.py --non-interactive \
  --project "$(gcloud config get-value project)" \
  --location us-central1
```

---

## Troubleshooting & Maintenance Reference

| Symptom / Error | Root Cause | Resolution |
|---|---|---|
| **Google Workspace returns `403 ACCESS_TOKEN_SCOPE_INSUFFICIENT` or `PERMISSION_DENIED` on Calendar / Gmail / Drive** | Default `gcloud auth application-default login` only requests Cloud Platform scopes, or Google Workspace APIs require a quota project header (`X-Goog-User-Project`) when called with ADC tokens. | 1. Enable APIs: `gcloud services enable calendar-json.googleapis.com gmail.googleapis.com drive.googleapis.com`<br>2. Click **Auto-Detect Local CLI** in the Google Workspace card (or run `gcloud auth application-default login --scopes=https://www.googleapis.com/auth/cloud-platform,https://www.googleapis.com/auth/calendar,https://www.googleapis.com/auth/gmail.modify,https://www.googleapis.com/auth/drive.readonly`).<br>3. `app/auth.py` (`get_workspace_headers()`) automatically attaches `X-Goog-User-Project: <GOOGLE_CLOUD_PROJECT>`. |
| **`gcloud auth application-default login` warns that `calendar` / `drive.readonly` scopes may be blocked on the default gcloud client ID** | Google Cloud restricts sensitive Workspace scopes on the shared default `gcloud` OAuth client ID in some organizations. | Create a **Web application** OAuth 2.0 Client ID in [GCP Credentials](https://console.cloud.google.com/apis/credentials) with redirect URI `http://127.0.0.1:8000/api/v1/auth/google_workspace/callback`, add `GOOGLE_WORKSPACE_CLIENT_ID` and `GOOGLE_WORKSPACE_CLIENT_SECRET` to `.env`, and click **Sign in with Google Workspace (OAuth 2.0)** in the app UI. |
| **Spotify OAuth fails with `INVALID_CLIENT: Invalid redirect URI`** | Spotify deprecated `localhost` redirect URIs in 2025 and strictly requires explicit IPv4 loopback (`127.0.0.1`) for local development. | In your [Spotify Developer Dashboard](https://developer.spotify.com/dashboard), add `http://127.0.0.1:8000/api/v1/auth/spotify/callback` (and `https://<your-cloud-run-url>/api/v1/auth/spotify/callback` for remote). Access the local UI via `http://127.0.0.1:5173`. |
| **Spotify connects (`✓ VERIFIED`) but voice play/pause commands do not start audio** | Spotify Web API (`PUT /v1/me/player/play`) requires a **Spotify Premium** account and at least one active Spotify client (phone, desktop app, or web player) awake on your account. | Open the Spotify app on your phone or laptop so it registers as an active device, then ask ADK Sonar to play a track. |
| **Vertex AI Sandbox returns `502 Bad Gateway` or `No healthy upstream`** | Vertex AI Agent Engine Sandbox containers have a finite TTL (default 2 hours) or may be evicted after extended idle periods. | Run `make sandbox` (or click **Provision Sandbox** in the Workspaces sheet). `SandboxProvisioner.ensure_sandbox()` also automatically detects unreachable sandboxes (`404`/`502`/`503`) and provisions a replacement container. |
| **Vertex AI Sandbox provisioning fails with `Permission 'iam.serviceAccounts.signJwt' denied`** | Generating the `X-Goog-Usr-Token` JWT for the Vertex Sandbox HTTPS proxy requires the `Service Account Token Creator` role on your project's default compute service account. | Run:<br>`gcloud iam service-accounts add-iam-policy-binding $(gcloud projects describe $(gcloud config get-value project) --format='value(projectNumber)')-compute@developer.gserviceaccount.com --member="user:$(gcloud config get-value account)" --role="roles/iam.serviceAccountTokenCreator"` |
| **Model `404 Not Found` on `gemini-live-2.5-flash-native-audio`** | The Gemini Live native audio model on Vertex AI is hosted in `us-central1`. | Ensure `GOOGLE_CLOUD_LOCATION=us-central1` in `.env` (never change the model name unless instructed). |
