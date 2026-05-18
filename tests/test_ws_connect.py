from node.ws_connect import normalize_ws_uri


def test_normalize_ws_uri_removes_default_wss_port():
    uri = "wss://mep-hub.silentcopilot.ai:443/ws/node?timestamp=1&signature=sig"

    assert normalize_ws_uri(uri) == "wss://mep-hub.silentcopilot.ai/ws/node?timestamp=1&signature=sig"


def test_normalize_ws_uri_preserves_non_default_port():
    uri = "ws://localhost:8000/ws/node?timestamp=1&signature=sig"

    assert normalize_ws_uri(uri) == uri
