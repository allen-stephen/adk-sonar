"""Integration registry and ADK McpToolset builder driven by config/integrations.yaml.

Constructs ADK native `McpToolset` instances using `StreamableHTTPConnectionParams`
and dynamic `header_provider` callbacks for Google Workspace and remote MCP endpoints,
as well as generating the `.mcp.json` passed to headless Claude Code (`--mcp-config`).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.tools.mcp_tool.mcp_session_manager import (
    StdioConnectionParams,
    StreamableHTTPConnectionParams,
)
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset
from mcp import StdioServerParameters

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "integrations.yaml"


@dataclass
class IntegrationSpec:
    name: str
    display_name: str
    category: str
    enabled: bool
    conn_type: str  # "adk_builtin" | "streamable_http" | "stdio"
    description: str
    url: str | None = None
    command: str | None = None
    args: list[str] = field(default_factory=list)
    auth_env: str | None = None
    auth_config: dict[str, Any] = field(default_factory=dict)
    tool_name_prefix: str | None = None
    tool_filter: list[str] | None = None
    expose_to: list[str] = field(default_factory=lambda: ["voice_agent"])

    def has_valid_credentials(self) -> bool:
        if not self.auth_env:
            return True
        if os.getenv(self.auth_env):
            return True
        provider = self.auth_config.get("provider")
        if provider:
            from app.auth import has_provider_credentials

            return has_provider_credentials(provider)
        return False


def make_auth_header_provider(auth_env_key: str | None) -> Callable[[ReadonlyContext], dict[str, str]]:
    """Builds an ADK McpToolset header_provider that resolves per-session or env tokens at runtime."""

    def _provider(readonly_context: ReadonlyContext) -> dict[str, str]:
        if not auth_env_key:
            return {}
        # 1. Check session state first (supports per-user OAuth tokens)
        token = None
        if hasattr(readonly_context, "state") and readonly_context.state:
            token = readonly_context.state.get(auth_env_key) or readonly_context.state.get(auth_env_key.lower())
        # 2. Fallback to environment variable
        if not token:
            token = os.getenv(auth_env_key)
        if token:
            return {"Authorization": f"Bearer {token}"}
        return {}

    return _provider


class IntegrationRegistry:
    """Loads config/integrations.yaml and provides runtime toggleable MCP and builtin integrations."""

    def __init__(self, config_path: Path | None = None) -> None:
        self.config_path = config_path or DEFAULT_CONFIG_PATH
        self._specs: dict[str, IntegrationSpec] = {}
        self._load()

    def _load(self) -> None:
        if not self.config_path.exists():
            logger.warning("Integrations config not found at %s", self.config_path)
            return
        raw = yaml.safe_load(self.config_path.read_text()) or {}
        for name, item in (raw.get("integrations") or {}).items():
            env_override = os.getenv(f"ENABLE_{name.upper()}")
            if env_override is not None:
                enabled = env_override.strip().lower() in {"1", "true", "yes", "on"}
            else:
                enabled = bool(item.get("enabled", True))

            self._specs[name] = IntegrationSpec(
                name=name,
                display_name=item.get("display_name", name),
                category=item.get("category", "general"),
                enabled=enabled,
                conn_type=item.get("type", "streamable_http"),
                description=item.get("description", ""),
                url=item.get("url"),
                command=item.get("command"),
                args=item.get("args", []),
                auth_env=item.get("auth_env"),
                auth_config=item.get("auth_config") or {},
                tool_name_prefix=item.get("tool_name_prefix"),
                tool_filter=item.get("tool_filter"),
                expose_to=item.get("expose_to", ["voice_agent"]),
            )

    def list_all(self) -> list[IntegrationSpec]:
        return list(self._specs.values())

    def get(self, name: str) -> IntegrationSpec | None:
        return self._specs.get(name)

    def is_enabled(self, name: str) -> bool:
        spec = self._specs.get(name)
        return bool(spec and spec.enabled)

    def set_enabled(self, name: str, enabled: bool) -> IntegrationSpec | None:
        spec = self._specs.get(name)
        if spec is None:
            return None
        spec.enabled = enabled
        return spec

    def build_adk_mcp_toolsets(self, only_with_credentials: bool = True) -> list[McpToolset]:
        """Constructs ADK McpToolset objects using StreamableHTTPConnectionParams + header_provider.

        By default (`only_with_credentials=True`), only mounts remote MCP servers whose required
        authentication token (`auth_env`) is present in the environment so an unconfigured
        connector never blocks WebSocket handshake in `/run_live`.
        """
        toolsets: list[McpToolset] = []
        for spec in self._specs.values():
            if not spec.enabled or "voice_agent" not in spec.expose_to:
                continue
            if spec.conn_type not in {"streamable_http", "stdio"}:
                continue
            if only_with_credentials and not spec.has_valid_credentials():
                logger.info(
                    "Skipping live mount of %s McpToolset (%s is not set in environment)",
                    spec.name,
                    spec.auth_env,
                )
                continue

            if spec.conn_type == "streamable_http" and spec.url:
                toolsets.append(
                    McpToolset(
                        connection_params=StreamableHTTPConnectionParams(url=spec.url),
                        header_provider=make_auth_header_provider(spec.auth_env),
                        tool_filter=spec.tool_filter,
                        tool_name_prefix=spec.tool_name_prefix,
                    )
                )
            elif spec.conn_type == "stdio" and spec.command:
                env_vars = {**os.environ}
                toolsets.append(
                    McpToolset(
                        connection_params=StdioConnectionParams(
                            server_params=StdioServerParameters(
                                command=spec.command,
                                args=spec.args,
                                env=env_vars,
                            )
                        ),
                        tool_filter=spec.tool_filter,
                        tool_name_prefix=spec.tool_name_prefix,
                    )
                )
        return toolsets

    def build_claude_mcp_config(self) -> dict[str, Any]:
        """Generates the --mcp-config dictionary for headless Claude Code runs."""
        mcp_servers: dict[str, Any] = {}
        for spec in self._specs.values():
            if not spec.enabled or "claude_worker" not in spec.expose_to:
                continue
            if spec.conn_type == "streamable_http" and spec.url:
                entry: dict[str, Any] = {"type": "http", "url": spec.url}
                if spec.auth_env and os.getenv(spec.auth_env):
                    entry["headers"] = {"Authorization": f"Bearer {os.getenv(spec.auth_env)}"}
                mcp_servers[spec.name] = entry
            elif spec.conn_type == "stdio" and spec.command:
                env_map: dict[str, str] = {}
                if spec.auth_env and os.getenv(spec.auth_env):
                    env_map[spec.auth_env] = os.getenv(spec.auth_env, "")
                for extra_key in spec.auth_config.get("extra_env", []):
                    if os.getenv(extra_key):
                        env_map[extra_key] = os.getenv(extra_key, "")
                for key_field in ("client_id_env", "client_secret_env", "refresh_token_env"):
                    env_name = spec.auth_config.get(key_field)
                    if env_name and os.getenv(env_name):
                        env_map[env_name] = os.getenv(env_name, "")
                mcp_servers[spec.name] = {
                    "command": spec.command,
                    "args": spec.args,
                    "env": env_map,
                }
        return {"mcpServers": mcp_servers}


_registry: IntegrationRegistry | None = None


def get_integration_registry() -> IntegrationRegistry:
    global _registry
    if _registry is None:
        _registry = IntegrationRegistry()
    return _registry


def reset_integration_registry() -> None:
    global _registry
    _registry = None


async def list_integrations() -> str:
    """List all configurable integrations (Google Search, Google Maps, Calendar, Gmail, Drive, GitHub, Spotify, Slack) and their status.

    Returns:
        Spoken-friendly list of enabled and disabled integrations and whether credentials are active.
    """
    reg = get_integration_registry()
    active_parts: list[str] = []
    disabled_parts: list[str] = []

    for spec in reg.list_all():
        if spec.enabled:
            if not spec.has_valid_credentials():
                active_parts.append(f"{spec.display_name} (enabled, awaiting {spec.auth_env})")
            else:
                active_parts.append(f"{spec.display_name} (ready)")
        else:
            disabled_parts.append(spec.display_name)

    response = f"Enabled integrations: {', '.join(active_parts)}."
    if disabled_parts:
        response += f" Disabled integrations: {', '.join(disabled_parts)}."
    return response


async def configure_integration(integration_name: str, enabled: bool) -> str:
    """Enable or disable a configurable integration at runtime.

    Args:
        integration_name: Name of the integration (e.g. 'google_calendar', 'gmail', 'slack', 'github', 'spotify', 'google_search', 'google_maps').
        enabled: True to enable, False to disable.

    Returns:
        Spoken confirmation of the updated state.
    """
    reg = get_integration_registry()
    key = integration_name.strip().lower().replace(" ", "_")
    spec = reg.set_enabled(key, enabled)
    if not spec:
        valid = ", ".join(s.name for s in reg.list_all())
        return f"Unknown integration {integration_name}. Available integrations are: {valid}."
    status_str = "enabled" if enabled else "disabled"
    return f"{spec.display_name} is now {status_str}."
