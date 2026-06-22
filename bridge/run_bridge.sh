#!/bin/bash
set -u

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
BRIDGE_HOST="${MEP_BRIDGE_HOST:-0.0.0.0}"
BRIDGE_PORT="${MEP_BRIDGE_PORT:-8787}"
RESTART_DELAY="${MEP_BRIDGE_RESTART_DELAY_SECONDS:-5}"
MAX_RESTARTS="${MEP_BRIDGE_MAX_RESTARTS:-0}"
restart_count=0

cd "$ROOT_DIR"

echo "[bridge] starting keepalive wrapper"
echo "[bridge] root=$ROOT_DIR host=$BRIDGE_HOST port=$BRIDGE_PORT max_restarts=$MAX_RESTARTS"

while true; do
    echo "[bridge] $(date '+%Y-%m-%d %H:%M:%S') starting bridge"
    "$PYTHON_BIN" -m uvicorn bridge.github_to_mep:app --host "$BRIDGE_HOST" --port "$BRIDGE_PORT"
    exit_code=$?

    if [ "$exit_code" -eq 0 ] || [ "$exit_code" -eq 130 ]; then
        echo "[bridge] bridge exited cleanly with code $exit_code; not restarting"
        break
    fi

    restart_count=$((restart_count + 1))
    echo "[bridge] bridge crashed with code $exit_code"

    if [ "$MAX_RESTARTS" -gt 0 ] && [ "$restart_count" -ge "$MAX_RESTARTS" ]; then
        echo "[bridge] reached restart limit ($MAX_RESTARTS); exiting"
        break
    fi

    echo "[bridge] restarting in ${RESTART_DELAY}s (attempt $restart_count)"
    sleep "$RESTART_DELAY"
done
