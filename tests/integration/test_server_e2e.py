import logging
import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest
import requests
from requests.exceptions import RequestException

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

APP_NAME = "app"
BASE_URL = "http://127.0.0.1:8000"
LIST_APPS_URL = f"{BASE_URL}/list-apps"
HEADERS = {"Content-Type": "application/json"}


def log_output(pipe: Any, log_func: Any) -> None:
    for line in iter(pipe.readline, ""):
        log_func(line.strip())


def start_server() -> subprocess.Popen[str]:
    command = [
        sys.executable,
        "-m",
        "uvicorn",
        "app.fast_api_app:app",
        "--host",
        "0.0.0.0",
        "--port",
        "8000",
    ]
    env = os.environ.copy()
    env["INTEGRATION_TEST"] = "TRUE"
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=env,
    )
    threading.Thread(
        target=log_output, args=(process.stdout, logger.info), daemon=True
    ).start()
    threading.Thread(
        target=log_output, args=(process.stderr, logger.error), daemon=True
    ).start()
    return process


def wait_for_server(timeout: int = 45, interval: int = 1) -> bool:
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            response = requests.get(LIST_APPS_URL, timeout=5)
            if response.status_code == 200:
                logger.info("Server is ready")
                return True
        except RequestException:
            pass
        time.sleep(interval)
    return False


@pytest.fixture(scope="session")
def server_fixture(request: Any) -> Iterator[subprocess.Popen[str]]:
    server_process = start_server()
    if not wait_for_server():
        server_process.terminate()
        pytest.fail("Server failed to start")

    def stop_server() -> None:
        server_process.terminate()
        server_process.wait()

    request.addfinalizer(stop_server)
    yield server_process


def test_server_readiness(server_fixture: subprocess.Popen[str]) -> None:
    """Verify that the FastAPI server starts and serves /list-apps."""
    resp = requests.get(LIST_APPS_URL, timeout=5)
    assert resp.status_code == 200
    assert "app" in resp.json() or isinstance(resp.json(), list)
