.PHONY: onboard dev check deploy sync-secrets provision seed setup setup-workspace discover test eval eval-routing playground web

# ==============================================================================
# 1. CORE 3-STEP WORKFLOW (Onboard -> Dev -> Deploy)
# ==============================================================================

# Step 1: One-command interactive onboarding (installs deps, enables GCP APIs/IAM, authenticates Workspace/GitHub/Spotify/Slack, configures forks, provisions Vertex Sandbox, and verifies all endpoints)
onboard:
	uv sync
	@[ -d web/node_modules ] || npm --prefix web install
	uv run python scripts/provision_sandbox.py --onboard

# Step 2: Start local FastAPI backend (:8000) + Mobile Gemini Live Web UI (:3000)
dev:
	@[ -d web/node_modules ] || npm --prefix web install
	(uv run uvicorn app.fast_api_app:app --host 0.0.0.0 --port 8000 --reload & cd web && npm run dev)

# Step 3: Deploy to Cloud Run AND automatically sync configured .env OAuth refresh tokens & secrets
deploy:
	agents-cli deploy \
		--project $${GOOGLE_CLOUD_PROJECT} \
		--region us-central1 \
		--timeout 3600 \
		--min-instances 1 \
		--no-confirm-project
	uv run python scripts/provision_sandbox.py --sync-cloud-run

# ==============================================================================
# 2. DIAGNOSTICS, MAINTENANCE & CI
# ==============================================================================

# Live Doctor Scorecard: verify GCP, Vertex Sandbox, Seeded Repos, and live API auth for all Connected Apps
check:
	uv run python scripts/provision_sandbox.py --check

# Push updated local .env OAuth refresh tokens & secrets to Cloud Run (without rebuilding container)
sync-secrets:
	uv run python scripts/provision_sandbox.py --sync-cloud-run

# Non-interactive / CI sandbox provisioning & harness warmup
provision:
	uv sync
	@[ -d web/node_modules ] || npm --prefix web install
	uv run python scripts/provision_sandbox.py

# Run unit and integration tests
test:
	uv run pytest tests/unit tests/integration

# Run ADK Live evaluation suites
eval:
	agents-cli eval run --mode adk_live

eval-routing:
	agents-cli eval run --mode adk_live --dataset tests/eval/datasets/routing-dataset.json

# ==============================================================================
# 3. GRANULAR HELPERS
# ==============================================================================

setup:
	uv sync
	@[ -d web/node_modules ] || npm --prefix web install
	uv run python scripts/setup.py

seed:
	uv run python scripts/provision_sandbox.py --seed-only

discover:
	uv run python scripts/provision_sandbox.py --discover

setup-workspace:
	uv run python scripts/setup.py --clone-repos

web:
	@[ -d web/node_modules ] || npm --prefix web install
	cd web && npm run dev

playground:
	agents-cli playground


