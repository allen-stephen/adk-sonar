# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""FastAPI server for the voice orchestrator.

Native ADK Live websocket route (/run_live) is mounted automatically by get_fast_api_app.
A2A routes are omitted as Live agents do not support A2A.
"""

import contextlib
import os
from collections.abc import AsyncIterator

from dotenv import load_dotenv
from fastapi import FastAPI
from google.adk.cli.fast_api import get_fast_api_app
from google.adk.runners import Runner

import sys

from app.app_utils import services

if "pytest" not in sys.modules:
    load_dotenv()
_default_origins = (
    "http://127.0.0.1:3000,http://localhost:3000,"
    "http://127.0.0.1:5173,http://localhost:5173,"
    "http://127.0.0.1:8000,http://localhost:8000,"
    "http://127.0.0.1:8080,http://localhost:8080,*"
)
allow_origins = [
    origin.strip()
    for origin in os.getenv("ALLOW_ORIGINS", _default_origins).split(",")
    if origin.strip()
]
otel_to_cloud = False

AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    from app.agent import app as adk_app
    from app.auth import ensure_fresh_access_token, is_remote_deployment
    from app.tools.workspace_tools import get_workspace_root

    # 1. Self-heal workspace repositories on fresh local clones or Cloud Run containers
    try:
        ws_root = get_workspace_root()
        if not ws_root.exists() or not any(
            d.is_dir() and not d.name.startswith(".") for d in ws_root.iterdir()
        ):
            from scripts.provision_sandbox import seed_declarative_repositories

            seed_declarative_repositories(ws_root)
    except Exception:
        pass

    # 2. Warm/refresh OAuth access tokens (Spotify & Google Workspace) from refresh tokens on boot
    if not os.getenv("PYTEST_CURRENT_TEST"):
        try:
            await ensure_fresh_access_token("spotify")
            await ensure_fresh_access_token("google_workspace")
            if not is_remote_deployment():
                from scripts.provision_sandbox import ensure_env_and_secrets

                ensure_env_and_secrets(auto_gcloud_token=True, auto_gh_token=True)
        except Exception:
            pass

    runner = Runner(
        app=adk_app,
        session_service=services.get_session_service(),
        artifact_service=services.get_artifact_service(),
        auto_create_session=True,
    )
    app.state.runner = runner
    app.state.agent_app_name = adk_app.name
    yield


app: FastAPI = get_fast_api_app(
    agents_dir=AGENT_DIR,
    web=True,
    artifact_service_uri=services.ARTIFACT_SERVICE_URI,
    allow_origins=allow_origins,
    session_service_uri=services.SESSION_SERVICE_URI,
    otel_to_cloud=otel_to_cloud,
    lifespan=lifespan,
)
from app.api_routes import router as ui_control_router

app.title = "voice-orchestrator"
app.description = "Voice orchestrator for remote Claude Code coding agents"
app.include_router(ui_control_router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
