import argparse
import json
import time
from datetime import datetime, timezone

from node.mep_autopilot_config import AutopilotConfig, ConfigValidationError


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_status_snapshot(config: AutopilotConfig) -> dict:
    snapshot = config.as_dict()
    snapshot["status"] = "paused" if config.autopilot_pause else "ready"
    snapshot["runtime"] = {
        "mode": "skeleton",
        "timestamp_utc": _utc_now_iso(),
        "note": "PR-A scaffold only. No autonomous DM/compute actions are executed yet.",
    }
    return snapshot


def run_tick(config: AutopilotConfig) -> dict:
    jobs = []
    if config.idle_earn_enabled:
        jobs.append("idle_earn")
    if config.dm_sync_enabled:
        jobs.append("dm_sync")
    if config.compute_sync_enabled:
        jobs.append("compute_sync")

    if not config.autopilot_enabled:
        decision = "autopilot_disabled"
    elif config.autopilot_pause:
        decision = "autopilot_paused"
    elif not jobs:
        decision = "no_jobs_enabled"
    else:
        decision = "jobs_planned_noop"

    return {
        "decision": decision,
        "planned_jobs": jobs,
        "timestamp_utc": _utc_now_iso(),
    }


def print_status(config: AutopilotConfig) -> None:
    print(json.dumps(build_status_snapshot(config), indent=2, sort_keys=True))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mep_autopilot_daemon")
    parser.add_argument("--status", action="store_true", help="Print config/runtime status and exit")
    parser.add_argument("--once", action="store_true", help="Run one scheduling tick and exit")
    parser.add_argument(
        "--sleep-seconds",
        type=float,
        default=30.0,
        help="Loop sleep duration for skeleton mode (used when not --once)",
    )
    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    try:
        config = AutopilotConfig.from_env()
    except ConfigValidationError as exc:
        print(f"[autopilot] config error: {exc}")
        return 2

    if args.status:
        print_status(config)
        return 0

    if args.once:
        print(json.dumps(run_tick(config), indent=2, sort_keys=True))
        return 0

    print("[autopilot] skeleton daemon started. Press Ctrl+C to stop.")
    print_status(config)
    try:
        while True:
            print(json.dumps(run_tick(config), sort_keys=True))
            time.sleep(max(args.sleep_seconds, 0.1))
    except KeyboardInterrupt:
        print("\n[autopilot] stopped by user.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
