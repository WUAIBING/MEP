#!/usr/bin/env python3
"""Phase 5 Comparative Evaluation Harness.

Compares baseline (prompt-first, MEP_AGENTIC_REVIEW=0) against V1.5
(agentic, MEP_AGENTIC_REVIEW=1) using the existing bridge trial telemetry.

Usage:
    python scripts/run_phase5_benchmark.py --days 30
    python scripts/run_phase5_benchmark.py --scenario approval_safety
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone


def _bridge_store_path() -> str:
    base = os.environ.get("MEP_BRIDGE_DATA_DIR", os.path.join(os.getcwd(), "data"))
    return os.path.join(base, "bridge_store.sqlite")


def _connect_store():
    import sqlite3

    path = _bridge_store_path()
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_trials(
    *,
    days: int = 30,
    model_filter: str = "",
    limit: int = 500,
) -> list[dict]:
    """Fetch review trials from the bridge SQLite store."""
    conn = _connect_store()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    try:
        rows = conn.execute(
            """SELECT rowid, *, review_result_json, review_feedback_json
               FROM review_results
               WHERE created_at >= ?
               ORDER BY created_at DESC
               LIMIT ?""",
            (cutoff, limit),
        ).fetchall()
        trials = []
        for row in rows:
            trial = dict(row)
            if trial.get("review_result_json"):
                try:
                    trial["result"] = json.loads(trial["review_result_json"])
                except (json.JSONDecodeError, TypeError):
                    trial["result"] = {}
            if trial.get("review_feedback_json"):
                try:
                    trial["feedback"] = json.loads(trial["review_feedback_json"])
                except (json.JSONDecodeError, TypeError):
                    trial["feedback"] = {}
            if model_filter and model_filter not in trial.get("model", ""):
                continue
            trials.append(trial)
        return trials
    finally:
        conn.close()


def fetch_trials_via_api(base_url: str, days: int = 30) -> list[dict]:
    """Fallback: fetch trials via the bridge HTTP API."""
    import urllib.request

    url = f"{base_url.rstrip('/')}/bridge/review-trials?days={days}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            return data.get("trials", [])
    except Exception:
        return []


def compute_metrics(trials: list[dict]) -> dict:
    """Compute quality metrics from a set of trials."""
    total = len(trials)
    if total == 0:
        return {"total": 0, "error": "no trials"}

    published = [t for t in trials if t.get("suppression_reason") is None or t.get("suppression_reason") == ""]
    suppressed = [t for t in trials if t.get("suppression_reason")]
    approved = [t for t in trials if t.get("bridge_action") == "approved"]
    approved_green = [t for t in approved if t.get("ci_all_green")]
    useful = [t for t in trials if t.get("feedback", {}).get("verdict") == "useful"]
    false_positive = [t for t in trials if t.get("feedback", {}).get("verdict") == "false_positive"]
    missed = [t for t in trials if t.get("feedback", {}).get("verdict") == "missed_issue"]

    scores = [t.get("quality_score", 0) or 0 for t in published]

    return {
        "total": total,
        "published_count": len(published),
        "suppressed_count": len(suppressed),
        "suppression_rate": len(suppressed) / total if total else 0,
        "approval_count": len(approved),
        "green_approval_publish_rate": len(approved_green) / len(approved) if approved else 1.0,
        "avg_quality_score": sum(scores) / len(scores) if scores else 0,
        "median_quality_score": sorted(scores)[len(scores) // 2] if scores else 0,
        "useful_count": len(useful),
        "false_positive_count": len(false_positive),
        "missed_issue_count": len(missed),
    }


def compare_baseline_to_v1_5(
    *,
    days: int = 30,
    base_url: str = "",
) -> dict:
    """Compare baseline and V1.5 agentic review metrics."""
    # Try SQLite first
    all_trials = fetch_trials(days=days)
    if not all_trials and base_url:
        all_trials = fetch_trials_via_api(base_url, days=days)

    baseline = [t for t in all_trials if not t.get("result", {}).get("agentic", False)]
    agentic = [t for t in all_trials if t.get("result", {}).get("agentic", False)]

    return {
        "baseline": compute_metrics(baseline),
        "agentic_v1_5": compute_metrics(agentic),
        "comparison": {
            "trials_baseline": len(baseline),
            "trials_agentic": len(agentic),
            "suppression_delta": (
                compute_metrics(baseline).get("suppression_rate", 0)
                - compute_metrics(agentic).get("suppression_rate", 0)
            ),
            "quality_score_delta": (
                (compute_metrics(agentic).get("avg_quality_score", 0) or 0)
                - (compute_metrics(baseline).get("avg_quality_score", 0) or 0)
            ),
        },
    }


def print_phase5_report(result: dict) -> None:
    """Print a human-readable Phase 5 report."""
    print("=" * 60)
    print("  MEP Phase 5: Comparative Evaluation Report")
    print("=" * 60)
    baseline = result.get("baseline", {})
    agentic = result.get("agentic_v1_5", {})

    for label, metrics, emoji in [("Baseline", baseline, ""), ("V1.5 Agentic", agentic, "")]:
        t = metrics.get("total", 0)
        if t == 0:
            print(f"\n  {label}: NO DATA (0 trials)")
            continue
        print(f"\n  {label} ({t} trials):")
        print(f"    Published:  {metrics.get('published_count', 0)}")
        print(f"    Suppressed: {metrics.get('suppressed_count', 0)} ({metrics.get('suppression_rate', 0):.1%})")
        print(f"    Approvals:  {metrics.get('approval_count', 0)}")
        print(f"    Green CI approval rate: {metrics.get('green_approval_publish_rate', 0):.1%}")
        print(f"    Avg quality score: {metrics.get('avg_quality_score', 0):.1f}")
        print(f"    Median quality score: {metrics.get('median_quality_score', 0):.1f}")
        print(f"    Useful:     {metrics.get('useful_count', 0)}")
        print(f"    False pos:  {metrics.get('false_positive_count', 0)}")
        print(f"    Missed:     {metrics.get('missed_issue_count', 0)}")

    comp = result.get("comparison", {})
    if comp.get("trials_baseline") and comp.get("trials_agentic"):
        print("\n  Comparison:")
        print(f"    Suppression delta: {comp.get('suppression_delta', 0):+.1%}")
        print(f"    Quality score delta: {comp.get('quality_score_delta', 0):+.1f}")
    print("\n" + "=" * 60)


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 5 comparative evaluation harness")
    parser.add_argument("--days", type=int, default=30, help="Days of trial history to compare")
    parser.add_argument("--base-url", type=str, default="", help="Bridge HTTP base URL (fallback)")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    result = compare_baseline_to_v1_5(days=args.days, base_url=args.base_url)

    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print_phase5_report(result)


if __name__ == "__main__":
    main()
