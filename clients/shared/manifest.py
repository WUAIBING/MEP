import json
import os
from dataclasses import dataclass
from typing import Any


@dataclass
class MEPManifest:
    path: str
    raw: dict[str, Any]

    @property
    def version(self) -> int:
        return int(self.raw.get("version", 1))

    @property
    def alias(self) -> str | None:
        value = self.raw.get("alias")
        return value.strip() if isinstance(value, str) and value.strip() else None

    @property
    def hub_url(self) -> str | None:
        return _string_at(self.raw, "transport", "hub_url")

    @property
    def ws_url(self) -> str | None:
        return _string_at(self.raw, "transport", "ws_url")

    @property
    def heartbeat_seconds(self) -> int | None:
        value = _value_at(self.raw, "transport", "heartbeat_seconds")
        return int(value) if value is not None else None

    @property
    def key_path(self) -> str | None:
        value = _string_at(self.raw, "auth", "key_path")
        if not value:
            return None
        if os.path.isabs(value):
            return value
        base_dir = os.path.dirname(os.path.abspath(self.path))
        return os.path.abspath(os.path.join(base_dir, value))

    @property
    def capabilities(self) -> dict[str, Any]:
        value = self.raw.get("capabilities")
        return value if isinstance(value, dict) else {}

    @property
    def runtime(self) -> dict[str, Any]:
        value = self.raw.get("runtime")
        return value if isinstance(value, dict) else {}

    @property
    def registry_skills(self) -> list[str]:
        value = self.capabilities.get("skills", [])
        if not isinstance(value, list):
            return []
        return [item.strip().lower() for item in value if isinstance(item, str) and item.strip()]

    @property
    def registry_models(self) -> list[str]:
        value = self.capabilities.get("models", [])
        if not isinstance(value, list):
            return []
        return [item.strip() for item in value if isinstance(item, str) and item.strip()]

    @property
    def metadata(self) -> dict[str, Any]:
        value = self.raw.get("metadata")
        return value if isinstance(value, dict) else {}


def _value_at(payload: dict[str, Any], *path: str) -> Any:
    current: Any = payload
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _string_at(payload: dict[str, Any], *path: str) -> str | None:
    value = _value_at(payload, *path)
    return value.strip() if isinstance(value, str) and value.strip() else None


def load_manifest(path: str | None = None) -> MEPManifest | None:
    manifest_path = path or os.getenv("MEP_MANIFEST_PATH")
    if not manifest_path:
        return None
    resolved = os.path.abspath(manifest_path)
    with open(resolved, "r", encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, dict):
        raise ValueError("Manifest root must be a JSON object")
    version = raw.get("version", 1)
    if version != 1:
        raise ValueError(f"Unsupported manifest version: {version}")
    return MEPManifest(path=resolved, raw=raw)
