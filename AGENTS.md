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

1. **Non-Blocking Live Voice Tool Wrapper (`non_blocking_tool`)** — Wraps any async tool ([`app/agent.py`](app/agent.py)) as an async generator so it immediately returns pending status to the Gemini Live stream, executes the coroutine in the background, and injects the completion event alongside a `WHEN_IDLE` scheduling hint so the voice persona narrates results at the next natural pause.
2. **Pluggable Multi-Harness Sandbox Protocol (`CodingHarness` + `SandboxProvisioner`)** — Executes background coding tasks inside a persistent **Vertex AI Agent Engine Sandbox** container ([`app/workers/sandbox.py`](app/workers/sandbox.py)) through a unified `CodingHarness` protocol ([`app/workers/harnesses/base.py`](app/workers/harnesses/base.py)) supporting **ADK Long-Horizon** (`horizon`), **Antigravity CLI** (`antigravity`), and **Claude Code** (`claude`).
3. **Two-Phase Plan → Execute with Git Worktree Isolation & Mid-Task Handoff (`TaskRegistry` + `TaskStore`)** — Isolates every background task in its own git worktree (`.worktrees/<repo>/<task_id>`), gates code changes behind plan/diff approval checkpoints (`mode="plan"` / `require_approval=True`), persists state in a durable SQLite/Postgres ledger ([`app/store/task_store.py`](app/store/task_store.py)), and allows mid-task harness switching (`steer_task(harness=...)`) on the shared worktree.
4. **Scoped A2UI v0.9 Surface Deck (`_build_a2ui_surfaces` + `A2UISurfaceDeck`)** — Maps every tool execution and background task transition to a single active **A2UI v0.9** visual surface (`plan_approval`, `task_trajectory`, `task_outcome`, `context_card`) rendered alongside real-time audio.
5. **Declarative OAuth 2.0 + ADC Workspace Bridge (`app/auth.py`)** — Centralizes authentication (`config/integrations.yaml` + `app/auth.py`) with OAuth 2.0 + PKCE + automatic refresh-token rotation (`SPOTIFY_REFRESH_TOKEN`, `GOOGLE_WORKSPACE_REFRESH_TOKEN`), 1-click `gcloud` ADC scope expansion with `X-Goog-User-Project` quota headers, and dynamic GitHub `@me` fork resolution.
6. **Human + AI-Agent Dual-Mode Onboarding (`provision_sandbox.py`)** — Provides both a guided 5-minute interactive CLI wizard (`make onboard`) and a deterministic, zero-prompt `--non-interactive` mode for coding agents to provision Vertex sandboxes, clone forks, and verify integrations (`make check`).

---

## Recipe table — where to study + how to lift

