import os
from dataclasses import dataclass
from typing import Mapping


class ConfigValidationError(ValueError):
    pass


def _parse_bool(raw: str, key: str) -> bool:
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ConfigValidationError(f"{key} must be a boolean-like value, got: {raw!r}")


def _parse_int(raw: str, key: str) -> int:
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigValidationError(f"{key} must be an integer, got: {raw!r}") from exc


def _parse_float(raw: str, key: str) -> float:
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigValidationError(f"{key} must be numeric, got: {raw!r}") from exc


def _parse_csv(raw: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _validate_cron(expr: str) -> None:
    # v1 keeps classic 5-field cron syntax for predictable cross-platform behavior.
    if len(expr.split()) != 5:
        raise ConfigValidationError(
            "MEP_AUTOPILOT_CRON must contain 5 fields (minute hour day month weekday)"
        )


def _validate_url(url: str, key: str, allowed_prefixes: tuple[str, ...]) -> None:
    if not url.startswith(allowed_prefixes):
        raise ConfigValidationError(f"{key} must start with one of {allowed_prefixes}, got: {url!r}")


@dataclass(frozen=True)
class AutopilotConfig:
    autopilot_enabled: bool
    idle_earn_enabled: bool
    dm_sync_enabled: bool
    compute_sync_enabled: bool
    autopilot_cron: str
    autopilot_timezone: str
    idle_required: bool
    max_tasks_per_hour: int
    max_runtime_seconds: int
    max_token_spend_per_hour: int
    allowed_models: tuple[str, ...]
    min_bounty: float
    max_bounty: float
    hub_url: str
    ws_url: str
    autopilot_pause: bool

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "AutopilotConfig":
        source = env if env is not None else os.environ

        cfg = cls(
            autopilot_enabled=_parse_bool(source.get("MEP_AUTOPILOT_ENABLED", "false"), "MEP_AUTOPILOT_ENABLED"),
            idle_earn_enabled=_parse_bool(source.get("MEP_IDLE_EARN_ENABLED", "false"), "MEP_IDLE_EARN_ENABLED"),
            dm_sync_enabled=_parse_bool(source.get("MEP_DM_SYNC_ENABLED", "false"), "MEP_DM_SYNC_ENABLED"),
            compute_sync_enabled=_parse_bool(source.get("MEP_COMPUTE_SYNC_ENABLED", "false"), "MEP_COMPUTE_SYNC_ENABLED"),
            autopilot_cron=source.get("MEP_AUTOPILOT_CRON", "*/5 * * * *").strip(),
            autopilot_timezone=source.get("MEP_AUTOPILOT_TIMEZONE", "UTC").strip() or "UTC",
            idle_required=_parse_bool(source.get("MEP_IDLE_REQUIRED", "true"), "MEP_IDLE_REQUIRED"),
            max_tasks_per_hour=_parse_int(source.get("MEP_MAX_TASKS_PER_HOUR", "20"), "MEP_MAX_TASKS_PER_HOUR"),
            max_runtime_seconds=_parse_int(source.get("MEP_MAX_RUNTIME_SECONDS", "600"), "MEP_MAX_RUNTIME_SECONDS"),
            max_token_spend_per_hour=_parse_int(
                source.get("MEP_MAX_TOKEN_SPEND_PER_HOUR", "1000"),
                "MEP_MAX_TOKEN_SPEND_PER_HOUR",
            ),
            allowed_models=_parse_csv(source.get("MEP_ALLOWED_MODELS", "cli-agent,gemini,deepseek")),
            min_bounty=_parse_float(source.get("MEP_MIN_BOUNTY", "0.0"), "MEP_MIN_BOUNTY"),
            max_bounty=_parse_float(source.get("MEP_MAX_BOUNTY", "20.0"), "MEP_MAX_BOUNTY"),
            hub_url=source.get("HUB_URL", "https://mep-hub.silentcopilot.ai").strip(),
            ws_url=source.get("WS_URL", "wss://mep-hub.silentcopilot.ai").strip(),
            autopilot_pause=_parse_bool(source.get("MEP_AUTOPILOT_PAUSE", "false"), "MEP_AUTOPILOT_PAUSE"),
        )

        _validate_cron(cfg.autopilot_cron)
        _validate_url(cfg.hub_url, "HUB_URL", ("http://", "https://"))
        _validate_url(cfg.ws_url, "WS_URL", ("ws://", "wss://"))

        if cfg.max_tasks_per_hour < 0:
            raise ConfigValidationError("MEP_MAX_TASKS_PER_HOUR must be >= 0")
        if cfg.max_runtime_seconds <= 0:
            raise ConfigValidationError("MEP_MAX_RUNTIME_SECONDS must be > 0")
        if cfg.max_token_spend_per_hour < 0:
            raise ConfigValidationError("MEP_MAX_TOKEN_SPEND_PER_HOUR must be >= 0")
        if cfg.min_bounty > cfg.max_bounty:
            raise ConfigValidationError("MEP_MIN_BOUNTY cannot be greater than MEP_MAX_BOUNTY")
        if not cfg.allowed_models:
            raise ConfigValidationError("MEP_ALLOWED_MODELS must contain at least one model name")

        return cfg

    def as_dict(self) -> dict:
        return {
            "autopilot_enabled": self.autopilot_enabled,
            "autopilot_pause": self.autopilot_pause,
            "jobs": {
                "idle_earn_enabled": self.idle_earn_enabled,
                "dm_sync_enabled": self.dm_sync_enabled,
                "compute_sync_enabled": self.compute_sync_enabled,
            },
            "scheduler": {
                "cron": self.autopilot_cron,
                "timezone": self.autopilot_timezone,
                "timezone_policy": "UTC schedule in v1; timezone for display/logging only",
            },
            "safety": {
                "idle_required": self.idle_required,
                "max_tasks_per_hour": self.max_tasks_per_hour,
                "max_runtime_seconds": self.max_runtime_seconds,
                "max_token_spend_per_hour": self.max_token_spend_per_hour,
                "allowed_models": list(self.allowed_models),
                "min_bounty": self.min_bounty,
                "max_bounty": self.max_bounty,
            },
            "connectivity": {
                "hub_url": self.hub_url,
                "ws_url": self.ws_url,
            },
        }
