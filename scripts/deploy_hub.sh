#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TARGET_REF="${1:-origin/main}"

require_clean_tree() {
  if [[ -n "$(git -C "$ROOT_DIR" status --porcelain --untracked-files=no)" ]]; then
    echo "Refusing deploy: working tree is dirty in $ROOT_DIR" >&2
    exit 1
  fi
}

require_tool() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required tool: $1" >&2
    exit 1
  fi
}

require_tool git
require_tool docker
require_tool curl
require_tool python3

require_clean_tree

git -C "$ROOT_DIR" fetch origin
TARGET_SHA="$(git -C "$ROOT_DIR" rev-parse "$TARGET_REF")"
git -C "$ROOT_DIR" checkout --force "$TARGET_SHA"

export MEP_BUILD_SHA="$TARGET_SHA"
export MEP_BUILD_TIME="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
export MEP_DEPLOY_SOURCE="scripts/deploy_hub.sh"

cd "$ROOT_DIR"
docker compose up -d --build mep-hub

VERSION_JSON="$(curl -fsS http://127.0.0.1:8000/version)"
printf '%s' "$VERSION_JSON" | python3 - "$TARGET_SHA" <<'PY'
import json
import sys

expected_sha = sys.argv[1]
payload = json.loads(sys.stdin.read())

if payload.get("build_sha") != expected_sha:
    raise SystemExit(f"deploy smoke check failed: expected {expected_sha}, got {payload.get('build_sha')}")

print(json.dumps(payload, indent=2))
PY

echo "Hub deploy smoke check passed for $TARGET_SHA"
