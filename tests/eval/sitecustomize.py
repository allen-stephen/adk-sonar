"""Evaluation-only startup hook loaded via `PYTHONPATH=tests/eval:.` during `make eval`.

Wires the test doubles from `tests/fakes.py` (or recorded L1 sandbox fixtures from
`tests/eval/fixtures/<harness>/*.json` when `EVAL_REPLAY_FIXTURES=1`) into the
`agents-cli eval run --mode adk_live` server subprocess so that `app/` remains 100%
free of test/eval branches while multi-turn voice evaluations execute deterministically.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


def _load_recorded_fixture(harness_name: str) -> dict | None:
    """Return the most recently recorded L1 sandbox fixture for `harness_name`, if enabled."""
    if os.getenv("EVAL_REPLAY_FIXTURES", "").strip().lower() not in {"1", "true", "yes"}:
        return None
    fixture_dir = ROOT_DIR / "tests" / "eval" / "fixtures" / harness_name
    if not fixture_dir.exists():
        return None
    candidates = sorted(fixture_dir.glob("*.json"))
    if not candidates:
        return None
    try:
        return json.loads(candidates[0].read_text(encoding="utf-8"))
    except Exception:
        return None


def _install_eval_harness_and_integration_fakes() -> None:
    os.environ.setdefault("ORCHESTRATOR_SKIP_STARTUP_WARMUP", "1")
    os.environ.setdefault("ORCHESTRATOR_DISABLE_HOST_GIT", "1")
    os.environ.setdefault("SPOTIFY_ACCESS_TOKEN", "eval-spotify-token")
    os.environ.setdefault("GOOGLE_WORKSPACE_ACCESS_TOKEN", "eval-workspace-token")
    os.environ.setdefault("SLACK_BOT_TOKEN", "eval-slack-token")
    os.environ.setdefault("GITHUB_PERSONAL_ACCESS_TOKEN", "eval-github-token")

    try:
        from app.app_utils.http_client import set_default_http_transport
        from app.auth import ProviderAuthState, _provider_states
        from app.workers import SandboxWorker, get_harness_registry
        import app.workers.factory as factory_mod
        import app.tools.grounding_tools as grounding_mod
        from tests.fakes import (
            FakeHarness,
            exec_transport,
            integrations_transport,
            sandbox_connection,
        )

        # 1. Route downstream integration HTTP calls through integrations_transport
        set_default_http_transport(integrations_transport())

        now = time.time()
        for prov, label in (
            ("spotify", "Spotify Eval"),
            ("google_workspace", "Workspace Eval"),
            ("slack", "Slack Eval"),
            ("github", "@sonar-dev"),
        ):
            _provider_states[prov] = ProviderAuthState(
                provider=prov,
                account_label=label,
                expires_at=now + 86400,
                last_verified_at=now,
            )

        # 2. Deterministic grounded search & maps specialist for eval runs
        async def _eval_specialist(agent: object, prompt: str) -> str:
            name = getattr(agent, "name", "")
            if "maps" in name:
                return (
                    f"Google Maps results for {prompt}: Medici Roasting on Congress Avenue "
                    "is open now with a four point seven star rating and quiet upstairs Wi-Fi seating."
                )
            return (
                f"Google Search results for {prompt}: Python 3.13 introduces an experimental "
                "free-threaded mode disabling the global interpreter lock and an experimental JIT compiler."
            )

        grounding_mod._run_specialist = _eval_specialist

        # 3. Coding harnesses: replay recorded L1 sandbox fixtures when EVAL_REPLAY_FIXTURES=1,
        #    otherwise use deterministic FakeHarness baseline.
        reg = get_harness_registry()
        for h_name, disp in (
            ("claude", "Claude Code"),
            ("horizon", "ADK Long Horizon"),
            ("antigravity", "Antigravity"),
        ):
            rec = _load_recorded_fixture(h_name)
            if rec:
                fake_h = FakeHarness(
                    name=h_name,
                    display_name=disp,
                    summary=str(rec.get("summary") or f"Completed via {disp}."),
                    questions=list(
                        rec.get("questions")
                        or ["Should we use Redis-backed token rotation or in-memory caching?"]
                    ),
                    writes={
                        f: "# Replayed from L1 sandbox fixture\n"
                        for f in (rec.get("files_changed") or ["src/module.py"])
                    },
                    diff_summary=str(
                        rec.get("diff_summary") or "1 file changed (+2 -0) in worktree."
                    ),
                )
            else:
                fake_h = FakeHarness(
                    name=h_name,
                    display_name=disp,
                    summary=f"Implemented requested changes via {disp} and verified all unit tests pass.",
                    questions=[
                        "Should we use Redis-backed token rotation or in-memory caching?"
                    ],
                    writes={"src/module.py": "def execute() -> bool:\n    return True\n"},
                    diff_summary="1 file changed (+2 -0) in worktree.",
                )
            reg.register_harness(fake_h)

        factory_mod._active_worker = SandboxWorker(
            connection=sandbox_connection(),
            http_transport=exec_transport(exit_code=0),
        )
    except Exception:
        pass


_install_eval_harness_and_integration_fakes()
