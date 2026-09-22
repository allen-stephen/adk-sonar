"""Shared async HTTP client factory supporting transport injection in tests."""

from __future__ import annotations

import httpx

_default_transport: httpx.AsyncBaseTransport | None = None


def set_default_http_transport(transport: httpx.AsyncBaseTransport | None) -> None:
    """Configure an optional process-wide httpx transport override (used by tests/fakes.py)."""
    global _default_transport
    _default_transport = transport


def get_default_http_transport() -> httpx.AsyncBaseTransport | None:
    """Return the currently configured httpx transport override, if any."""
    return _default_transport


def get_http_client(
    *,
    timeout: float = 8.0,
    headers: dict[str, str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    """Create an httpx.AsyncClient using the active transport."""
    return httpx.AsyncClient(
        timeout=timeout,
        headers=headers,
        transport=transport if transport is not None else _default_transport,
    )
