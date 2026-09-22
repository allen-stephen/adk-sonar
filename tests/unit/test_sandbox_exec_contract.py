"""Contract tests for the single `/exec` HTTP seam into the Vertex Sandbox.

`exec_in_sandbox` is the one place harness code talks to a sandbox, and its
contract is load-bearing: every harness decides success or failure from the
`SandboxExecResult` it returns. The distinction that matters most is between
"the sandbox is broken" (`error` set) and "the command ran and failed"
(`error is None`, non-zero `exit_code`) — conflating them is what made sandbox
failures invisible before P0.

These tests drive the real function through `httpx.MockTransport`, so no code
path is stubbed out.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.workers.harnesses.base import SandboxContext, exec_in_sandbox
from tests.fakes import exec_transport


@pytest.mark.asyncio
async def test_successful_command_parses_the_response():
    captured: list[httpx.Request] = []
    context = SandboxContext(
        lb_host="sandbox-123.aiplatform.googleapis.com",
        routing_token="routing-abc",
        sandbox_token="auth-xyz",
    )

    result = await exec_in_sandbox(
        context,
        "echo hello",
        env={"FOO": "bar"},
        transport=exec_transport(
            stdout="hello\n", stderr="", exit_code=0, requests=captured
        ),
    )

    assert result.exit_code == 0
    assert result.stdout == "hello\n"
    assert result.error is None
    assert result.ok is True

    # The request itself is part of the contract: wrong host or missing routing
    # headers means Vertex silently routes the command nowhere.
    assert len(captured) == 1
    request = captured[0]
    assert str(request.url) == "https://sandbox-123.aiplatform.googleapis.com/exec"
    assert request.headers["Authorization"] == "Bearer auth-xyz"
    assert request.headers["X-Sandbox-Routing-Token"] == "routing-abc"
    assert request.headers["X-Sandbox-Port"] == "8080"
    assert json.loads(request.content) == {
        "command": "echo hello",
        "env": {"FOO": "bar"},
    }


@pytest.mark.asyncio
async def test_failed_command_is_not_a_transport_error():
    """A command that ran and failed must stay distinguishable from a dead sandbox."""
    result = await exec_in_sandbox(
        SandboxContext(),
        "exit 3",
        transport=exec_transport(exit_code=3, stderr="boom"),
    )

    assert result.exit_code == 3
    assert result.stderr == "boom"
    assert result.error is None  # the sandbox worked fine; the command did not
    assert result.ok is False


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 404, 500, 502, 503])
async def test_non_2xx_responses_surface_as_transport_errors(status_code: int):
    context = SandboxContext(lb_host="dead-sandbox.example.com")

    result = await exec_in_sandbox(
        context, "ls", transport=exec_transport(status_code=status_code)
    )

    assert result.exit_code == status_code
    assert result.error is not None
    assert f"HTTP {status_code}" in result.error
    assert "dead-sandbox.example.com" in result.error
    assert "No healthy upstream" in result.error  # response body is preserved
    assert result.ok is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectTimeout("connect timed out"),
        httpx.ReadTimeout("read timed out"),
        httpx.ConnectError("name resolution failed"),
    ],
)
async def test_connection_failures_never_raise(exc: Exception):
    """Transport failures come back as a result, so a harness never crashes mid-task."""
    result = await exec_in_sandbox(
        SandboxContext(lb_host="unreachable.example.com"),
        "ls",
        transport=exec_transport(raise_exc=exc),
    )

    assert result.exit_code == 1
    assert result.error is not None
    assert "unreachable.example.com" in result.error
    assert result.ok is False


@pytest.mark.asyncio
async def test_unparseable_body_is_an_error_not_a_success():
    result = await exec_in_sandbox(
        SandboxContext(), "ls", transport=exec_transport(body="<html>gateway</html>")
    )

    assert result.error is not None
    assert "unparseable" in result.error
    assert result.ok is False


@pytest.mark.asyncio
async def test_unexpected_payload_type_is_rejected():
    result = await exec_in_sandbox(
        SandboxContext(), "ls", transport=exec_transport(body=["not", "a", "dict"])
    )

    assert result.error is not None
    assert "unexpected payload type list" in result.error
    assert result.ok is False


@pytest.mark.asyncio
async def test_non_numeric_exit_code_is_rejected_rather_than_coerced():
    """A malformed exit_code must not be reported to the user as success."""
    result = await exec_in_sandbox(
        SandboxContext(),
        "ls",
        transport=exec_transport(body={"stdout": "partial", "exit_code": "error"}),
    )

    assert result.ok is False
    assert result.exit_code == 1
    assert result.error is not None
    assert "non-numeric exit_code" in result.error
    assert result.stdout == "partial"  # kept so the failure stays diagnosable


@pytest.mark.asyncio
async def test_missing_exit_code_defaults_to_success():
    """Absent is treated as "the sandbox didn't say", unlike a malformed value."""
    result = await exec_in_sandbox(
        SandboxContext(), "ls", transport=exec_transport(body={"stdout": "fine"})
    )

    assert result.ok is True
    assert result.exit_code == 0
    assert result.stdout == "fine"


@pytest.mark.asyncio
async def test_output_key_is_accepted_as_a_stdout_alias():
    result = await exec_in_sandbox(
        SandboxContext(),
        "ls",
        transport=exec_transport(body={"output": "from-output-key", "exit_code": 0}),
    )

    assert result.stdout == "from-output-key"
    assert result.ok is True
