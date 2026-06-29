# GitHub-to-MEP Bridge

This bridge connects GitHub webhooks to MEP Hub so a target bot can receive actionable MEP tasks for PR and issue workflows.

This README is the operator-facing deployment guide for the first GitHub-to-MEP bridge slice.

## Production Routing

The canonical production webhook target is:

`{MEP_BRIDGE_PUBLIC_BASE_URL}/github/webhook`

Example:

`https://bridge.example.com/github/webhook`

Do not use an older hub-side GitHub webhook path as the long-term target after the bridge is deployed. That older path may still exist in some environments as a stopgap, but the bridge endpoint is the production-correct ingress.

## What This Bridge Does

- verifies GitHub webhook signatures
- filters allowed repositories
- parses explicit bot-trigger phrases
- normalizes GitHub events into bridge-owned correlation state
- creates targeted `mep.interbot.v1` DM tasks for MEP Hub
- supports delivery dedup and same-thread coalescence
- ignores bot-authored GitHub trigger comments by default to prevent bot-to-bot ping-pong loops
- emits Telegram status updates
- accepts authenticated `/bridge/status` callbacks from runtime follow-up work

## Current First-Slice Scope

Supported inbound GitHub events:

- `pull_request`
- `issue_comment`
- `pull_request_review_comment`

Current actionable trigger verbs:

- `review` -> `code.review.request`
- `analyze` -> `analysis.request`
- `check` -> `code.review.request`
- `comment` -> `code.review.comment`
- `approve` -> `code.review.approve`
- `triage` -> `issue.triage.request`

Examples:

- `@Hub-Sentinel review this PR`
- `@Hub-Sentinel analyze this issue`
- `@Hub-Sentinel check this PR`
- `@Hub-Sentinel comment on this PR`
- `@Hub-Sentinel approve`
- `@Hub-Sentinel triage this issue`

Current parser limitation:

- first-slice imperative verbs are single-word only
- phrases like `request changes` are not yet implemented as parser-native actionable commands
- bot-authored GitHub comments are output-only by default unless their login is explicitly trusted

## Quick Start

1. Set the required environment variables.
2. Start the bridge service.
3. Configure the GitHub webhook to point to the bridge endpoint.
4. Trigger a GitHub test event with a valid bot invocation.
5. Verify the bridge creates a targeted MEP task and emits Telegram status.

## Environment Variables

### Required

| Variable | Example | Purpose |
| --- | --- | --- |
| `MEP_HUB_URL` | `https://mep-hub.example.com` | MEP Hub base URL used for task submission |
| `MEP_BRIDGE_TARGET_NODE_ID` | `node_b2f19654a37c` | Target bot node ID |
| `MEP_BRIDGE_TARGET_ALIAS` | `Hub Sentinel` | Human-readable target alias |
| `MEP_BRIDGE_PUBLIC_BASE_URL` | `https://bridge.example.com` | Public base URL for webhook ingress and status callback generation |
| `MEP_BRIDGE_STATUS_SECRET` | `replace-me` | HMAC signing key for short-lived status tokens |
| `GITHUB_WEBHOOK_SECRET` | `replace-me` | Shared secret used for GitHub HMAC verification |

### Commonly Used

| Variable | Example | Purpose |
| --- | --- | --- |
| `GITHUB_ALLOWED_REPOS` | `WUAIBING/MEP` | Comma-separated allowlist of `owner/repo` |
| `TELEGRAM_BOT_TOKEN` | `replace-me` | Telegram bot token for status messages |
| `TELEGRAM_CHAT_ID` | `123456789` | Telegram chat ID for status messages |
| `MEP_BRIDGE_TRIGGER_ALIASES` | `Hub-Sentinel` | Comma-separated aliases matched in GitHub text |
| `MEP_BRIDGE_SOURCE_ALIAS` | `GitHub Bridge` | Alias used when the bridge registers with MEP Hub |
| `MEP_BRIDGE_KEY_PATH` | `./bridge/bridge_identity.pem` | Bridge signing key path used for MEP auth |
| `MEP_BRIDGE_SQLITE_PATH` | `./bridge/github_bridge.db` | SQLite persistence path for dedup and correlation |

### Optional Tuning

| Variable | Default | Purpose |
| --- | --- | --- |
| `MEP_BRIDGE_DEDUP_TTL_HOURS` | `72` | How long to remember seen GitHub deliveries |
| `MEP_BRIDGE_COALESCE_WINDOW_SECONDS` | `10` | Buffer window for same-context GitHub bursts |
| `MEP_BRIDGE_COALESCE_MAX_BUFFER_SIZE` | `50` | Max buffered contexts before oldest is flushed |
| `MEP_BRIDGE_MAINTAINER_ONLY` | `true` | Restrict actionable triggers to maintainer-like associations |
| `MEP_BRIDGE_ALLOWED_ASSOCIATIONS` | `OWNER,MEMBER,COLLABORATOR` | Allowed GitHub author associations when maintainer-only is enabled |
| `MEP_BRIDGE_HUMAN_ONLY_TRIGGERS` | `true` | Ignore bot-authored trigger comments unless explicitly trusted |
| `MEP_BRIDGE_TRUSTED_BOT_LOGINS` | `` | Comma-separated bot logins allowed to trigger automation |
| `MEP_BRIDGE_STATUS_TOKEN_LIFETIME_SECONDS` | `1800` | Lifetime for signed `/bridge/status` tokens |
| `MEP_BRIDGE_TELEGRAM_COMPACT` | `true` | Edit a compact Telegram status message instead of emitting many messages |

