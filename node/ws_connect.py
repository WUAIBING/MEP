"""Shared WebSocket connection helper.

Ensures the Host header is set correctly when connecting through
an nginx reverse proxy. Without an explicit host, the Python
websockets library may include the port in the Host header
(e.g. "Host: host:443") which nginx may reject with HTTP 403.

Usage:
    from ws_connect import ws_connect
    async with ws_connect(uri) as ws:
        ...
"""

from urllib.parse import urlparse
import websockets


def ws_connect(uri: str, **kwargs):
    """Connect to a WebSocket URI, ensuring the Host header is correct."""
    parsed = urlparse(uri)
    kwargs.setdefault("host", parsed.hostname)
    return websockets.connect(uri, **kwargs)
