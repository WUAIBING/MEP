"""Shared WebSocket connection helpers."""

from __future__ import annotations

from urllib.parse import ParseResult, urlparse, urlunparse


def normalize_ws_uri(uri: str) -> str:
    """Remove explicit default ports so proxy Host checks see the canonical host."""
    parsed = urlparse(uri)
    if (parsed.scheme, parsed.port) not in {("ws", 80), ("wss", 443)}:
        return uri

    hostname = parsed.hostname or ""
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    if parsed.username:
        userinfo = parsed.username
        if parsed.password:
            userinfo = f"{userinfo}:{parsed.password}"
        netloc = f"{userinfo}@{hostname}"
    else:
        netloc = hostname

    normalized = ParseResult(
        scheme=parsed.scheme,
        netloc=netloc,
        path=parsed.path,
        params=parsed.params,
        query=parsed.query,
        fragment=parsed.fragment,
    )
    return urlunparse(normalized)


def ws_connect(uri: str, **kwargs):
    """Connect to a WebSocket URI after canonicalizing default ports."""
    import websockets

    return websockets.connect(normalize_ws_uri(uri), **kwargs)