| # | Interface | Start here (file → symbol) | What to look for | How to lift into your own agent |
|---|---|---|---|---|
| 1 | **Non-blocking Live Voice tool dispatch (`WHEN_IDLE`)** | [`app/agent.py`](app/agent.py) → `non_blocking_tool()` | How the wrapper converts an async function into an async generator, returns immediate pending status, and schedules completion with `WHEN_IDLE`. | Wrap slow tools with `non_blocking_tool()` so `Runner.run_live()` never stalls the audio stream. |
| 2 | **ADK Live FastAPI server & Lifespan** | [`app/fast_api_app.py`](app/fast_api_app.py) → `get_fast_api_app()`, `lifespan` | How ADK mounts the `/run_live` WebSocket alongside custom UI control-plane routes (`app/api_routes.py`). | Use `get_fast_api_app(web=True, lifespan=lifespan)` to serve both native Gemini Live audio and custom application APIs. |
| 3 | **Vertex AI Agent Engine Sandbox provisioner & executor** | [`app/workers/sandbox.py`](app/workers/sandbox.py) → `SandboxWorker._ensure_sandbox()` & [`app/workers/harnesses/base.py`](app/workers/harnesses/base.py) → `exec_in_sandbox()` | How `sandboxEnvironments` are created under a parent `ReasoningEngine`, signed with a JWT (`iam.credentials.signJwt`), probed dynamically, and sent shell commands over HTTPS (`/exec`). | Copy `SandboxWorker` + `SandboxProvisioner` (`app/workers/harnesses/provisioner.py`) to give any agent isolated remote Linux execution. |
| 4 | **Interchangeable Coding Harnesses (`horizon`, `antigravity`, `claude`)** | [`app/workers/harnesses/`](app/workers/harnesses/) → `CodingHarness`, `HorizonA2AHarness`, `AntigravityHarness`, `ClaudeCodeHarness` | How harnesses implement the `CodingHarness` protocol in `app/workers/harnesses/base.py` and register via `get_harness_registry()`. | Implement a new subclass of `CodingHarness` in `app/workers/harnesses/` and register it in `HarnessRegistry` to add another agent CLI. |
| 5 | **Git worktree isolation, Plan gate & task tracking** | [`app/tasks/registry.py`](app/tasks/registry.py) → `TaskRegistry`, [`app/store/task_store.py`](app/store/task_store.py) → `TaskStore` & [`app/tools/task_tools.py`](app/tools/task_tools.py) → `dispatch_task()`, `approve_task()`, `steer_task()` | How git worktrees isolate each task branch (`agent/<task_id>`), how `mode="plan"` pauses at `awaiting_input` or `awaiting_approval`, and how steering resumes execution. | Use per-task git worktrees and approval checkpoints for human-in-the-loop validation over codebases. |
| 6 | **Scoped A2UI v0.9 visual surface builder** | [`app/api_routes.py`](app/api_routes.py) → `_build_a2ui_surfaces()` & [`web/src/components/a2ui/A2UISurfaceDeck.tsx`](web/src/components/a2ui/A2UISurfaceDeck.tsx) | How task states are translated into A2UI v0.9 surface payloads (`plan_approval`, `task_trajectory`, `task_outcome`, `context_card`) and rendered in React. | Generate declarative A2UI JSON structures and map them to interactive mobile cards. |
| 7 | **Declarative OAuth 2.0 + PKCE + Refresh Token & ADC bridge** | [`config/integrations.yaml`](config/integrations.yaml) & [`app/auth.py`](app/auth.py) → `ensure_fresh_access_token()`, `exchange_oauth_code()` | How OAuth flows dynamically resolve local (`127.0.0.1:8000`) vs Cloud Run (`APP_URL`) callbacks, auto-rotate refresh tokens, and attach `X-Goog-User-Project` headers to ADC tokens. | Add a new block to `config/integrations.yaml` and call `ensure_fresh_access_token("<id>")` inside your tool function. |

---

## Beyond the core interfaces

- **Dynamic personal fork discovery (`@me`)** — [`app/tools/integration_tools.py`](app/tools/integration_tools.py) (`github_operations`) queries `GET /user` with your `GH_TOKEN` at runtime to inspect both upstream repositories and your personal forks (`<authenticated_user>/<repo>`) plus authored PRs (`author:<authenticated_user>`) with zero hardcoded usernames.
- **Zero-test-code production purity (`tests/fakes.py` + `httpx.MockTransport`)** — Production modules under `app/` contain zero test-mode branches (`PYTEST_CURRENT_TEST`, `ADK_EVAL_MODE`) or canned fallback payloads. All test doubles (`FakeHarness`, `exec_transport`, `a2a_transport`, `integrations_transport`) live strictly under `tests/fakes.py` and are injected at the HTTP transport and harness registry boundaries.
- **Async TaskStore hydration & situational briefing** — [`app/agent.py`](app/agent.py) (`build_orchestrator_instruction`) asynchronously hydrates active and recently completed tasks from `TaskStore` (`await get_task_registry().list_all()`) before each session turn without bloating the voice prompt.

---

## Study order

If you are reading the codebase from scratch, follow this path (~20 minutes):

