"""Registry and singleton helpers for configurable coding harnesses."""

from __future__ import annotations

import os

from app.workers.harnesses.antigravity import AntigravityHarness
from app.workers.harnesses.base import CodingHarness
from app.workers.harnesses.claude import ClaudeCodeHarness
from app.workers.harnesses.horizon import HorizonA2AHarness

_BUILTIN_ALIASES: dict[str, str] = {
    "claude": "claude",
    "claude-code": "claude",
    "claude_code": "claude",
    "claudecode": "claude",
    "horizon": "horizon",
    "long-horizon": "horizon",
    "long_horizon": "horizon",
    "long-horizon-harness": "horizon",
    "long_horizon_harness": "horizon",
    "adk": "horizon",
    "adk-horizon": "horizon",
    "antigravity": "antigravity",
    "agy": "antigravity",
    "gemini": "antigravity",
    "gemini-cli": "antigravity",
    "gemini_cli": "antigravity",
}


class HarnessRegistry:
    """Registry of available coding harnesses and active default selection."""

    def __init__(self) -> None:
        self._harnesses: dict[str, CodingHarness] = {
            "horizon": HorizonA2AHarness(),
            "antigravity": AntigravityHarness(),
            "claude": ClaudeCodeHarness(),
        }
        # Copied per instance so `register_harness` cannot leak aliases into
        # class state and survive `reset_harness_registry()`.
        self._aliases: dict[str, str] = dict(_BUILTIN_ALIASES)
        env_default = os.getenv("CODING_HARNESS", "horizon")
        self._default_name = self.normalize_name(env_default) or "horizon"

    def normalize_name(self, name: str | None) -> str | None:
        if not name:
            return None
        cleaned = name.strip().lower().replace(" ", "-")
        if cleaned in self._harnesses:
            return cleaned
        return self._aliases.get(cleaned)

    def register_harness(
        self, harness: CodingHarness, aliases: list[str] | None = None
    ) -> None:
        key = harness.name.strip().lower()
        self._harnesses[key] = harness
        self._aliases[key] = key
        for alias in aliases or []:
            self._aliases[alias.strip().lower()] = key

    def get(self, name: str | None = None) -> CodingHarness:
        """Return the requested harness, or the active default if name is None."""
        if name is None:
            return self._harnesses[self._default_name]
        canonical = self.normalize_name(name)
        if not canonical or canonical not in self._harnesses:
            valid = ", ".join(self._harnesses.keys())
            raise ValueError(
                f"Unknown coding harness '{name}'. Available harnesses: {valid}."
            )
        return self._harnesses[canonical]

    @property
    def default_harness(self) -> CodingHarness:
        return self._harnesses[self._default_name]

    def set_default(self, name: str) -> CodingHarness:
        harness = self.get(name)
        self._default_name = harness.name
        return harness

    def list_all(self) -> list[CodingHarness]:
        return list(self._harnesses.values())


_harness_registry: HarnessRegistry | None = None


def get_harness_registry() -> HarnessRegistry:
    global _harness_registry
    if _harness_registry is None:
        _harness_registry = HarnessRegistry()
    return _harness_registry


def get_coding_harness(name: str | None = None) -> CodingHarness:
    return get_harness_registry().get(name)


def reset_harness_registry() -> None:
    global _harness_registry
    _harness_registry = None
