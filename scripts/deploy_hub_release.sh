#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TARGET_REF="${1:-origin/main}"

SOURCE_REPO="${MEP_DEPLOY_SOURCE_REPO:-$ROOT_DIR}"
DEPLOY_BASE_DIR="${MEP_DEPLOY_BASE_DIR:-$HOME/mep-hub}"
RELEASES_DIR="${MEP_DEPLOY_RELEASES_DIR:-$DEPLOY_BASE_DIR/releases}"
CURRENT_LINK="${MEP_DEPLOY_CURRENT_LINK:-$RELEASES_DIR/current}"
LIVE_REPO_DIR="${MEP_DEPLOY_LIVE_REPO_DIR:-$DEPLOY_BASE_DIR/MEP}"
ARCHIVE_DIR="${MEP_DEPLOY_ARCHIVE_DIR:-$DEPLOY_BASE_DIR/live-hotfix-backups}"
SHARED_ENV_FILE="${MEP_DEPLOY_ENV_FILE:-$LIVE_REPO_DIR/.env}"
SHARED_HUB_DATA_DIR="${MEP_DEPLOY_HUB_DATA_DIR:-$LIVE_REPO_DIR/hub_data}"
COMPOSE_PROJECT_NAME="${MEP_DEPLOY_COMPOSE_PROJECT_NAME:-mep}"
VERSION_URL="${MEP_DEPLOY_VERSION_URL:-http://127.0.0.1:8000/version}"
ALLOW_DIRTY_LIVE_TREE="${MEP_DEPLOY_ALLOW_DIRTY_LIVE_TREE:-0}"

require_tool() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required tool: $1" >&2
    exit 1
  fi
}

require_clean_tree() {
  if [[ -n "$(git -C "$1" status --porcelain --untracked-files=no)" ]]; then
    echo "Refusing deploy: source checkout is dirty: $1" >&2
    exit 1
  fi
}

ensure_git_repo() {
  if ! git -C "$1" rev-parse --git-dir >/dev/null 2>&1; then
    echo "Not a git repository: $1" >&2
    exit 1
  fi
}

archive_live_tree_if_needed() {
  local live_status=""
  local timestamp=""
  local backup_dir=""

  if [[ ! -d "$LIVE_REPO_DIR" ]]; then
    return 0
  fi
  if ! git -C "$LIVE_REPO_DIR" rev-parse --git-dir >/dev/null 2>&1; then
    echo "Refusing deploy: live repo dir is not a git checkout: $LIVE_REPO_DIR" >&2
    exit 1
  fi

  live_status="$(git -C "$LIVE_REPO_DIR" status --porcelain)"
  if [[ -z "$live_status" ]]; then
    return 0
  fi

  timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
  backup_dir="$ARCHIVE_DIR/$timestamp"
  mkdir -p "$backup_dir"

  git -C "$LIVE_REPO_DIR" status --short > "$backup_dir/status.txt"
  git -C "$LIVE_REPO_DIR" rev-parse HEAD > "$backup_dir/head.txt"
  git -C "$LIVE_REPO_DIR" diff --binary > "$backup_dir/tracked.patch"
  git -C "$LIVE_REPO_DIR" diff --binary --cached > "$backup_dir/staged.patch"
  git -C "$LIVE_REPO_DIR" ls-files --others --exclude-standard > "$backup_dir/untracked.txt"

  echo "Archived dirty live checkout to $backup_dir" >&2

  if [[ "$ALLOW_DIRTY_LIVE_TREE" != "1" ]]; then
    echo "Refusing deploy: live repo checkout is dirty. Review the archived patch set or rerun with MEP_DEPLOY_ALLOW_DIRTY_LIVE_TREE=1." >&2
    exit 1
  fi
}

prepare_release_checkout() {
  local target_sha="$1"
  local release_dir="$RELEASES_DIR/$target_sha"

  mkdir -p "$RELEASES_DIR"

  if [[ -d "$release_dir/.git" ]]; then
    if [[ -n "$(git -C "$release_dir" status --porcelain --untracked-files=no)" ]]; then
      echo "Refusing deploy: release checkout is dirty: $release_dir" >&2
      exit 1
    fi
  else
    rm -rf "$release_dir"
    git clone "$SOURCE_REPO" "$release_dir" >/dev/null 2>&1 || {
      echo "git clone failed: $SOURCE_REPO" >&2
      exit 1
    }
  fi

  git -C "$release_dir" fetch origin >/dev/null 2>&1
  git -C "$release_dir" checkout --force "$target_sha" >/dev/null 2>&1

  if [[ ! -f "$SHARED_ENV_FILE" ]]; then
    echo "Missing shared env file: $SHARED_ENV_FILE" >&2
    exit 1
  fi

  mkdir -p "$(dirname "$SHARED_HUB_DATA_DIR")"
  mkdir -p "$SHARED_HUB_DATA_DIR"
  rm -rf "$release_dir/hub_data"
  ln -sfn "$SHARED_HUB_DATA_DIR" "$release_dir/hub_data"
  ln -sfn "$SHARED_ENV_FILE" "$release_dir/.env"

  printf '%s\n' "$release_dir"
}

verify_version() {
  local expected_sha="$1"
  local version_json=""

  version_json="$(curl -fsS "$VERSION_URL")"
  VERSION_JSON="$version_json" python3 - "$expected_sha" <<'PY'
import json
import os
import sys

expected_sha = sys.argv[1]
payload = json.loads(os.environ["VERSION_JSON"])

if payload.get("build_sha") != expected_sha:
    raise SystemExit(
        f"deploy smoke check failed: expected {expected_sha}, got {payload.get('build_sha')}"
    )

print(json.dumps(payload, indent=2))
PY
}

require_tool git
require_tool docker
require_tool curl
require_tool python3

ensure_git_repo "$SOURCE_REPO"
require_clean_tree "$SOURCE_REPO"

git -C "$SOURCE_REPO" fetch origin
TARGET_SHA="$(git -C "$SOURCE_REPO" rev-parse "$TARGET_REF")"

archive_live_tree_if_needed
RELEASE_DIR="$(prepare_release_checkout "$TARGET_SHA")"

export MEP_BUILD_SHA="$TARGET_SHA"
export MEP_BUILD_TIME="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
export MEP_DEPLOY_SOURCE="scripts/deploy_hub_release.sh"
export MEP_HUB_LOGS_DIR="$SHARED_HUB_DATA_DIR"

docker compose \
  --project-name "$COMPOSE_PROJECT_NAME" \
  -f "$RELEASE_DIR/docker-compose.yml" \
  build mep-hub

docker rm -f mep-hub >/dev/null 2>&1 || true
docker compose \
  --project-name "$COMPOSE_PROJECT_NAME" \
  -f "$RELEASE_DIR/docker-compose.yml" \
  up -d --no-build --no-deps mep-hub

verify_version "$TARGET_SHA"
ln -sfn "$RELEASE_DIR" "$CURRENT_LINK"

echo "Hub release deploy smoke check passed for $TARGET_SHA"
echo "Release checkout: $RELEASE_DIR"
