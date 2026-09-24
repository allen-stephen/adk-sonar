.PHONY: onboard dev check deploy sync-secrets provision seed setup setup-workspace discover test eval playground web

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
	uv run python scripts/provision_sandbox.py --non-interactive

# Alias to reprovision the live Vertex AI Agent Engine Sandbox with pre-installed skills & GCP auth
sandbox:
	uv run python scripts/provision_sandbox.py --non-interactive

# Build and push the custom Vertex Sandbox container image (Dockerfile.sandbox) to Artifact Registry
sandbox-image:
	gcloud builds submit --tag us-central1-docker.pkg.dev/$${GOOGLE_CLOUD_PROJECT}/sonar/sandbox:latest -f Dockerfile.sandbox .

# Run unit and integration tests
test:
	uv run pytest tests/unit tests/integration

# Run ADK Live evaluation suite (override dataset with: make eval DATASET=tests/eval/datasets/coding-tasks.json)
EVAL_DB_URL ?= sqlite+aiosqlite:////tmp/sonar_eval_tasks.db
DATASET ?= tests/eval/datasets/basic-dataset.json
HARNESS ?= claude
SANDBOX_DATASET ?= tests/eval/sandbox/sandbox_dataset.json

eval:
	TASK_DB_URL=$(EVAL_DB_URL) uv run python tests/eval/seed_eval_store.py
	PYTHONPATH=tests/eval:. TASK_DB_URL=$(EVAL_DB_URL) agents-cli eval run --mode adk_live --dataset $(DATASET) --config tests/eval/eval_config.yaml

# Run L1 live sandbox harness evaluation (HARNESS=claude|antigravity|horizon|all) and grade with agents-cli
eval-sandbox:
	uv run python tests/eval/sandbox/run_sandbox_eval.py --harness $(HARNESS) --dataset $(SANDBOX_DATASET) $(SANDBOX_EVAL_FLAGS)
	@for h in $$(if [ "$(HARNESS)" = "all" ]; then echo "claude antigravity horizon"; else echo "$(HARNESS)"; fi); do \
		echo "=== Grading sandbox traces for $$h ==="; \
		uv run agents-cli eval grade --traces artifacts/sandbox_traces/$$h --config tests/eval/sandbox/sandbox_eval_config.yaml --output artifacts/grade_results/sandbox_$$h; \
	done

# Compare two graded sandbox runs (e.g., make eval-sandbox-compare BASELINE=artifacts/grade_results/sandbox_claude/results_...json CANDIDATE=artifacts/grade_results/sandbox_antigravity/results_...json)
eval-sandbox-compare:
	uv run agents-cli eval compare $(BASELINE) $(CANDIDATE)

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


