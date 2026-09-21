"""Setup script for configuring integrations, verifying credentials, and initializing git workspaces."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
INTEGRATIONS_YAML = PROJECT_ROOT / "config" / "integrations.yaml"
WORKSPACES_YAML = PROJECT_ROOT / "config" / "workspaces.yaml"
MCP_JSON_OUT = PROJECT_ROOT / ".mcp.json"


def load_integrations_config() -> dict[str, Any]:
    if not INTEGRATIONS_YAML.exists():
        raise FileNotFoundError(f"Missing {INTEGRATIONS_YAML}")
    return yaml.safe_load(INTEGRATIONS_YAML.read_text()) or {}


def generate_claude_mcp_json() -> Path:
    """Reads config/integrations.yaml and writes the .mcp.json file consumed by `claude -p --mcp-config`."""
    data = load_integrations_config()
    integrations = data.get("integrations", {})
    mcp_servers: dict[str, Any] = {}

    for name, spec in integrations.items():
        if not spec.get("enabled", False):
            continue
        conn_type = spec.get("type")
        if conn_type not in ("streamable_http", "stdio"):
            continue
        if conn_type == "streamable_http":
            mcp_servers[name] = {
                "type": "http",
                "url": spec["url"],
            }
        elif conn_type == "stdio":
            mcp_servers[name] = {
                "command": spec["command"],
                "args": spec.get("args", []),
            }

    MCP_JSON_OUT.write_text(json.dumps({"mcpServers": mcp_servers}, indent=2) + "\n")
    display_path = (
        MCP_JSON_OUT.relative_to(PROJECT_ROOT)
        if MCP_JSON_OUT.is_relative_to(PROJECT_ROOT)
        else MCP_JSON_OUT
    )
    print(f"✓ Generated {display_path} with {len(mcp_servers)} worker MCP servers.")
    return MCP_JSON_OUT


def _configure_git_remotes(target_dir: Path, origin_url: str, upstream_url: str | None) -> None:
    """Ensure `origin` points to the developer's primary/fork URL and `upstream` points to the canonical upstream repo."""
    if not (target_dir / ".git").exists():
        return
    subprocess.run(
        ["git", "-C", str(target_dir), "remote", "set-url", "origin", origin_url],
        check=False,
        capture_output=True,
    )
    if upstream_url:
        res = subprocess.run(
            ["git", "-C", str(target_dir), "remote", "get-url", "upstream"],
            check=False,
            capture_output=True,
        )
        if res.returncode == 0:
            subprocess.run(
                ["git", "-C", str(target_dir), "remote", "set-url", "upstream", upstream_url],
                check=False,
                capture_output=True,
            )
        else:
            subprocess.run(
                ["git", "-C", str(target_dir), "remote", "add", "upstream", upstream_url],
                check=False,
                capture_output=True,
            )


def setup_workspace(clone_remotes: bool = False) -> Path:
    """Initializes the local workspace root and optionally clones repositories listed in config/workspaces.yaml (+ workspaces.local.yaml)."""
    from app.workers.harnesses.base import load_workspaces_manifest

    if not WORKSPACES_YAML.exists():
        raise FileNotFoundError(f"Missing {WORKSPACES_YAML}")

    ws_cfg = load_workspaces_manifest()
    ws_root = (PROJECT_ROOT / ws_cfg.get("workspace_root", "./workspaces")).resolve()
    ws_root.mkdir(parents=True, exist_ok=True)
    print(f"✓ Workspace directory ready at: {ws_root}")

    if not clone_remotes:
        return ws_root

    for repo in ws_cfg.get("repositories", []):
        name = repo["name"]
        git_url = repo["git_url"]
        upstream_url = repo.get("upstream_url")
        branch = repo.get("branch", "main")
        target_dir = ws_root / name

        if target_dir.exists():
            _configure_git_remotes(target_dir, git_url, upstream_url)
            print(f"  • {name}: updating existing checkout at {target_dir} ({branch})...")
            subprocess.run(
                ["git", "-C", str(target_dir), "pull", "--ff-only", "origin", branch],
                check=False,
            )
            continue

        print(f"  • Cloning {name} ({git_url})...")
        sparse_paths = repo.get("sparse_paths")
        if sparse_paths:
            subprocess.run(
                ["git", "clone", "--filter=blob:none", "--depth", "1", "--sparse", "--branch", branch, git_url, str(target_dir)],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(target_dir), "sparse-checkout", "add", *sparse_paths],
                check=True,
            )
        else:
            subprocess.run(
                ["git", "clone", "--depth", "1", "--branch", branch, git_url, str(target_dir)],
                check=True,
            )
        _configure_git_remotes(target_dir, git_url, upstream_url)
    return ws_root


def main() -> None:
    parser = argparse.ArgumentParser(description="Setup Voice Orchestrator integrations and workspace.")
    parser.add_argument("--clone-repos", action="store_true", help="Clone repositories from config/workspaces.yaml")
    args = parser.parse_args()

    generate_claude_mcp_json()
    setup_workspace(clone_remotes=args.clone_repos)


if __name__ == "__main__":
    main()