1. **[`app/agent.py`](app/agent.py)** — Read `build_orchestrator_instruction`, `non_blocking_tool`, and `root_agent` to see how the voice orchestrator separates spoken brevity from visual A2UI rendering.
2. **[`app/workers/sandbox.py`](app/workers/sandbox.py) & [`app/workers/harnesses/`](app/workers/harnesses/)** — See how commands execute remotely inside the Vertex AI Agent Engine Sandbox across `horizon`, `antigravity`, and `claude`.
3. **[`app/tasks/registry.py`](app/tasks/registry.py) & [`app/store/task_store.py`](app/store/task_store.py)** — Trace a task from `dispatch_task(mode="plan")` → `awaiting_input` / `awaiting_approval` → `approve_task()` or `steer_task()`.
4. **[`app/auth.py`](app/auth.py) & [`config/integrations.yaml`](config/integrations.yaml)** — See how OAuth 2.0 PKCE, refresh tokens, and `gcloud` ADC tokens are managed and persisted.
5. **[`web/src/App.tsx`](web/src/App.tsx) & [`web/src/components/a2ui/A2UISurfaceDeck.tsx`](web/src/components/a2ui/A2UISurfaceDeck.tsx)** — See how the React frontend renders the active A2UI surface card alongside the bottom control sheets.

---

# Maintaining the code

## Project Layout

```text
adk-sonar/
├── app/
│   ├── agent.py                 # Root ADK Gemini Live agent (`root_agent`), `non_blocking_tool`, and async instruction
│   ├── fast_api_app.py          # FastAPI entrypoint, lifespan, native ADK `/run_live` WebSocket, static UI host
│   ├── api_routes.py            # REST endpoints (`/api/v1/state`, `/api/v1/tasks`, `/api/v1/auth/*`, A2UI surfaces)
│   ├── auth.py                  # Unified OAuth 2.0 + PKCE + Refresh Token + CLI ADC manager
│   ├── integrations.py          # Integration registry, specs, and MCP toolset generation
│   ├── store/                   # Durable SQLAlchemy async TaskStore (`models.py`, `task_store.py`, `reconciler.py`)
│   ├── tasks/                   # `TaskRegistry`, `TaskHandle`, persistence bridge, and concurrency limits
│   ├── app_utils/
│   │   ├── http_client.py       # Shared async HTTP client factory with transport injection seam
│   │   └── services.py          # ADK session, memory & artifact service factory (`shared://session`, `shared://artifact`)
│   ├── tools/
│   │   ├── grounding_tools.py   # Grounded search (Google Search, Google Maps Places)
│   │   ├── integration_tools.py # Downstream APIs: Workspace (Calendar, Gmail, Drive), Slack, GitHub, Spotify
│   │   ├── task_tools.py        # Voice tools: `dispatch_task`, `steer_task`, `approve_task`, `watch_tasks`
│   │   └── workspace_tools.py   # Repository inspection, cloning, file reading
│   └── workers/
│       ├── base.py              # `WorkerBackend` protocol & `WorkerExecutionResult`
│       ├── factory.py           # Worker backend selector (`sandbox` vs `local`)
│       ├── local.py             # `LocalWorker` subprocess runner
│       ├── sandbox.py           # `SandboxWorker` (Vertex AI Agent Engine Sandbox `/exec`)
│       └── harnesses/           # Interchangeable harnesses (`base.py`, `provisioner.py`, `claude.py`, `antigravity.py`, `horizon.py`, `registry.py`)
├── config/
│   ├── integrations.yaml        # Declarative integration metadata, OAuth endpoints, scopes, and setup steps
│   ├── workspaces.yaml          # Default tracked repositories, demo tasks, and sandbox package manifest
│   └── workspaces.local.yaml    # Local repository overrides
├── scripts/
│   ├── provision_sandbox.py     # Interactive + `--non-interactive` onboarding wizard & `--check-only` verifier
│   └── setup.py                 # Environment verification & installation helper
├── web/                         # Mobile-first React + TypeScript + Tailwind + Vite PWA
│   ├── src/
│   │   ├── App.tsx              # Main orchestrator layout, header status pills, and audio visualizer
│   │   ├── components/a2ui/     # `A2UISurfaceDeck.tsx` rendering scoped A2UI v0.9 visual cards
│   │   ├── components/sheets/   # `ConnectionsSheet.tsx`, `FleetTasksSheet.tsx`
│   │   └── lib/                 # `live-audio-client.ts`, `api.ts`
├── tests/                       # Unit, integration, eval test suites, and `tests/fakes.py` network/protocol doubles
├── Makefile                     # Developer workflow shortcuts (`onboard`, `check`, `sandbox`, `dev`, `test`, `eval`, `deploy`)
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
