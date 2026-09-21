"""Global pytest fixtures ensuring unit and integration tests never mutate the real workspace or project config files."""

from __future__ import annotations

from pathlib import Path
import pytest


@pytest.fixture(autouse=True)
def _isolate_workspace_and_project_files(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Redirect ORCHESTRATOR_WORKSPACE_ROOT and generated config paths to an isolated temp dir for every test."""
    isolated_ws = tmp_path_factory.mktemp("test-workspaces")
    isolated_cfg = tmp_path_factory.mktemp("test-config")
    monkeypatch.setenv("ORCHESTRATOR_WORKSPACE_ROOT", str(isolated_ws))
    monkeypatch.setenv("ORCHESTRATOR_DISABLE_HOST_GIT", "1")
    monkeypatch.setenv(
        "ORCHESTRATOR_WORKSPACES_LOCAL_YAML",
        str(isolated_cfg / "workspaces.local.yaml"),
    )

    try:
        import scripts.setup as setup_mod

        monkeypatch.setattr(setup_mod, "MCP_JSON_OUT", isolated_cfg / ".mcp.json")
    except Exception:
        pass
