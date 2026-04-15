from node.mep_autopilot_config import AutopilotConfig, ConfigValidationError
from node.mep_autopilot_daemon import print_status


def main() -> int:
    try:
        config = AutopilotConfig.from_env()
    except ConfigValidationError as exc:
        print(f"[mep status] config error: {exc}")
        return 2
    print_status(config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
