import pytest

from node.mep_autopilot_config import AutopilotConfig, ConfigValidationError


def test_autopilot_config_defaults_are_valid() -> None:
    cfg = AutopilotConfig.from_env({})
    assert cfg.autopilot_enabled is False
    assert cfg.autopilot_cron == "*/5 * * * *"
    assert cfg.max_token_spend_per_hour == 1000
    assert cfg.allowed_models


def test_autopilot_config_rejects_bad_cron() -> None:
    with pytest.raises(ConfigValidationError, match="must contain 5 fields"):
        AutopilotConfig.from_env({"MEP_AUTOPILOT_CRON": "*/5 * *"})


def test_autopilot_config_rejects_invalid_boolean() -> None:
    with pytest.raises(ConfigValidationError, match="boolean-like"):
        AutopilotConfig.from_env({"MEP_AUTOPILOT_ENABLED": "maybe"})


def test_autopilot_config_rejects_min_bounty_above_max() -> None:
    with pytest.raises(ConfigValidationError, match="cannot be greater"):
        AutopilotConfig.from_env({"MEP_MIN_BOUNTY": "10", "MEP_MAX_BOUNTY": "5"})