Documented but not yet enforced in this first bridge-side implementation:

- `MEP_BRIDGE_STALE_DM_SOFT_TTL_SECONDS`
- `MEP_BRIDGE_STALE_DM_HARD_TTL_SECONDS`

These remain approved design follow-up settings, not active runtime behavior in this slice.

## Reviewer Runtime Safety

Production reviewer nodes such as `Hub Sentinel` and `Elsaws Bot` should not silently downgrade from a requested AI adapter to `MockAdapter`.

Set these runtime variables for both reviewer nodes:

- `DEEPSEEK_API_KEY=<real key>`
- `MEP_AI_MODEL=deepseek-chat`
- `MEP_STRICT_ADAPTERS=true`

With `MEP_STRICT_ADAPTERS=true`, `python -m node.mep_runtime --adapter deepseek run` fails closed if `DEEPSEEK_API_KEY` is missing instead of publishing `MOCK_ADAPTER_OK` placeholder output. Keep mock mode only for local onboarding or smoke tests where deterministic non-AI output is intentional.

## Starting The Bridge

The bridge is implemented as a Python module under `bridge/`.

Run it with your preferred ASGI server, for example:

```bash
uvicorn bridge.github_to_mep:app --host 0.0.0.0 --port 8787
```

For a simple production keepalive wrapper on Linux hosts, use:

```bash
chmod +x bridge/run_bridge.sh
./bridge/run_bridge.sh
```

`bridge/run_bridge.sh` launches `uvicorn bridge.github_to_mep:app` from the repo root and restarts after crashes. It does not restart after a clean exit or `Ctrl+C`.

Optional wrapper environment variables:

- `PYTHON_BIN` to select the Python executable, default `python3`
- `MEP_BRIDGE_HOST` to override the bind host, default `0.0.0.0`
- `MEP_BRIDGE_PORT` to override the bind port, default `8787`
- `MEP_BRIDGE_RESTART_DELAY_SECONDS` to change restart backoff, default `5`
- `MEP_BRIDGE_MAX_RESTARTS` to cap retries, default `0` for unlimited restarts

If you deploy behind a reverse proxy, set `MEP_BRIDGE_PUBLIC_BASE_URL` to the externally reachable HTTPS URL.

## GitHub Webhook Setup

1. Open the GitHub repository.
2. Go to `Settings -> Webhooks`.
3. Click `Add webhook`.
4. Set **Payload URL** to:

   `${MEP_BRIDGE_PUBLIC_BASE_URL}/github/webhook`

   Example:

   `https://bridge.example.com/github/webhook`

5. Set **Content type** to `application/json`.
6. Set **Secret** to the same value as `GITHUB_WEBHOOK_SECRET`.
7. Select individual events:
   - `Pull requests`
   - `Issue comments`
   - `Pull request review comments`
   - optionally `Issues`
8. Save the webhook.
9. Use GitHub redelivery to confirm the bridge returns success.

## How The Flow Works

```text
GitHub webhook
-> bridge /github/webhook
-> signature verification + normalization
-> bridge correlation state (delivery_id, bridge_id, context_id)
-> targeted MEP DM/new_task via hub
-> target bot runtime
-> Telegram status updates
-> optional runtime callback to /bridge/status
```

## Runtime Contract

The bridge creates structured DM tasks that include:

- `bridge_id`
- `delivery_id`
- `context_id`
- `status_endpoint`
- `status_token`
- GitHub context fields under `task.inputs.github`

Runtime-side completion callback is a follow-up integration step. The bridge endpoint is already implemented:

- `POST /bridge/status`

Status tokens are short-lived HMAC-SHA256 signed tokens with claims:

- `bridge_id`
- `target_node_id`
- `exp`

Bridge-generated GitHub output comments should be marked with hidden provenance such as `<!-- mep-bridge:output ... -->`. The bridge suppresses trigger processing for comments containing that marker so bot result messages do not recursively reopen automation.

## Testing

Run the focused bridge tests:

```bash
python -m pytest tests/test_github_bridge.py -q
```

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Webhook returns `401` | `GITHUB_WEBHOOK_SECRET` must match GitHub webhook secret |
| Webhook is ignored | repo may not be in `GITHUB_ALLOWED_REPOS`, or trigger text is non-actionable |
| No Telegram status | verify `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` |
| Bot does not act | verify `MEP_BRIDGE_TARGET_NODE_ID` is correct and the target bot is online |
| Duplicate reviews | verify bridge SQLite path is writable and dedup TTL is configured |
| Too many same-thread events | verify coalescence settings and inspect buffered GitHub actions |
| Bots retrigger each other | keep `MEP_BRIDGE_HUMAN_ONLY_TRIGGERS=true` and do not allowlist result-posting bot accounts |

## Related Docs

- [GITHUB_TO_MEP_TELEGRAM_FIRST_SLICE.md](./GITHUB_TO_MEP_TELEGRAM_FIRST_SLICE.md)
