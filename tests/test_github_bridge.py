import hashlib
import hmac
import json
import os
import sys
import tempfile
import time
import asyncio
import unittest
from typing import Any, Optional
from unittest.mock import patch

from fastapi.testclient import TestClient

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from bridge.github_to_mep import (  # noqa: E402
    BridgeConfig,
    BridgeRegistrationPendingApprovalError,
    BridgeStore,
    DefaultMEPSubmissionClient,
    GitHubToMEPBridgeService,
    NormalizedGitHubEvent,
    create_app,
)


class _FakeSubmissionClient:
    def __init__(self):
        self.node_id = "node_bridge"
        self.calls = []

    def submit_structured_dm(self, envelope, target_node_id, intent_type):
        self.calls.append(
            {
                "envelope": envelope,
                "target_node_id": target_node_id,
                "intent_type": intent_type,
            }
        )
        return {
            "status_code": 200,
            "json": {
                "status": "queued",
                "task_id": f"task-{len(self.calls)}",
            },
        }


class _FakeNotifier:
    def __init__(self):
        self.calls = []

    def send_or_edit(self, text, message_id=None):
        if message_id is None:
            next_id = str(len(self.calls) + 100)
        else:
            next_id = str(message_id)
        self.calls.append({"text": text, "message_id": next_id, "editing": message_id is not None})
        return next_id


class _FakeResponse:
    def __init__(self, payload: Any, *, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http_{self.status_code}")

    def json(self):
        return self._payload


class _FakeRequestsSession:
    def __init__(self, responses=None, *, default_response=None, get_responses=None, default_get_response=None):
        self.responses = list(responses or [])
        self.get_responses = list(get_responses or [])
        self.default_response = default_response
        self.default_get_response = default_get_response or _FakeResponse({}, status_code=404)
        self.posts = []
        self.gets = []

    def post(self, url, **kwargs):
        self.posts.append({"url": url, **kwargs})
        if self.responses:
            return self.responses.pop(0)
        if self.default_response is None:
            raise AssertionError("No fake responses remaining")
        return self.default_response

    def get(self, url, **kwargs):
        self.gets.append({"url": url, **kwargs})
        if self.get_responses:
            return self.get_responses.pop(0)
        return self.default_get_response


def _build_config(tmp_dir: str) -> BridgeConfig:
    return BridgeConfig(
        hub_url="http://hub.example.test",
        key_path=os.path.join(tmp_dir, "bridge_identity.pem"),
        sqlite_path=os.path.join(tmp_dir, "bridge.sqlite3"),
        webhook_secret="github-secret",
        github_token="github-token",
        github_writeback_aliases=set(),
        github_writeback_login="bridge-writer",
        github_tokens_by_alias={},
        github_logins_by_alias={},
        target_node_id="node_target",
        target_alias="Hub Sentinel",
        trigger_aliases=["Hub-Sentinel"],
        alias_map={"Hub-Sentinel": "node_target"},
        public_base_url="http://bridge.example.test",
        status_secret="status-secret",
        status_token_lifetime_seconds=1800,
        dedup_ttl_hours=72,
        coalesce_window_seconds=60.0,
        coalesce_max_buffer_size=50,
        allowed_repos={"WUAIBING/MEP"},
        maintainer_only=True,
        allowed_associations={"OWNER", "MEMBER", "COLLABORATOR"},
        human_only_triggers=True,
        trusted_bot_logins=set(),
        bridge_source_alias="GitHub Bridge",
        telegram_bot_token=None,
        telegram_chat_id=None,
        compact_telegram_updates=True,
    )


def _sign_payload(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _issue_comment_payload(
    comment_body: str,
    *,
    action: str = "created",
    delivery_number: int = 226,
    sender_login: str = "alice",
    sender_type: str = "User",
) -> dict:
    return {
        "action": action,
        "repository": {"full_name": "WUAIBING/MEP"},
        "issue": {
            "number": delivery_number,
            "title": "Bridge automation",
            "html_url": f"https://github.com/WUAIBING/MEP/pull/{delivery_number}",
            "pull_request": {"url": f"https://api.github.com/repos/WUAIBING/MEP/pulls/{delivery_number}"},
            "author_association": "MEMBER",
        },
        "comment": {
            "body": comment_body,
            "html_url": f"https://github.com/WUAIBING/MEP/pull/{delivery_number}#discussion_r1",
            "author_association": "MEMBER",
        },
        "sender": {"login": sender_login, "type": sender_type},
    }


class TestGitHubToMEPBridge(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="mep_bridge_")
        self.config = _build_config(self.tmp_dir)
        self.store = BridgeStore(self.config.sqlite_path)
        self.submission = _FakeSubmissionClient()
        self.notifier = _FakeNotifier()
        self.github_session = _FakeRequestsSession(default_response=_FakeResponse({}))
        self.service = GitHubToMEPBridgeService(
            self.config,
            store=self.store,
            submission_client=self.submission,
            notifier=self.notifier,
            github_session=self.github_session,
        )
        self.client = TestClient(create_app(config=self.config, service=self.service))

    def tearDown(self):
        self.client.close()

    def _set_pr_review_package(
        self,
        changed_files: list[dict[str, Any]],
        *,
        pr_body: str = "Bridge review package",
        checks_payload: Optional[dict[str, Any]] = None,
        fetch_cycles: int = 3,
    ) -> None:
        additions = sum(int(item.get("additions") or 0) for item in changed_files)
        deletions = sum(int(item.get("deletions") or 0) for item in changed_files)
        pr_payload = {
            "body": pr_body,
            "changed_files": len(changed_files),
            "additions": additions,
            "deletions": deletions,
            "commits": 1,
            "head": {
                "sha": "headsha123",
                "ref": "headref",
                "repo": {"clone_url": "https://github.com/example/repo.git"},
            },
            "base": {
                "sha": "basesha456",
                "ref": "baseref",
            },
        }
        checks_response = checks_payload or {"total_count": 0, "check_runs": []}
        responses = []
        for _ in range(fetch_cycles):
            responses.extend(
                [
                    _FakeResponse(pr_payload),
                    _FakeResponse(changed_files),
                    _FakeResponse(checks_response),
                ]
            )
        self.github_session.get_responses = responses

    def _post_webhook(self, payload: dict, *, delivery_id: str, event_name: str = "issue_comment"):
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "X-GitHub-Event": event_name,
            "X-GitHub-Delivery": delivery_id,
            "X-Hub-Signature-256": _sign_payload(self.config.webhook_secret, body),
        }
        return self.client.post("/github/webhook", content=body, headers=headers)

    def _flush_context(self, context_id: str) -> None:
        asyncio.run(self.service._flush_context(context_id))

    def _configure_multi_target_aliases(self) -> None:
        self.config.trigger_aliases = ["Hub Sentinel", "Elsaws Bot"]
        self.config.alias_map = {
            "Hub Sentinel": "node_target",
            "Elsaws Bot": "node_elsaws",
        }

    def test_actionable_webhook_submits_structured_dm_after_coalescence(self):
        response = self._post_webhook(
            _issue_comment_payload("@Hub-Sentinel review this PR"),
            delivery_id="delivery-1",
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "buffered")
        self._flush_context(response.json()["context_id"])

        self.assertEqual(len(self.submission.calls), 1)
        call = self.submission.calls[0]
        envelope = call["envelope"]
        self.assertEqual(call["target_node_id"], "node_target")
        self.assertEqual(call["intent_type"], "code.review.request")
        self.assertEqual(envelope["economics"]["bounty_ns"], 0)
        self.assertEqual(envelope["economics"]["currency"], "MEP_NS")
        bridge_metadata = envelope["task"]["inputs"]["bridge_metadata"]
        self.assertEqual(bridge_metadata["source_type"], "github")
        self.assertEqual(bridge_metadata["coalesced_delivery_ids"], ["delivery-1"])
        self.assertIn("/bridge/status", bridge_metadata["status_endpoint"])
        self.assertTrue(bridge_metadata["status_token"])

    def test_extract_trigger_accepts_polite_review_prefixes(self):
        self.assertEqual(
            self.service._extract_trigger("@Hub-Sentinel please review this PR"),
            ("review", "code.review.request"),
        )
        self.assertEqual(
            self.service._extract_trigger("@Hub-Sentinel kindly review this PR"),
            ("review", "code.review.request"),
        )

    def test_extract_trigger_accepts_rereview_variants(self):
        for body in (
            "@Hub-Sentinel re-review this PR",
            "@Hub-Sentinel rereview this PR",
            "@Hub-Sentinel re review this PR",
            "@Hub-Sentinel please re-review this PR",
        ):
            self.assertEqual(
                self.service._extract_trigger(body),
                ("rereview", "code.review.request"),
            )

    def test_duplicate_delivery_is_deduplicated(self):
        payload = _issue_comment_payload("@Hub-Sentinel review this PR")
        first = self._post_webhook(payload, delivery_id="delivery-dup")
        second = self._post_webhook(payload, delivery_id="delivery-dup")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["status"], "duplicate")

    def test_same_context_bursts_are_coalesced_into_one_submission(self):
        payload_one = _issue_comment_payload("@Hub-Sentinel review this PR", action="created")
        payload_two = _issue_comment_payload("@Hub-Sentinel analyze this PR", action="edited")

        first = self._post_webhook(payload_one, delivery_id="delivery-a")
        second = self._post_webhook(payload_two, delivery_id="delivery-b")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self._flush_context(first.json()["context_id"])

        self.assertEqual(len(self.submission.calls), 1)
        bridge_metadata = self.submission.calls[0]["envelope"]["task"]["inputs"]["bridge_metadata"]
        self.assertEqual(set(bridge_metadata["coalesced_delivery_ids"]), {"delivery-a", "delivery-b"})
        self.assertEqual(self.submission.calls[0]["intent_type"], "analysis.request")
        github_inputs = self.submission.calls[0]["envelope"]["task"]["inputs"]["github"]
        self.assertEqual(github_inputs["source_action"], "edited")

    def test_grouped_multi_alias_trigger_routes_each_target(self):
        self._configure_multi_target_aliases()

        response = self._post_webhook(
            _issue_comment_payload("@Hub Sentinel @Elsaws Bot review this PR"),
            delivery_id="delivery-multi",
        )
        self.assertEqual(response.status_code, 200, response.text)

        self._flush_context(response.json()["context_id"])

        self.assertEqual(len(self.submission.calls), 2)
        routed_targets = {call["target_node_id"] for call in self.submission.calls}
        self.assertEqual(routed_targets, {"node_target", "node_elsaws"})
        routed_aliases = {
            call["envelope"]["target"]["alias"]: call["envelope"]["task"]["inputs"]["bridge_metadata"]["bridge_id"]
            for call in self.submission.calls
        }
        self.assertIn("Hub Sentinel", routed_aliases)
        self.assertIn("Elsaws Bot", routed_aliases)
        self.assertNotEqual(routed_aliases["Hub Sentinel"], routed_aliases["Elsaws Bot"])

    def test_same_context_burst_preserves_targets_from_separate_mentions(self):
        self._configure_multi_target_aliases()
        payload_one = _issue_comment_payload("@Hub Sentinel review this PR", action="created")
        payload_two = _issue_comment_payload("@Elsaws Bot review this PR", action="edited")

        first = self._post_webhook(payload_one, delivery_id="delivery-target-a")
        second = self._post_webhook(payload_two, delivery_id="delivery-target-b")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)

        self._flush_context(first.json()["context_id"])

        self.assertEqual(len(self.submission.calls), 2)
        routed_targets = {call["target_node_id"] for call in self.submission.calls}
        self.assertEqual(routed_targets, {"node_target", "node_elsaws"})
        for call in self.submission.calls:
            github_inputs = call["envelope"]["task"]["inputs"]["github"]
            self.assertEqual(github_inputs["source_action"], "edited")

    def test_pr_review_instructions_include_diff_context(self):
        self.github_session.get_responses = [
            _FakeResponse(
                {
                    "body": "Adds review quality handling for bridge-triggered PR reviews.",
                    "changed_files": 1,
                    "additions": 12,
                    "deletions": 3,
                    "commits": 1,
                    "head": {"sha": "abc123head"},
                    "base": {"sha": "def456base"},
                }
            ),
            _FakeResponse(
                [
                    {
                        "filename": "bridge/github_to_mep.py",
                        "status": "modified",
                        "additions": 12,
                        "deletions": 3,
                        "changes": 15,
                        "patch": "@@ -1,3 +1,8 @@\n+def improved_review_context():\n+    return True",
                    }
                ]
            ),
        ]

        response = self._post_webhook(
            _issue_comment_payload("@Hub-Sentinel review this PR"),
            delivery_id="delivery-context",
        )
        self.assertEqual(response.status_code, 200, response.text)

        self._flush_context(response.json()["context_id"])

        self.assertEqual(len(self.submission.calls), 1)
        instructions = self.submission.calls[0]["envelope"]["task"]["instructions"]
        self.assertIn("Review guidance:", instructions)
        self.assertIn("PR description:", instructions)
        self.assertIn("Revision identity:", instructions)
        self.assertIn("Changed files and patch excerpts:", instructions)
        self.assertIn("bridge/github_to_mep.py", instructions)
        github_inputs = self.submission.calls[0]["envelope"]["task"]["inputs"]["github"]
        self.assertEqual(github_inputs["head_sha"], "abc123head")
        self.assertEqual(github_inputs["base_sha"], "def456base")
        self.assertEqual(github_inputs["pr_stats"]["changed_files"], 1)
        self.assertEqual(github_inputs["touched_paths"], ["bridge/github_to_mep.py"])
        self.assertEqual(github_inputs["ci_checks"]["state"], "none")
        self.assertEqual(len(self.github_session.gets), 3)

    def test_pr_review_package_includes_revision_paths_tests_and_risk_tags(self):
        self.github_session.get_responses = [
            _FakeResponse(
                {
                    "body": "Tightens persistence handling and expands bridge tests.",
                    "changed_files": 2,
                    "additions": 25,
                    "deletions": 4,
                    "commits": 2,
                    "head": {"sha": "1234567890abcdef"},
                    "base": {"sha": "fedcba0987654321"},
                }
            ),
            _FakeResponse(
                [
                    {
                        "filename": "hub/db.py",
                        "status": "modified",
                        "additions": 12,
                        "deletions": 3,
                        "changes": 15,
                        "patch": "@@ -1,3 +1,8 @@\n+def write_state():\n+    return True",
                    },
                    {
                        "filename": "tests/test_github_bridge.py",
                        "status": "modified",
                        "additions": 13,
                        "deletions": 1,
                        "changes": 14,
                        "patch": "@@ -1,3 +1,8 @@\n+def test_review_package():\n+    assert True",
                    },
                ]
            ),
        ]

        response = self._post_webhook(
            _issue_comment_payload("@Hub-Sentinel review this PR"),
            delivery_id="delivery-package",
        )
        self.assertEqual(response.status_code, 200, response.text)

        self._flush_context(response.json()["context_id"])

        self.assertEqual(len(self.submission.calls), 1)
        envelope = self.submission.calls[0]["envelope"]
        github_inputs = envelope["task"]["inputs"]["github"]
        instructions = envelope["task"]["instructions"]

        self.assertEqual(github_inputs["delivery_id"], "delivery-package")
        self.assertEqual(github_inputs["head_sha"], "1234567890abcdef")
        self.assertEqual(github_inputs["base_sha"], "fedcba0987654321")
        self.assertEqual(github_inputs["review_mode"], "discovery_review")
        self.assertEqual(
            github_inputs["touched_paths"],
            ["hub/db.py", "tests/test_github_bridge.py"],
        )
        self.assertEqual(github_inputs["touched_tests"], ["tests/test_github_bridge.py"])
        self.assertEqual(github_inputs["ci_checks"]["state"], "none")
        self.assertIn("persistence", github_inputs["risk_tags"])
        self.assertEqual(github_inputs["risk_pack"]["changed_identifiers"], ["write_state", "test_review_package"])
        self.assertIn("persistence", github_inputs["risk_pack"]["risk_tags"])
        self.assertEqual(github_inputs["risk_pack"]["deleted_tests"], [])
        self.assertEqual(github_inputs["hunk_contexts"][0]["filename"], "hub/db.py")
        self.assertEqual(github_inputs["hunk_contexts"][0]["hunk_header"], "@@ -1,3 +1,8 @@")
        self.assertIn("+def write_state()", github_inputs["hunk_contexts"][0]["changed_lines"][0])
        self.assertEqual(github_inputs["coalesced_delivery_ids"], ["delivery-package"])
        self.assertEqual(github_inputs["event_sequence"], 1)
        self.assertEqual(github_inputs["changed_files"][0]["filename"], "hub/db.py")
        self.assertEqual(
            github_inputs["changed_files"][1]["patch_excerpt"],
            "@@ -1,3 +1,8 @@\n+def test_review_package():\n+    assert True",
        )
        self.assertIn("Touched tests:", instructions)
        self.assertIn("Risk tags: persistence", instructions)
        self.assertIn("Deterministic risk pack:", instructions)
        self.assertIn("Hunk-centered context pack:", instructions)
        self.assertIn("Changed identifiers: write_state, test_review_package", instructions)
        self.assertIn("Review mode: discovery_review", instructions)

    def test_rereview_trigger_sets_recheck_review_mode_in_github_inputs(self):
        response = self._post_webhook(
            _issue_comment_payload("@Hub-Sentinel rereview this PR"),
            delivery_id="delivery-rereview-mode",
        )
        self.assertEqual(response.status_code, 200, response.text)

        self._flush_context(response.json()["context_id"])

        envelope = self.submission.calls[0]["envelope"]
        github_inputs = envelope["task"]["inputs"]["github"]
        instructions = envelope["task"]["instructions"]

        self.assertEqual(github_inputs["trigger_verb"], "rereview")
        self.assertEqual(github_inputs["review_mode"], "recheck_review")
        self.assertIn("Requested verb: rereview", instructions)
        self.assertIn("Review mode: recheck_review", instructions)

    def test_pr_review_package_detects_singular_test_path_and_security_tag(self):
        self.github_session.get_responses = [
            _FakeResponse(
                {
                    "body": "Adds validation coverage for webhook security handling.",
                    "changed_files": 2,
                    "additions": 16,
                    "deletions": 2,
                    "commits": 1,
                    "head": {"sha": "bead1234"},
                    "base": {"sha": "face5678"},
                }
            ),
            _FakeResponse(
                [
                    {
                        "filename": "bridge/security_validate.py",
                        "status": "modified",
                        "additions": 10,
                        "deletions": 2,
                        "changes": 12,
                        "patch": "@@ -1,3 +1,6 @@\n+def validate_signature():\n+    return True",
                    },
                    {
                        "filename": "src/test/webhook_security_test.py",
                        "status": "added",
                        "additions": 6,
                        "deletions": 0,
                        "changes": 6,
                        "patch": "@@ -0,0 +1,6 @@\n+def test_webhook_signature():\n+    assert True",
                    },
                ]
            ),
        ]

        response = self._post_webhook(
            _issue_comment_payload("@Hub-Sentinel review this PR", delivery_number=231),
            delivery_id="delivery-security-test",
        )
        self.assertEqual(response.status_code, 200, response.text)

        self._flush_context(response.json()["context_id"])

        self.assertEqual(len(self.submission.calls), 1)
        github_inputs = self.submission.calls[0]["envelope"]["task"]["inputs"]["github"]
        instructions = self.submission.calls[0]["envelope"]["task"]["instructions"]

        self.assertEqual(github_inputs["touched_tests"], ["src/test/webhook_security_test.py"])
        self.assertIn("security", github_inputs["risk_tags"])
        self.assertIn("Touched tests:\n- src/test/webhook_security_test.py", instructions)
        self.assertIn("Risk tags: security", instructions)

    def test_pr_review_package_builds_risk_pack_for_config_and_deleted_tests(self):
        self.github_session.get_responses = [
            _FakeResponse(
                {
                    "body": "Updates runtime environment handling and removes a stale test.",
                    "changed_files": 3,
                    "additions": 18,
                    "deletions": 9,
                    "commits": 1,
                    "head": {"sha": "feed1234"},
                    "base": {"sha": "dead5678"},
                }
            ),
            _FakeResponse(
                [
                    {
                        "filename": ".github/workflows/ci.yml",
                        "status": "modified",
                        "additions": 4,
                        "deletions": 1,
                        "changes": 5,
                        "patch": "@@ -1,3 +1,6 @@\n+env:\n+  MEP_REVIEW_RUN_CHECKS: true\n",
                    },
                    {
                        "filename": "node/mep_runtime.py",
                        "status": "modified",
                        "additions": 10,
                        "deletions": 2,
                        "changes": 12,
                        "patch": (
                            "@@ -10,3 +10,10 @@\n"
                            "+def build_verification_report():\n"
                            "+    value = os.getenv('MEP_REVIEW_RUN_CHECKS')\n"
                            "+    return requests.get('https://example.test').status_code\n"
                        ),
                    },
                    {
                        "filename": "tests/test_legacy_bridge.py",
                        "status": "removed",
                        "additions": 0,
                        "deletions": 6,
                        "changes": 6,
                        "patch": "@@ -1,6 +0,0 @@\n-def test_legacy_bridge():\n-    assert True\n",
                    },
                ]
            ),
        ]

        response = self._post_webhook(
            _issue_comment_payload("@Hub-Sentinel review this PR", delivery_number=263),
            delivery_id="delivery-risk-pack",
        )
        self.assertEqual(response.status_code, 200, response.text)

        self._flush_context(response.json()["context_id"])

        github_inputs = self.submission.calls[0]["envelope"]["task"]["inputs"]["github"]
        instructions = self.submission.calls[0]["envelope"]["task"]["instructions"]

        self.assertEqual(github_inputs["risk_pack"]["changed_identifiers"], ["build_verification_report", "value"])
        self.assertIn("network_io", github_inputs["risk_pack"]["risky_api_hits"])
        self.assertIn("env_config", github_inputs["risk_pack"]["risky_api_hits"])
        self.assertEqual(github_inputs["risk_pack"]["config_paths"], [".github/workflows/ci.yml"])
        self.assertEqual(github_inputs["risk_pack"]["deleted_tests"], ["tests/test_legacy_bridge.py"])
        self.assertIn("Risky API hits:", instructions)
        self.assertIn("env_config", instructions)
        self.assertIn("network_io", instructions)
        self.assertIn("Deleted tests: tests/test_legacy_bridge.py", instructions)

    def test_status_callback_suppresses_approve_without_code_evidence_and_updates_existing_message(self):
        response = self._post_webhook(
            _issue_comment_payload("@Hub-Sentinel approve this PR"),
            delivery_id="delivery-status",
        )
        self.assertEqual(response.status_code, 200)
        bridge_id = response.json()["bridge_id"]
        self._flush_context(response.json()["context_id"])

        self.assertEqual(len(self.submission.calls), 1)
        initial_message = self.notifier.calls[-1]
        self.assertFalse(initial_message["editing"])

        token = self.service._generate_status_token(bridge_id, "node_target")
        status_response = self.client.post(
            "/bridge/status",
            json={
                "bridge_id": bridge_id,
                "status": "completed",
                "target_node_id": "node_target",
                "task_id": "task-1",
                "action": "approved",
                "detail": (
                    "## Review Summary\n\n"
                    "Checked the provided diff.\n\n"
                    "Observation: The changed path is narrow and test-backed.\n\n"
                    "Touched paths reviewed: `node/mep_runtime.py`\n\n"
                    "Tests reviewed: `tests/test_node_runtime.py`."
                ),
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(status_response.status_code, 200, status_response.text)

        self.assertEqual(len(self.notifier.calls), 2)
        final_message = self.notifier.calls[-1]
        self.assertTrue(final_message["editing"])
        self.assertIn("action: retrying", final_message["text"])
        self.assertEqual(len(self.github_session.posts), 0)
        
        # Verify retry task was emitted
        self.assertEqual(len(self.submission.calls), 2)
        retry_task = self.submission.calls[-1]
        self.assertIn("Your previous review was suppressed because: generic_observation", retry_task["envelope"]["task"]["instructions"])
        self.assertEqual(self.service.github_writeback_metrics["reviews_published"], 0)
        self.assertEqual(self.service.github_writeback_metrics["suppressed_weak_reviews"], 1)
        self.assertEqual(self.service.github_writeback_metrics["suppressed_approvals"], 1)
        self.assertEqual(self.service.github_writeback_metrics["last_suppressed_reason"], "generic_observation")

    def test_status_callback_suppresses_approve_without_test_awareness(self):
        self._set_pr_review_package(
            [
                {
                    "filename": "node/mep_runtime.py",
                    "status": "modified",
                    "additions": 6,
                    "deletions": 1,
                    "changes": 7,
                    "patch": (
                        "@@ -945,0 +945,6 @@\n"
                        "+def _record_pending_task_poll_failure(self, status: int, detail: str) -> None:\n"
                        "+    self.pending_task_recovery_metrics['last_poll_status'] = status\n"
                    ),
                },
                {
                    "filename": "tests/test_node_runtime.py",
                    "status": "modified",
                    "additions": 8,
                    "deletions": 0,
                    "changes": 8,
                    "patch": (
                        "@@ -494,0 +494,8 @@\n"
                        "+def test_fetch_pending_tasks_uses_authenticated_get(self):\n"
                        "+    self.assertEqual(tasks, [{'id': 'task_pending'}])\n"
                    ),
                },
            ],
            pr_body="Adds pending-task recovery observability and focused runtime tests.",
        )
        response = self._post_webhook(
            _issue_comment_payload("@Hub-Sentinel approve this PR", delivery_number=245),
            delivery_id="delivery-approve-no-tests",
        )
        self.assertEqual(response.status_code, 200)
        bridge_id = response.json()["bridge_id"]
        self._flush_context(response.json()["context_id"])

        token = self.service._generate_status_token(bridge_id, "node_target")
        status_response = self.client.post(
            "/bridge/status",
            json={
                "bridge_id": bridge_id,
                "status": "completed",
                "target_node_id": "node_target",
                "task_id": "task-approve-no-tests",
                "action": "approved",
                "detail": (
                    "## Review Summary\n\n"
                    "The PR adds pending-task recovery observability in `node/mep_runtime.py`.\n\n"
                    "Observation: `_record_pending_task_poll_failure` now records `last_poll_status`, which makes the recovery metrics more actionable.\n\n"
                    "Touched paths reviewed: `node/mep_runtime.py`."
                ),
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(status_response.status_code, 200, status_response.text)
        self.assertEqual(len(self.github_session.posts), 0)
        self.assertIn("action: retrying", self.notifier.calls[-1]["text"])
        self.assertEqual(self.service.github_writeback_metrics["suppressed_approvals"], 1)
        self.assertEqual(self.service.github_writeback_metrics["last_suppressed_reason"], "approval_without_test_awareness")
        self.assertGreaterEqual(self.service.github_writeback_metrics["last_quality_score"], 2)

    def test_status_callback_queues_retry_when_approve_checks_are_pending(self):
        self._set_pr_review_package(
            [
                {
                    "filename": "node/mep_runtime.py",
                    "status": "modified",
                    "additions": 6,
                    "deletions": 1,
                    "changes": 7,
                    "patch": (
                        "@@ -945,0 +945,6 @@\n"
                        "+def _record_pending_task_poll_failure(self, status: int, detail: str) -> None:\n"
                        "+    self.pending_task_recovery_metrics['last_poll_status'] = status\n"
                    ),
                },
                {
                    "filename": "tests/test_node_runtime.py",
                    "status": "modified",
                    "additions": 8,
                    "deletions": 0,
                    "changes": 8,
                    "patch": (
                        "@@ -494,0 +494,8 @@\n"
                        "+def test_fetch_pending_tasks_uses_authenticated_get(self):\n"
                        "+    self.assertEqual(tasks, [{'id': 'task_pending'}])\n"
                    ),
                },
            ],
            pr_body="Adds pending-task recovery observability and focused runtime tests.",
            checks_payload={
                "total_count": 1,
                "check_runs": [
                    {
                        "name": "test (windows-latest, 3.10)",
                        "status": "in_progress",
                        "conclusion": None,
                    }
                ],
            },
        )
        response = self._post_webhook(
            _issue_comment_payload("@Hub-Sentinel approve this PR", delivery_number=247),
            delivery_id="delivery-approve-checks-pending",
        )
        self.assertEqual(response.status_code, 200)
        bridge_id = response.json()["bridge_id"]
        self._flush_context(response.json()["context_id"])

        token = self.service._generate_status_token(bridge_id, "node_target")
        status_response = self.client.post(
            "/bridge/status",
            json={
                "bridge_id": bridge_id,
                "status": "completed",
                "target_node_id": "node_target",
                "task_id": "task-approve-checks-pending",
                "action": "approved",
                "detail": (
                    "## Review Summary\n\n"
                    "The PR adds pending-task recovery observability in `node/mep_runtime.py` and keeps the verification path narrow.\n\n"
                    "Observation: `_record_pending_task_poll_failure` now records `last_poll_status`, and the changed test keeps the recovery behavior covered. No risky changes.\n\n"
                    "Touched paths reviewed: `node/mep_runtime.py`, `tests/test_node_runtime.py`\n\n"
                    "Tests reviewed: `tests/test_node_runtime.py`."
                ),
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(status_response.status_code, 200, status_response.text)
        self.assertEqual(len(self.github_session.posts), 0)
        self.assertEqual(len(self.submission.calls), 2)
        self.assertIn("action: retrying", self.notifier.calls[-1]["text"])
        self.assertEqual(self.service.github_writeback_metrics["last_suppressed_reason"], "approval_checks_pending")

        execution = self.store.get_execution(bridge_id)
        self.assertIsNotNone(execution)
        trial = execution["review_result"]
        self.assertEqual(trial["attempted_action"], "approved")
        self.assertEqual(trial["resolved_action"], "retrying")
        self.assertTrue(trial["suppressed"])
        self.assertEqual(trial["suppression_reason"], "approval_checks_pending")
        self.assertEqual(trial["ci_state"], "pending")
        self.assertTrue(trial["retry_queued"])
        self.assertEqual(trial["retry_count"], 1)
        self.assertEqual(trial["head_sha"], "headsha123")
        self.assertEqual(trial["anchored_path_count"], 2)
        self.assertGreaterEqual(trial["quality_score"], 4)
        self.assertEqual(execution["status"], "queued")
        self.assertEqual(execution["action"], "retrying")
        self.assertEqual(execution["task_id"], "task-2")
        self.assertEqual(execution["retry_count"], 1)

    def test_status_callback_queues_retry_when_approve_checks_fail(self):
        self._set_pr_review_package(
            [
                {
                    "filename": "node/mep_runtime.py",
                    "status": "modified",
                    "additions": 6,
                    "deletions": 1,
                    "changes": 7,
                    "patch": (
                        "@@ -945,0 +945,6 @@\n"
                        "+def _record_pending_task_poll_failure(self, status: int, detail: str) -> None:\n"
                        "+    self.pending_task_recovery_metrics['last_poll_status'] = status\n"
                    ),
                },
                {
                    "filename": "tests/test_node_runtime.py",
                    "status": "modified",
                    "additions": 8,
                    "deletions": 0,
                    "changes": 8,
                    "patch": (
                        "@@ -494,0 +494,8 @@\n"
                        "+def test_fetch_pending_tasks_uses_authenticated_get(self):\n"
                        "+    self.assertEqual(tasks, [{'id': 'task_pending'}])\n"
                    ),
                },
            ],
            pr_body="Adds pending-task recovery observability and focused runtime tests.",
            checks_payload={
                "total_count": 2,
                "check_runs": [
                    {
                        "name": "test (ubuntu-latest, 3.10)",
                        "status": "completed",
                        "conclusion": "success",
                    },
                    {
                        "name": "test (windows-latest, 3.10)",
                        "status": "completed",
                        "conclusion": "failure",
                    },
                ],
            },
        )
        response = self._post_webhook(
            _issue_comment_payload("@Hub-Sentinel approve this PR", delivery_number=248),
            delivery_id="delivery-approve-checks-fail",
        )
        self.assertEqual(response.status_code, 200)
        bridge_id = response.json()["bridge_id"]
        self._flush_context(response.json()["context_id"])

        token = self.service._generate_status_token(bridge_id, "node_target")
        status_response = self.client.post(
            "/bridge/status",
            json={
                "bridge_id": bridge_id,
                "status": "completed",
                "target_node_id": "node_target",
                "task_id": "task-approve-checks-fail",
                "action": "approved",
                "detail": (
                    "## Review Summary\n\n"
                    "The PR adds pending-task recovery observability in `node/mep_runtime.py` and keeps the verification path narrow.\n\n"
                    "Observation: `_record_pending_task_poll_failure` now records `last_poll_status`, and the changed test keeps the recovery behavior covered. No risky changes.\n\n"
                    "Touched paths reviewed: `node/mep_runtime.py`, `tests/test_node_runtime.py`\n\n"
                    "Tests reviewed: `tests/test_node_runtime.py`."
                ),
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(status_response.status_code, 200, status_response.text)
        self.assertEqual(len(self.github_session.posts), 0)
        self.assertEqual(len(self.submission.calls), 2)
        self.assertIn("action: retrying", self.notifier.calls[-1]["text"])
        self.assertEqual(self.service.github_writeback_metrics["last_suppressed_reason"], "approval_checks_not_green")

        execution = self.store.get_execution(bridge_id)
        self.assertIsNotNone(execution)
        trial = execution["review_result"]
        self.assertEqual(trial["resolved_action"], "retrying")
        self.assertTrue(trial["retry_queued"])
        self.assertEqual(trial["retry_count"], 1)
        self.assertEqual(trial["suppression_reason"], "approval_checks_not_green")
        self.assertEqual(execution["status"], "queued")
        self.assertEqual(execution["action"], "retrying")
        self.assertEqual(execution["task_id"], "task-2")
        self.assertEqual(execution["retry_count"], 1)

    def test_status_callback_allows_approve_with_grounded_code_and_test_awareness(self):
        self._set_pr_review_package(
            [
                {
                    "filename": "node/mep_runtime.py",
                    "status": "modified",
                    "additions": 6,
                    "deletions": 1,
                    "changes": 7,
                    "patch": (
                        "@@ -945,0 +945,6 @@\n"
                        "+def _record_pending_task_poll_failure(self, status: int, detail: str) -> None:\n"
                        "+    self.pending_task_recovery_metrics['last_poll_status'] = status\n"
                    ),
                },
                {
                    "filename": "tests/test_node_runtime.py",
                    "status": "modified",
                    "additions": 8,
                    "deletions": 0,
                    "changes": 8,
                    "patch": (
                        "@@ -494,0 +494,8 @@\n"
                        "+def test_fetch_pending_tasks_uses_authenticated_get(self):\n"
                        "+    self.assertEqual(tasks, [{'id': 'task_pending'}])\n"
                    ),
                },
            ],
            pr_body="Adds pending-task recovery observability and focused runtime tests.",
            checks_payload={
                "total_count": 2,
                "check_runs": [
                    {
                        "name": "test (ubuntu-latest, 3.10)",
                        "status": "completed",
                        "conclusion": "success",
                    },
                    {
                        "name": "test (windows-latest, 3.10)",
                        "status": "completed",
                        "conclusion": "success",
                    },
                ],
            },
        )
        response = self._post_webhook(
            _issue_comment_payload("@Hub-Sentinel approve this PR", delivery_number=246),
            delivery_id="delivery-approve-with-tests",
        )
        self.assertEqual(response.status_code, 200)
        bridge_id = response.json()["bridge_id"]
        self._flush_context(response.json()["context_id"])

        token = self.service._generate_status_token(bridge_id, "node_target")
        status_response = self.client.post(
            "/bridge/status",
            json={
                "bridge_id": bridge_id,
                "status": "completed",
                "target_node_id": "node_target",
                "task_id": "task-approve-with-tests",
                "action": "approved",
                "detail": (
                    "## Review Summary\n\n"
                    "The PR adds pending-task recovery observability in `node/mep_runtime.py` and keeps the verification path narrow.\n\n"
                    "Observation: `_record_pending_task_poll_failure` now records `last_poll_status`, and the changed test keeps the recovery behavior covered. No risky changes.\n\n"
                    "Touched paths reviewed: `node/mep_runtime.py`, `tests/test_node_runtime.py`\n\n"
                    "Tests reviewed: `tests/test_node_runtime.py`."
                ),
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(status_response.status_code, 200, status_response.text)
        self.assertEqual(len(self.github_session.posts), 1)
        self.assertEqual(self.github_session.posts[0]["json"]["event"], "APPROVE")
        self.assertEqual(self.service.github_writeback_metrics["suppressed_approvals"], 0)
        self.assertGreaterEqual(self.service.github_writeback_metrics["last_quality_score"], 4)

    def test_score_review_quality_accepts_professional_low_risk_approval_phrasing(self):
        score, reasons = self.service._score_review_quality(  # noqa: SLF001
            {
                "anchored_paths": {"bridge/github_to_mep.py", "tests/test_github_bridge.py"},
                "changed_tokens": {"_score_review_quality", "PHASE8_STABILITY_TARGETS"},
                "grounded_tokens": {"_score_review_quality", "PHASE8_STABILITY_TARGETS"},
                "has_findings": False,
                "summary_text": (
                    "Verified `_score_review_quality`; no concrete correctness or regression issue is supported."
                ),
                "observation_text": "The reviewed diff does not show a concrete regression trigger.",
                "risk_areas_checked": ["approval gate calibration"],
                "checks_performed": ["reviewed changed diff"],
                "verified_identifiers": ["_score_review_quality", "PHASE8_STABILITY_TARGETS"],
                "expected_tests": ["tests/test_github_bridge.py"],
                "mentions_tests": True,
                "lowered": (
                    "verified _score_review_quality; no concrete correctness or regression issue is supported. "
                    "the reviewed diff does not show a concrete regression trigger. tests reviewed."
                ),
            },
            action="approved",
        )

        self.assertGreaterEqual(score, 6)
        self.assertIn("explicit_low_risk_claim", reasons)

    def test_score_review_quality_clamps_max_score_to_ten(self):
        score, reasons = self.service._score_review_quality(  # noqa: SLF001
            {
                "anchored_paths": {
                    "bridge/github_to_mep.py",
                    "tests/test_github_bridge.py",
                    "node/mep_runtime.py",
                },
                "changed_tokens": {
                    "_score_review_quality",
                    "PHASE8_STABILITY_TARGETS",
                    "_approval_quality_failure",
                },
                "grounded_tokens": {
                    "_score_review_quality",
                    "PHASE8_STABILITY_TARGETS",
                    "_approval_quality_failure",
                },
                "has_findings": True,
                "summary_text": "Verified the scoring path and found a concrete regression risk.",
                "observation_text": "Changed-line evidence supports the same finding.",
                "risk_areas_checked": ["approval gate", "phase8 stability"],
                "checks_performed": ["reviewed changed diff", "checked related tests"],
                "verified_identifiers": [
                    "_score_review_quality",
                    "PHASE8_STABILITY_TARGETS",
                    "_approval_quality_failure",
                ],
                "expected_tests": ["tests/test_github_bridge.py"],
                "mentions_tests": True,
                "lowered": "verified the scoring path. low risk. tests reviewed.",
            },
            action="approved",
        )

        self.assertEqual(score, 10)
        self.assertIn("explicit_low_risk_claim", reasons)

    def test_score_review_quality_empty_snapshot_returns_zero(self):
        score, reasons = self.service._score_review_quality({}, action="reviewed")  # noqa: SLF001

        self.assertEqual(score, 0)
        self.assertEqual(reasons, [])

    def test_review_trials_endpoint_returns_latest_trial_results(self):
        self._set_pr_review_package(
            [
                {
                    "filename": "node/mep_runtime.py",
                    "status": "modified",
                    "additions": 6,
                    "deletions": 1,
                    "changes": 7,
                    "patch": (
                        "@@ -945,0 +945,6 @@\n"
                        "+def _record_pending_task_poll_failure(self, status: int, detail: str) -> None:\n"
                        "+    self.pending_task_recovery_metrics['last_poll_status'] = status\n"
                    ),
                },
                {
                    "filename": "tests/test_node_runtime.py",
                    "status": "modified",
                    "additions": 8,
                    "deletions": 0,
                    "changes": 8,
                    "patch": (
                        "@@ -494,0 +494,8 @@\n"
                        "+def test_fetch_pending_tasks_uses_authenticated_get(self):\n"
                        "+    self.assertEqual(tasks, [{'id': 'task_pending'}])\n"
                    ),
                },
            ],
            pr_body="Adds pending-task recovery observability and focused runtime tests.",
            checks_payload={
                "total_count": 2,
                "check_runs": [
                    {
                        "name": "test (ubuntu-latest, 3.10)",
                        "status": "completed",
                        "conclusion": "success",
                    },
                    {
                        "name": "test (windows-latest, 3.10)",
                        "status": "completed",
                        "conclusion": "success",
                    },
                ],
            },
        )
        response = self._post_webhook(
            _issue_comment_payload("@Hub-Sentinel approve this PR", delivery_number=249),
            delivery_id="delivery-review-trials",
        )
        self.assertEqual(response.status_code, 200)
        bridge_id = response.json()["bridge_id"]
        self._flush_context(response.json()["context_id"])

        token = self.service._generate_status_token(bridge_id, "node_target")
        status_response = self.client.post(
            "/bridge/status",
            json={
                "bridge_id": bridge_id,
                "status": "completed",
                "target_node_id": "node_target",
                "task_id": "task-review-trials",
                "action": "approved",
                "detail": (
                    "## Review Summary\n\n"
                    "The PR adds pending-task recovery observability in `node/mep_runtime.py` and keeps the verification path narrow.\n\n"
                    "Observation: `_record_pending_task_poll_failure` now records `last_poll_status`, and the changed test keeps the recovery behavior covered. No risky changes.\n\n"
                    "Touched paths reviewed: `node/mep_runtime.py`, `tests/test_node_runtime.py`\n\n"
                    "Tests reviewed: `tests/test_node_runtime.py`."
                ),
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(status_response.status_code, 200, status_response.text)

        trials_response = self.client.get("/bridge/review-trials?limit=5")
        self.assertEqual(trials_response.status_code, 200, trials_response.text)
        payload = trials_response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["count"], 1)
        item = payload["items"][0]
        self.assertEqual(item["bridge_id"], bridge_id)
        self.assertEqual(item["target_alias"], "Hub-Sentinel")
        self.assertEqual(item["action"], "approved")
        self.assertEqual(item["review_result"]["resolved_action"], "approved")
        self.assertTrue(item["review_result"]["published"])
        self.assertTrue(item["feedback_required"])
        self.assertFalse(item["feedback_recorded"])
        self.assertEqual(item["feedback_status"], "pending")
        self.assertEqual(item["review_result"]["head_sha"], "headsha123")
        self.assertEqual(item["review_result"]["ci_state"], "green")
        self.assertEqual(payload["summary"]["total_trials"], 1)
        self.assertEqual(payload["summary"]["published_count"], 1)
        self.assertEqual(payload["summary"]["resolved_actions"], {"approved": 1})
        self.assertEqual(payload["summary"]["feedback_pending_count"], 1)
        self.assertEqual(payload["summary"]["feedback_label_coverage"], 0.0)

    def test_completed_status_callback_persists_final_state_in_single_write(self):
        response = self._post_webhook(
            _issue_comment_payload("@Hub-Sentinel review this PR", delivery_number=248),
            delivery_id="delivery-completed-atomic",
        )
        self.assertEqual(response.status_code, 200, response.text)
        bridge_id = response.json()["bridge_id"]
        self._flush_context(response.json()["context_id"])

        token = self.service._generate_status_token(bridge_id, "node_target")
        with patch.object(
            self.service,
            "_write_back_to_github",
            return_value=("reviewed", None, {"published": True, "suppressed": False}),
        ) as writeback_mock, patch.object(
            self.store,
            "update_execution",
            wraps=self.store.update_execution,
        ) as update_mock:
            status_response = self.client.post(
                "/bridge/status",
                json={
                    "bridge_id": bridge_id,
                    "status": "completed",
                    "target_node_id": "node_target",
                    "task_id": "task-completed-atomic",
                    "action": "reviewed",
                    "detail": "Grounded final review body.",
                },
                headers={"Authorization": f"Bearer {token}"},
            )

        self.assertEqual(status_response.status_code, 200, status_response.text)
        writeback_mock.assert_called_once()
        self.assertEqual(update_mock.call_count, 1)
        self.assertEqual(update_mock.call_args.args[0], bridge_id)
        self.assertEqual(update_mock.call_args.kwargs["status"], "completed")
        self.assertEqual(update_mock.call_args.kwargs["task_id"], "task-completed-atomic")
        self.assertEqual(update_mock.call_args.kwargs["action"], "reviewed")
        self.assertTrue(update_mock.call_args.kwargs["review_result"]["published"])
        self.assertEqual(
            update_mock.call_args.kwargs["review_result"],
            {
                "published": True,
                "suppressed": False,
                "resolved_action": "reviewed",
                "retry_queued": False,
                "retry_count": 0,
            },
        )

        execution = self.store.get_execution(bridge_id)
        assert execution is not None
        self.assertEqual(execution["status"], "completed")
        self.assertEqual(execution["task_id"], "task-completed-atomic")
        self.assertEqual(execution["action"], "reviewed")
        self.assertEqual(
            execution["review_result"],
            {
                "published": True,
                "suppressed": False,
                "resolved_action": "reviewed",
                "retry_queued": False,
                "retry_count": 0,
            },
        )

    def test_completed_status_callback_preserves_retrying_execution_state(self):
        response = self._post_webhook(
            _issue_comment_payload("@Hub-Sentinel review this PR", delivery_number=247),
            delivery_id="delivery-completed-retrying-state",
        )
        self.assertEqual(response.status_code, 200, response.text)
        bridge_id = response.json()["bridge_id"]
        self._flush_context(response.json()["context_id"])

        token = self.service._generate_status_token(bridge_id, "node_target")
        status_response = self.client.post(
            "/bridge/status",
            json={
                "bridge_id": bridge_id,
                "status": "completed",
                "target_node_id": "node_target",
                "task_id": "task-completed-retrying",
                "detail": "Too short review",
            },
            headers={"Authorization": f"Bearer {token}"},
        )

        self.assertEqual(status_response.status_code, 200, status_response.text)
        self.assertEqual(len(self.github_session.posts), 0)
        self.assertEqual(len(self.submission.calls), 2)
        self.assertIn("action: retrying", self.notifier.calls[-1]["text"])

        execution = self.store.get_execution(bridge_id)
        assert execution is not None
        self.assertEqual(execution["status"], "queued")
        self.assertEqual(execution["task_id"], "task-2")
        self.assertEqual(execution["action"], "retrying")
        self.assertEqual(execution["retry_count"], 1)
        self.assertEqual(execution["review_result"]["resolved_action"], "retrying")
        self.assertTrue(execution["review_result"]["retry_queued"])
        self.assertEqual(execution["review_result"]["retry_count"], 1)
        self.assertTrue(execution["review_result"]["suppressed"])
        self.assertFalse(execution["review_result"]["published"])

    def test_review_benchmarks_endpoint_returns_seeded_catalog(self):
        response = self.client.get("/bridge/review-benchmarks")
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertGreaterEqual(payload["count"], 6)
        self.assertIn("phase6_precision", payload["suites"])
        self.assertIn("phase8_stability", payload["suites"])
        self.assertEqual(payload["phase8_stability_targets"]["max_non_green_approval_publish_count"], 0)
        first_ids = {item["id"] for item in payload["items"]}
        self.assertIn("docs_only_precision", first_ids)
        self.assertIn("stability_guardrails", first_ids)
        phase8_item = next(item for item in payload["items"] if item["id"] == "stability_guardrails")
        self.assertEqual(phase8_item["fixture_issue_numbers"], [101, 244, 131, 109, 123])

        filtered_response = self.client.get("/bridge/review-benchmarks?suite=phase6_precision")
        self.assertEqual(filtered_response.status_code, 200, filtered_response.text)
        filtered = filtered_response.json()
        self.assertEqual(
            {item["suite"] for item in filtered["items"]},
            {"phase6_precision"},
        )

    def test_review_trial_summary_reports_phase8_stability_guardrails(self):
        summary = self.service._summarize_review_trials(  # noqa: SLF001
            [
                {
                    "intent_type": "code.review.request",
                    "review_result": {
                        "attempted_action": "reviewed",
                        "resolved_action": "reviewed",
                        "published": True,
                        "suppressed": False,
                        "quality_score": 8,
                        "reviewability_bucket": "standard",
                        "ci_state": "green",
                        "intent_type": "code.review.request",
                    },
                },
                {
                    "intent_type": "code.review.request",
                    "review_result": {
                        "attempted_action": "reviewed",
                        "resolved_action": "suppressed",
                        "published": False,
                        "suppressed": True,
                        "quality_score": 8,
                        "reviewability_bucket": "low_signal",
                        "ci_state": "green",
                        "intent_type": "code.review.request",
                    },
                },
                {
                    "intent_type": "code.review.approve",
                    "review_result": {
                        "attempted_action": "approved",
                        "resolved_action": "approved",
                        "published": True,
                        "suppressed": False,
                        "quality_score": 9,
                        "reviewability_bucket": "standard",
                        "ci_state": "green",
                        "intent_type": "code.review.approve",
                    },
                },
                {
                    "intent_type": "code.review.approve",
                    "review_result": {
                        "attempted_action": "approved",
                        "resolved_action": "suppressed",
                        "published": False,
                        "suppressed": True,
                        "quality_score": 9,
                        "reviewability_bucket": "standard",
                        "ci_state": "failing",
                        "intent_type": "code.review.approve",
                    },
                },
            ]
        )

        stability = summary["phase8_stability"]
        self.assertEqual(stability["standard_review_publish_rate"], 1.0)
        self.assertEqual(stability["low_signal_review_suppression_rate"], 1.0)
        self.assertEqual(stability["green_approval_publish_rate"], 1.0)
        self.assertEqual(stability["non_green_approval_suppression_rate"], 1.0)
        self.assertEqual(stability["quality_score_bands"], {"high": 4, "medium": 0, "low": 0})
        self.assertEqual(stability["stability_alerts"], [])
        self.assertTrue(stability["meets_phase8_guardrails"])

    def test_review_trial_summary_flags_phase8_stability_regressions(self):
        summary = self.service._summarize_review_trials(  # noqa: SLF001
            [
                {
                    "intent_type": "code.review.request",
                    "review_result": {
                        "attempted_action": "reviewed",
                        "resolved_action": "suppressed",
                        "published": False,
                        "suppressed": True,
                        "quality_score": 4,
                        "reviewability_bucket": "standard",
                        "ci_state": "green",
                        "intent_type": "code.review.request",
                    },
                },
                {
                    "intent_type": "code.review.request",
                    "review_result": {
                        "attempted_action": "reviewed",
                        "resolved_action": "reviewed",
                        "published": True,
                        "suppressed": False,
                        "quality_score": 8,
                        "reviewability_bucket": "low_signal",
                        "ci_state": "green",
                        "intent_type": "code.review.request",
                    },
                },
                {
                    "intent_type": "code.review.approve",
                    "review_result": {
                        "attempted_action": "approved",
                        "resolved_action": "approved",
                        "published": True,
                        "suppressed": False,
                        "quality_score": 8,
                        "reviewability_bucket": "standard",
                        "ci_state": "failing",
                        "intent_type": "code.review.approve",
                    },
                },
            ]
        )

        stability = summary["phase8_stability"]
        self.assertFalse(stability["meets_phase8_guardrails"])
        self.assertIn("standard_reviews_suppressed", stability["stability_alerts"])
        self.assertIn("low_signal_reviews_published", stability["stability_alerts"])
        self.assertIn("non_green_approvals_published", stability["stability_alerts"])
        self.assertIn("avg_quality_below_phase8_target", stability["stability_alerts"])

    def test_review_trials_feedback_endpoint_records_feedback_and_summary(self):
        checks_payload = {
            "total_count": 2,
            "check_runs": [
                {
                    "name": "test (ubuntu-latest, 3.10)",
                    "status": "completed",
                    "conclusion": "success",
                },
                {
                    "name": "test (windows-latest, 3.10)",
                    "status": "completed",
                    "conclusion": "success",
                },
            ],
        }
        self._set_pr_review_package(
            [
                {
                    "filename": "node/mep_runtime.py",
                    "status": "modified",
                    "additions": 6,
                    "deletions": 1,
                    "changes": 7,
                    "patch": (
                        "@@ -945,0 +945,6 @@\n"
                        "+def _record_pending_task_poll_failure(self, status: int, detail: str) -> None:\n"
                        "+    self.pending_task_recovery_metrics['last_poll_status'] = status\n"
                    ),
                },
                {
                    "filename": "tests/test_node_runtime.py",
                    "status": "modified",
                    "additions": 8,
                    "deletions": 0,
                    "changes": 8,
                    "patch": (
                        "@@ -494,0 +494,8 @@\n"
                        "+def test_fetch_pending_tasks_uses_authenticated_get(self):\n"
                        "+    self.assertEqual(tasks, [{'id': 'task_pending'}])\n"
                    ),
                },
            ],
            pr_body="Adds pending-task recovery observability and focused runtime tests.",
            checks_payload=checks_payload,
        )
        approved_response = self._post_webhook(
            _issue_comment_payload("@Hub-Sentinel approve this PR", delivery_number=270),
            delivery_id="delivery-feedback-approved",
        )
        self.assertEqual(approved_response.status_code, 200)
        approved_bridge_id = approved_response.json()["bridge_id"]
        self._flush_context(approved_response.json()["context_id"])

        approved_token = self.service._generate_status_token(approved_bridge_id, "node_target")
        approved_status = self.client.post(
            "/bridge/status",
            json={
                "bridge_id": approved_bridge_id,
                "status": "completed",
                "target_node_id": "node_target",
                "task_id": "task-feedback-approved",
                "action": "approved",
                "detail": (
                    "## Review Summary\n\n"
                    "The PR adds pending-task recovery observability in `node/mep_runtime.py` and keeps the verification path narrow.\n\n"
                    "Observation: `_record_pending_task_poll_failure` now records `last_poll_status`, and the changed test keeps the recovery behavior covered. No risky changes.\n\n"
                    "Touched paths reviewed: `node/mep_runtime.py`, `tests/test_node_runtime.py`\n\n"
                    "Tests reviewed: `tests/test_node_runtime.py`."
                ),
            },
            headers={"Authorization": f"Bearer {approved_token}"},
        )
        self.assertEqual(approved_status.status_code, 200, approved_status.text)

        self._set_pr_review_package(
            [
                {
                    "filename": "README.md",
                    "status": "modified",
                    "additions": 8,
                    "deletions": 3,
                    "changes": 11,
                    "patch": "@@ -1,3 +1,8 @@\n+# Reviewer docs\n+Updated wording.\n",
                },
                {
                    "filename": "docs/external-bridge/README.md",
                    "status": "modified",
                    "additions": 6,
                    "deletions": 1,
                    "changes": 7,
                    "patch": "@@ -40,1 +40,6 @@\n+Added trigger examples.\n",
                },
            ],
            pr_body="Docs-only clarification for reviewer trigger examples.",
        )
        suppressed_response = self._post_webhook(
            _issue_comment_payload("@Hub-Sentinel review this PR", delivery_number=271),
            delivery_id="delivery-feedback-suppressed",
        )
        self.assertEqual(suppressed_response.status_code, 200)
        suppressed_bridge_id = suppressed_response.json()["bridge_id"]
        self._flush_context(suppressed_response.json()["context_id"])

        suppressed_token = self.service._generate_status_token(suppressed_bridge_id, "node_target")
        suppressed_status = self.client.post(
            "/bridge/status",
            json={
                "bridge_id": suppressed_bridge_id,
                "status": "completed",
                "target_node_id": "node_target",
                "task_id": "task-feedback-suppressed",
                "detail": (
                    "## Review Summary\n\n"
                    "The docs update keeps the trigger examples aligned with the current reviewer behavior.\n\n"
                    "Touched paths reviewed: `README.md`, `docs/external-bridge/README.md`\n\n"
                    "Risk areas checked: operator guidance\n\n"
                    "Checks performed: compared the two README updates for consistency\n\n"
                    "Why no finding: The patch only changes documentation text and does not alter runtime behavior."
                ),
            },
            headers={"Authorization": f"Bearer {suppressed_token}"},
        )
        self.assertEqual(suppressed_status.status_code, 200, suppressed_status.text)

        feedback_response = self.client.post(
            f"/bridge/review-trials/{approved_bridge_id}/feedback",
            json={
                "benchmark_label": "phase6c",
                "verdict": "useful",
                "notes": "Two-pass review was more concrete than the earlier baseline.",
            },
            headers={"Authorization": f"Bearer {self.config.status_secret}"},
        )
        self.assertEqual(feedback_response.status_code, 200, feedback_response.text)
        feedback_payload = feedback_response.json()
        self.assertEqual(feedback_payload["review_feedback"]["benchmark_label"], "phase6c")
        self.assertEqual(feedback_payload["review_feedback"]["verdict"], "useful")

        suppressed_feedback_response = self.client.post(
            f"/bridge/review-trials/{suppressed_bridge_id}/feedback",
            json={
                "benchmark_label": "phase6c",
                "verdict": "not_useful",
                "notes": "Docs-only suppression stayed correctly fail-closed.",
            },
            headers={"Authorization": f"Bearer {self.config.status_secret}"},
        )
        self.assertEqual(suppressed_feedback_response.status_code, 200, suppressed_feedback_response.text)

        trials_response = self.client.get("/bridge/review-trials?limit=5")
        self.assertEqual(trials_response.status_code, 200, trials_response.text)
        payload = trials_response.json()
        self.assertEqual(payload["count"], 2)
        self.assertEqual(payload["total_count"], 2)
        self.assertEqual(payload["summary"]["total_trials"], 2)
        self.assertEqual(payload["summary"]["published_count"], 1)
        self.assertEqual(payload["summary"]["suppressed_count"], 1)
        self.assertEqual(payload["summary"]["resolved_actions"]["approved"], 1)
        self.assertEqual(payload["summary"]["resolved_actions"]["suppressed"], 1)
        self.assertEqual(payload["summary"]["suppression_reasons"]["low_signal_no_finding"], 1)
        self.assertEqual(payload["summary"]["feedback_verdicts"]["useful"], 1)
        self.assertEqual(payload["summary"]["feedback_verdicts"]["not_useful"], 1)
        self.assertEqual(payload["summary"]["benchmark_labels"]["phase6c"], 2)
        self.assertEqual(payload["summary"]["feedback_count"], 2)
        self.assertEqual(payload["summary"]["feedback_pending_count"], 0)
        self.assertEqual(payload["summary"]["feedback_label_coverage"], 1.0)
        self.assertEqual(payload["summary"]["feedback_useful_rate"], 0.5)

        label_filtered_response = self.client.get("/bridge/review-trials?limit=1&benchmark_label=phase6c")
        self.assertEqual(label_filtered_response.status_code, 200, label_filtered_response.text)
        label_filtered = label_filtered_response.json()
        self.assertEqual(label_filtered["count"], 1)
        self.assertEqual(label_filtered["total_count"], 2)
        self.assertEqual(label_filtered["summary"]["total_trials"], 2)
        self.assertEqual(label_filtered["summary"]["published_count"], 1)
        self.assertEqual(label_filtered["summary"]["suppressed_count"], 1)
        self.assertEqual(label_filtered["summary"]["benchmark_labels"]["phase6c"], 2)

        filtered_response = self.client.get(
            "/bridge/review-trials?limit=5&target_alias=Hub-Sentinel&benchmark_label=phase6c&verdict=useful"
        )
        self.assertEqual(filtered_response.status_code, 200, filtered_response.text)
        filtered = filtered_response.json()
        self.assertEqual(filtered["count"], 1)
        self.assertEqual(filtered["total_count"], 1)
        self.assertEqual(filtered["items"][0]["bridge_id"], approved_bridge_id)
        self.assertEqual(filtered["items"][0]["review_feedback"]["verdict"], "useful")

        pending_response = self.client.get("/bridge/review-trials?limit=5&needs_feedback=true")
        self.assertEqual(pending_response.status_code, 200, pending_response.text)
        pending_payload = pending_response.json()
        self.assertEqual(pending_payload["count"], 0)
        self.assertEqual(pending_payload["total_count"], 0)

    def test_review_trials_batch_label_endpoint_labels_latest_trial_per_issue(self):
        def _seed_trial(
            *,
            bridge_id: str,
            issue_number: int,
            intent_type: str,
            action: str,
            published: bool,
            suppressed: bool,
            quality_score: int,
            suppression_reason: Optional[str] = None,
        ) -> None:
            event = NormalizedGitHubEvent(
                delivery_id=f"delivery-{bridge_id}",
                source_event="issue_comment",
                source_action="created",
                repo_full_name="WUAIBING/MEP",
                entity_type="pr",
                number=issue_number,
                title=f"PR {issue_number}",
                url=f"https://example.test/pr/{issue_number}",
                actor_login="moltbot",
                author_association="COLLABORATOR",
                context_id=f"github-WUAIBING-MEP-pr-{issue_number}-{bridge_id}",
                imperative_verb="review" if intent_type == "code.review.request" else "approve",
                intent_type=intent_type,
                instructions="seeded test execution",
                raw_trigger_text="@Hub-Sentinel review this PR",
                github_inputs={"pull_request": {"number": issue_number}},
            )
            self.store.create_execution(
                event,
                bridge_id,
                "node_target",
                target_alias="Hub-Sentinel",
                instructions=event.instructions,
            )
            self.store.update_execution(
                bridge_id,
                status="completed",
                action=action,
                review_result={
                    "resolved_action": action,
                    "published": published,
                    "suppressed": suppressed,
                    "retry_queued": False,
                    "quality_score": quality_score,
                    "suppression_reason": suppression_reason,
                },
            )

        _seed_trial(
            bridge_id="br-old-101",
            issue_number=101,
            intent_type="code.review.request",
            action="suppressed",
            published=False,
            suppressed=True,
            quality_score=6,
            suppression_reason="generic_observation",
        )
        time.sleep(0.01)
        _seed_trial(
            bridge_id="br-new-101",
            issue_number=101,
            intent_type="code.review.request",
            action="reviewed",
            published=True,
            suppressed=False,
            quality_score=9,
        )
        time.sleep(0.01)
        _seed_trial(
            bridge_id="br-131",
            issue_number=131,
            intent_type="code.review.request",
            action="suppressed",
            published=False,
            suppressed=True,
            quality_score=7,
            suppression_reason="low_signal_no_finding",
        )

        response = self.client.post(
            "/bridge/review-trials/batch-label",
            json={
                "scenario_id": "stability_guardrails",
                "target_alias": "Hub-Sentinel",
            },
            headers={"Authorization": f"Bearer {self.config.status_secret}"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["count"], 2)
        self.assertEqual(payload["scenario_id"], "stability_guardrails")
        self.assertEqual(payload["resolved_issue_numbers"], [101, 244, 131, 109, 123])
        self.assertEqual(payload["missing_issue_numbers"], [244, 109, 123])
        self.assertTrue(payload["benchmark_label"].startswith("stability_guardrails_"))
        self.assertEqual(payload["summary"]["total_trials"], 2)
        self.assertEqual(payload["summary"]["published_count"], 1)
        self.assertEqual(payload["summary"]["suppressed_count"], 1)
        self.assertEqual(payload["summary"]["benchmark_labels"][payload["benchmark_label"]], 2)
        self.assertEqual({item["bridge_id"] for item in payload["items"]}, {"br-new-101", "br-131"})

        old_execution = self.store.get_execution("br-old-101")
        self.assertEqual(old_execution["review_feedback"], {})
        new_execution = self.store.get_execution("br-new-101")
        self.assertEqual(new_execution["review_feedback"]["benchmark_label"], payload["benchmark_label"])

        filtered_response = self.client.get(f"/bridge/review-trials?limit=1&benchmark_label={payload['benchmark_label']}")
        self.assertEqual(filtered_response.status_code, 200, filtered_response.text)
        filtered = filtered_response.json()
        self.assertEqual(filtered["count"], 1)
        self.assertEqual(filtered["total_count"], 2)
        self.assertEqual(filtered["summary"]["total_trials"], 2)
        self.assertEqual(filtered["summary"]["published_count"], 1)
        self.assertEqual(filtered["summary"]["suppressed_count"], 1)

    def test_review_feedback_endpoint_requires_valid_feedback_token(self):
        self._set_pr_review_package(
            [
                {
                    "filename": "node/mep_runtime.py",
                    "status": "modified",
                    "additions": 6,
                    "deletions": 1,
                    "changes": 7,
                    "patch": (
                        "@@ -945,0 +945,6 @@\n"
                        "+def _record_pending_task_poll_failure(self, status: int, detail: str) -> None:\n"
                        "+    self.pending_task_recovery_metrics['last_poll_status'] = status\n"
                    ),
                }
            ],
            pr_body="Adds pending-task recovery observability.",
        )
        response = self._post_webhook(
            _issue_comment_payload("@Hub-Sentinel review this PR", delivery_number=272),
            delivery_id="delivery-feedback-auth",
        )
        self.assertEqual(response.status_code, 200)
        bridge_id = response.json()["bridge_id"]
        self._flush_context(response.json()["context_id"])

        token = self.service._generate_status_token(bridge_id, "node_target")
        status_response = self.client.post(
            "/bridge/status",
            json={
                "bridge_id": bridge_id,
                "status": "completed",
                "target_node_id": "node_target",
                "task_id": "task-feedback-auth",
                "detail": (
                    "## Review Summary\n\n"
                    "The PR adds pending-task recovery observability.\n\n"
                    "Observation: `_record_pending_task_poll_failure` records the latest poll status.\n\n"
                    "Touched paths reviewed: `node/mep_runtime.py`."
                ),
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(status_response.status_code, 200, status_response.text)

        feedback_response = self.client.post(
            f"/bridge/review-trials/{bridge_id}/feedback",
            json={"verdict": "useful"},
            headers={"Authorization": "Bearer wrong-token"},
        )
        self.assertEqual(feedback_response.status_code, 403, feedback_response.text)

    def test_status_callback_suppresses_generic_pr_review_writeback(self):
        response = self._post_webhook(
            _issue_comment_payload("@Hub-Sentinel review this PR", delivery_number=231),
            delivery_id="delivery-weak-review",
        )
        self.assertEqual(response.status_code, 200)
        bridge_id = response.json()["bridge_id"]
        self._flush_context(response.json()["context_id"])

        token = self.service._generate_status_token(bridge_id, "node_target")
        status_response = self.client.post(
            "/bridge/status",
            json={
                "bridge_id": bridge_id,
                "status": "completed",
                "target_node_id": "node_target",
                "task_id": "task-weak-review",
                "detail": "The PR adds metrics and logging for pending-task recovery, plus focused runtime tests. The changes are minimal and well-scoped.",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(status_response.status_code, 200, status_response.text)
        self.assertEqual(len(self.github_session.posts), 0)
        self.assertIn("action: retrying", self.notifier.calls[-1]["text"])
        self.assertEqual(len(self.submission.calls), 2)
        retry_task = self.submission.calls[-1]
        self.assertIn("Your previous review was suppressed because: generic_summary", retry_task["envelope"]["task"]["instructions"])
        self.assertEqual(self.service.github_writeback_metrics["attempts"], 1)
        self.assertEqual(self.service.github_writeback_metrics["suppressed_weak_reviews"], 1)
        self.assertEqual(self.service.github_writeback_metrics["last_suppressed_reason"], "generic_summary")

    def test_retry_task_preserves_original_github_inputs(self):
        self._set_pr_review_package(
            [
                {
                    "filename": "bridge/github_to_mep.py",
                    "status": "modified",
                    "additions": 4,
                    "deletions": 1,
                    "changes": 5,
                    "patch": (
                        "@@ -1,2 +1,5 @@\n"
                        "+def preserve_retry_context():\n"
                        "+    return True\n"
                    ),
                }
            ],
            pr_body="Ensures retry submissions keep the original review package metadata.",
        )
        response = self._post_webhook(
            _issue_comment_payload("@Hub-Sentinel review this PR", delivery_number=234),
            delivery_id="delivery-retry-context",
        )
        self.assertEqual(response.status_code, 200)
        bridge_id = response.json()["bridge_id"]
        self._flush_context(response.json()["context_id"])

        token = self.service._generate_status_token(bridge_id, "node_target")
        status_response = self.client.post(
            "/bridge/status",
            json={
                "bridge_id": bridge_id,
                "status": "completed",
                "target_node_id": "node_target",
                "detail": "The change looks fine and focused.",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(status_response.status_code, 200, status_response.text)
        self.assertEqual(len(self.submission.calls), 2)
        retry_envelope = self.submission.calls[-1]["envelope"]
        github_inputs = retry_envelope["task"]["inputs"]["github"]
        self.assertEqual(github_inputs["head_sha"], "headsha123")
        self.assertEqual(github_inputs["head_ref"], "headref")
        self.assertEqual(github_inputs["repo_clone_url"], "https://github.com/example/repo.git")
        self.assertEqual(github_inputs["touched_paths"], ["bridge/github_to_mep.py"])

    def test_retry_task_refreshes_ci_checks_for_suppressed_approval(self):
        changed_files = [
            {
                "filename": "bridge/github_to_mep.py",
                "status": "modified",
                "additions": 4,
                "deletions": 1,
                "changes": 5,
                "patch": (
                    "@@ -1,2 +1,5 @@\n"
                    "+def preserve_retry_context():\n"
                    "+    return True\n"
                ),
            }
        ]
        pr_payload = {
            "body": "Ensures retry submissions refresh mutable PR metadata.",
            "changed_files": len(changed_files),
            "additions": 4,
            "deletions": 1,
            "commits": 1,
            "head": {
                "sha": "headsha123",
                "ref": "headref",
                "repo": {"clone_url": "https://github.com/example/repo.git"},
            },
            "base": {
                "sha": "basesha456",
                "ref": "baseref",
            },
        }
        pending_checks = {
            "total_count": 1,
            "check_runs": [
                {
                    "name": "test (windows-latest, 3.10)",
                    "status": "in_progress",
                    "conclusion": None,
                }
            ],
        }
        green_checks = {
            "total_count": 2,
            "check_runs": [
                {
                    "name": "test (ubuntu-latest, 3.10)",
                    "status": "completed",
                    "conclusion": "success",
                },
                {
                    "name": "test (windows-latest, 3.10)",
                    "status": "completed",
                    "conclusion": "success",
                },
            ],
        }
        self.github_session.get_responses = [
            _FakeResponse(pr_payload),
            _FakeResponse(changed_files),
            _FakeResponse(pending_checks),
            _FakeResponse(pr_payload),
            _FakeResponse(changed_files),
            _FakeResponse(pending_checks),
            _FakeResponse(pr_payload),
            _FakeResponse(changed_files),
            _FakeResponse(green_checks),
        ]
        response = self._post_webhook(
            _issue_comment_payload("@Hub-Sentinel approve this PR", delivery_number=234),
            delivery_id="delivery-retry-ci-refresh",
        )
        self.assertEqual(response.status_code, 200)
        bridge_id = response.json()["bridge_id"]
        self._flush_context(response.json()["context_id"])

        token = self.service._generate_status_token(bridge_id, "node_target")
        status_response = self.client.post(
            "/bridge/status",
            json={
                "bridge_id": bridge_id,
                "status": "completed",
                "target_node_id": "node_target",
                "detail": (
                    "## Review Summary\n\n"
                    "The PR keeps retry handling focused.\n\n"
                    "Observation: `preserve_retry_context` is narrow and grounded.\n\n"
                    "Touched paths reviewed: `bridge/github_to_mep.py`\n\n"
                    "Tests reviewed: `tests/test_github_bridge.py`."
                ),
                "action": "approved",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(status_response.status_code, 200, status_response.text)
        self.assertEqual(len(self.submission.calls), 2)
        retry_envelope = self.submission.calls[-1]["envelope"]
        github_inputs = retry_envelope["task"]["inputs"]["github"]
        self.assertEqual(github_inputs["head_sha"], "headsha123")
        self.assertEqual(github_inputs["head_ref"], "headref")
        self.assertEqual(github_inputs["repo_clone_url"], "https://github.com/example/repo.git")
        self.assertEqual(github_inputs["ci_checks"]["state"], "green")
        self.assertTrue(github_inputs["ci_checks"]["all_green"])

    def test_pr_review_submission_compacts_review_package_payload(self):
        large_patch = "@@ -1,1 +1,120 @@\n" + "\n".join(
            f"+line_{index:03d} = 'payload'" for index in range(120)
        )
        self._set_pr_review_package(
            [
                {
                    "filename": "bridge/github_to_mep.py",
                    "status": "modified",
                    "additions": 120,
                    "deletions": 1,
                    "changes": 121,
                    "patch": large_patch,
                },
                {
                    "filename": "tests/test_github_bridge.py",
                    "status": "modified",
                    "additions": 120,
                    "deletions": 1,
                    "changes": 121,
                    "patch": large_patch,
                },
            ],
            pr_body="Large review payload regression.",
        )

        response = self._post_webhook(
            _issue_comment_payload("@Hub-Sentinel review this PR", delivery_number=236),
            delivery_id="delivery-compact-review-package",
        )
        self.assertEqual(response.status_code, 200, response.text)
        self._flush_context(response.json()["context_id"])
        self.assertEqual(len(self.submission.calls), 1)

        envelope = self.submission.calls[0]["envelope"]
        github_inputs = envelope["task"]["inputs"]["github"]
        serialized = json.dumps(envelope)
        instructions = envelope["task"]["instructions"]

        self.assertLess(len(serialized), 20_000)
        self.assertIn("changed_files", github_inputs)
        self.assertNotIn("patch", github_inputs["changed_files"][0])
        self.assertIn("patch_excerpt", github_inputs["changed_files"][0])
        self.assertNotIn("instructions_context", github_inputs)
        self.assertLessEqual(len(instructions), 3803)

    def test_fetch_pr_review_package_marks_docs_only_patch_as_low_signal(self):
        self._set_pr_review_package(
            [
                {
                    "filename": "README.md",
                    "status": "modified",
                    "additions": 8,
                    "deletions": 3,
                    "changes": 11,
                    "patch": "@@ -1,3 +1,8 @@\n+# Reviewer docs\n+Updated wording.\n",
                },
                {
                    "filename": "docs/external-bridge/README.md",
                    "status": "modified",
                    "additions": 6,
                    "deletions": 1,
                    "changes": 7,
                    "patch": "@@ -40,1 +40,6 @@\n+Added trigger examples.\n",
                },
            ],
            pr_body="Docs-only clarification for reviewer trigger examples.",
        )

        review_package = self.service._fetch_pr_review_package("WUAIBING/MEP", 262)

        self.assertEqual(review_package["reviewability"]["bucket"], "low_signal")
        self.assertEqual(review_package["reviewability"]["reasons"], ["docs_only_patch"])
        self.assertFalse(review_package["reviewability"]["publish_no_finding"])
        self.assertIn("Reviewability assessment:", review_package["instructions_context"])

    def test_status_callback_suppresses_low_signal_no_finding_without_retry(self):
        self._set_pr_review_package(
            [
                {
                    "filename": "README.md",
                    "status": "modified",
                    "additions": 8,
                    "deletions": 3,
                    "changes": 11,
                    "patch": "@@ -1,3 +1,8 @@\n+# Reviewer docs\n+Updated wording.\n",
                },
                {
                    "filename": "docs/external-bridge/README.md",
                    "status": "modified",
                    "additions": 6,
                    "deletions": 1,
                    "changes": 7,
                    "patch": "@@ -40,1 +40,6 @@\n+Added trigger examples.\n",
                },
            ],
            pr_body="Docs-only clarification for reviewer trigger examples.",
        )
        response = self._post_webhook(
            _issue_comment_payload("@Hub-Sentinel review this PR", delivery_number=262),
            delivery_id="delivery-low-signal-docs",
        )
        self.assertEqual(response.status_code, 200)
        bridge_id = response.json()["bridge_id"]
        self._flush_context(response.json()["context_id"])

        token = self.service._generate_status_token(bridge_id, "node_target")
        status_response = self.client.post(
            "/bridge/status",
            json={
                "bridge_id": bridge_id,
                "status": "completed",
                "target_node_id": "node_target",
                "task_id": "task-low-signal-docs",
                "detail": (
                    "## Review Summary\n\n"
                    "The docs update keeps the trigger examples aligned with the current reviewer behavior.\n\n"
                    "Touched paths reviewed: `README.md`, `docs/external-bridge/README.md`\n\n"
                    "Risk areas checked: operator guidance\n\n"
                    "Checks performed: compared the two README updates for consistency\n\n"
                    "Why no finding: The patch only changes documentation text and does not alter runtime behavior."
                ),
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(status_response.status_code, 200, status_response.text)
        self.assertEqual(len(self.github_session.posts), 0)
        self.assertEqual(len(self.submission.calls), 1)
        self.assertIn("action: suppressed", self.notifier.calls[-1]["text"])
        self.assertEqual(self.service.github_writeback_metrics["last_suppressed_reason"], "low_signal_no_finding")

        execution = self.store.get_execution(bridge_id)
        self.assertIsNotNone(execution)
        trial = execution["review_result"]
        self.assertEqual(trial["resolved_action"], "suppressed")
        self.assertFalse(trial["retry_queued"])
        self.assertEqual(trial["reviewability_bucket"], "low_signal")
        self.assertEqual(trial["reviewability_reasons"], ["docs_only_patch"])

    def test_status_callback_keeps_action_suppressed_when_retry_submit_fails(self):
        response = self._post_webhook(
            _issue_comment_payload("@Hub-Sentinel review this PR", delivery_number=235),
            delivery_id="delivery-retry-fail",
        )
        self.assertEqual(response.status_code, 200)
        bridge_id = response.json()["bridge_id"]
        self._flush_context(response.json()["context_id"])

        token = self.service._generate_status_token(bridge_id, "node_target")
        with patch.object(self.submission, "submit_structured_dm", side_effect=RuntimeError("retry submit failed")):
            status_response = self.client.post(
                "/bridge/status",
                json={
                    "bridge_id": bridge_id,
                    "status": "completed",
                    "target_node_id": "node_target",
                    "detail": "Too short review",
                },
                headers={"Authorization": f"Bearer {token}"},
            )

        self.assertEqual(status_response.status_code, 200, status_response.text)
        self.assertIn("action: suppressed", self.notifier.calls[-1]["text"])
        execution = self.store.get_execution(bridge_id)
        self.assertIsNotNone(execution)
        self.assertEqual(execution["action"], "suppressed")
        self.assertEqual(execution["retry_count"], 0)

    def test_status_callback_stops_retrying_after_max_retries(self):
        response = self._post_webhook(
            _issue_comment_payload("@Hub-Sentinel review this PR", delivery_number=233),
            delivery_id="delivery-max-retry",
        )
        bridge_id = response.json()["bridge_id"]
        self._flush_context(response.json()["context_id"])

        token = self.service._generate_status_token(bridge_id, "node_target")
        
        # Retry 1
        self.client.post(
            "/bridge/status",
            json={
                "bridge_id": bridge_id,
                "status": "completed",
                "target_node_id": "node_target",
                "detail": "Too short review 1",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertIn("action: retrying", self.notifier.calls[-1]["text"])
        self.assertEqual(len(self.submission.calls), 2)
        
        # Retry 2
        self.client.post(
            "/bridge/status",
            json={
                "bridge_id": bridge_id,
                "status": "completed",
                "target_node_id": "node_target",
                "detail": "Too short review 2",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertIn("action: retrying", self.notifier.calls[-1]["text"])
        self.assertEqual(len(self.submission.calls), 3)
        
        # Should stop now (Retry 3)
        self.client.post(
            "/bridge/status",
            json={
                "bridge_id": bridge_id,
                "status": "completed",
                "target_node_id": "node_target",
                "detail": "Too short review 3",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertIn("action: suppressed", self.notifier.calls[-1]["text"])
        self.assertEqual(len(self.submission.calls), 3) # No new call

    def test_status_callback_suppresses_structured_review_without_touched_path_anchor(self):
        response = self._post_webhook(
            _issue_comment_payload("@Hub-Sentinel review this PR", delivery_number=232),
            delivery_id="delivery-structured-weak-review",
        )
        self.assertEqual(response.status_code, 200)
        bridge_id = response.json()["bridge_id"]
        self._flush_context(response.json()["context_id"])

        token = self.service._generate_status_token(bridge_id, "node_target")
        status_response = self.client.post(
            "/bridge/status",
            json={
                "bridge_id": bridge_id,
                "status": "completed",
                "target_node_id": "node_target",
                "task_id": "task-structured-weak-review",
                "detail": (
                    "## Review Summary\n\n"
                    "Checked the provided diff and verified the change is scoped.\n\n"
                    "Observation: The change reads like a small follow-up and the implementation style stays consistent."
                ),
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(status_response.status_code, 200, status_response.text)
        self.assertEqual(len(self.github_session.posts), 0)
        self.assertEqual(self.service.github_writeback_metrics["last_suppressed_reason"], "generic_observation")

    def test_status_callback_salvages_generic_observation_into_publishable_summary(self):
        self._set_pr_review_package(
            [
                {
                    "filename": "hub/auth.py",
                    "status": "modified",
                    "patch": (
                        "@@ -88,0 +88,7 @@\n"
                        "+def verify_signature(payload: str, signature: str) -> bool:\n"
                        "+    nonce = _evict_expired_nonces()\n"
                    ),
                },
                {
                    "filename": "hub/models.py",
                    "status": "modified",
                    "patch": (
                        "@@ -10,0 +10,5 @@\n"
                        "+class NodeRegistration(BaseModel):\n"
                        "+    validator = field_validator('callback_url')\n"
                    ),
                },
            ],
            pr_body="Adds replay protection and validator hardening.",
        )
        response = self._post_webhook(
            _issue_comment_payload("@Hub-Sentinel review this PR", delivery_number=245),
            delivery_id="delivery-salvage-generic-observation",
        )
        self.assertEqual(response.status_code, 200)
        bridge_id = response.json()["bridge_id"]
        self._flush_context(response.json()["context_id"])

        token = self.service._generate_status_token(bridge_id, "node_target")
        status_response = self.client.post(
            "/bridge/status",
            json={
                "bridge_id": bridge_id,
                "status": "completed",
                "target_node_id": "node_target",
                "task_id": "task-salvage-generic-observation",
                "detail": (
                    "## Review Summary\n\n"
                    "The PR adds replay-protection and validation hardening across the authentication and model layers.\n\n"
                    "Observation: The change looks coherent and follows the surrounding style.\n\n"
                    "Touched paths reviewed: `hub/auth.py`, `hub/models.py`\n\n"
                    "Risk areas checked: replay protection, validator coverage\n\n"
                    "Checks performed: reviewed the changed diff for `hub/auth.py` and `hub/models.py`, checked the new `verify_signature` and `NodeRegistration` paths\n\n"
                    "Why no finding: The added `verify_signature` and `NodeRegistration` validation paths look internally consistent with the changed behavior."
                ),
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(status_response.status_code, 200, status_response.text)
        self.assertEqual(len(self.github_session.posts), 1)
        review_payload = self.github_session.posts[0]["json"]
        self.assertEqual(review_payload["event"], "COMMENT")
        self.assertIn("## Review Summary", review_payload["body"])
        self.assertEqual(self.service.github_writeback_metrics["last_suppressed_reason"], None)

    def test_status_callback_salvages_partial_diff_caveat_observation(self):
        self._set_pr_review_package(
            [
                {
                    "filename": "bridge/github_to_mep.py",
                    "status": "modified",
                    "additions": 12,
                    "deletions": 2,
                    "changes": 14,
                    "patch": (
                        "@@ -2577,2 +2577,4 @@\n"
                        "+def _suppression_reason_allows_retry(reason: Optional[str]) -> bool:\n"
                        "+    return reason not in {\"low_signal_no_finding\"}\n"
                    ),
                },
                {
                    "filename": "tests/test_github_bridge.py",
                    "status": "modified",
                    "additions": 18,
                    "deletions": 0,
                    "changes": 18,
                    "patch": (
                        "@@ -735,0 +735,18 @@\n"
                        "+def test_status_callback_queues_retry_when_approve_checks_are_pending(self):\n"
                        "+    self.assertTrue(trial[\"retry_queued\"])\n"
                    ),
                },
            ],
            pr_body="Hardens retry handling for suppressed approval review writebacks.",
        )
        response = self._post_webhook(
            _issue_comment_payload("@Hub-Sentinel review this PR", delivery_number=309),
            delivery_id="delivery-partial-diff-caveat",
        )
        self.assertEqual(response.status_code, 200)
        bridge_id = response.json()["bridge_id"]
        self._flush_context(response.json()["context_id"])

        token = self.service._generate_status_token(bridge_id, "node_target")
        status_response = self.client.post(
            "/bridge/status",
            json={
                "bridge_id": bridge_id,
                "status": "completed",
                "target_node_id": "node_target",
                "task_id": "task-partial-diff-caveat",
                "detail": (
                    "## Review Summary\n\n"
                    "The retry handling changes stay focused on approval suppression paths.\n\n"
                    "Observation: The test bodies are not fully shown in the diff, so verification is limited.\n\n"
                    "Touched paths reviewed: `bridge/github_to_mep.py`, `tests/test_github_bridge.py`\n\n"
                    "Tests reviewed: `tests/test_github_bridge.py`\n\n"
                    "Risk areas checked: retry queuing, stale metadata refresh\n\n"
                    "Checks performed: reviewed `_suppression_reason_allows_retry`, checked the new retry queue coverage for pending approvals\n\n"
                    "Changed identifiers verified: `_suppression_reason_allows_retry`, `_issue_retry_task`"
                ),
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(status_response.status_code, 200, status_response.text)
        self.assertEqual(len(self.github_session.posts), 1)
        review_payload = self.github_session.posts[0]["json"]
        self.assertEqual(review_payload["event"], "COMMENT")
        self.assertIn("## Review Summary", review_payload["body"])
        self.assertNotIn("not fully shown in the diff", review_payload["body"])
        self.assertNotIn("verification is limited", review_payload["body"])
        self.assertEqual(self.service.github_writeback_metrics["last_suppressed_reason"], None)

    def test_status_callback_suppresses_finding_conflicting_with_patch_evidence(self):
        self._set_pr_review_package(
            [
                {
                    "filename": "hub/main.py",
                    "status": "modified",
                    "additions": 9,
                    "deletions": 0,
                    "changes": 9,
                    "patch": (
                        "@@ -2616,0 +2616,9 @@\n"
                        '+@app.get("/tasks/pending/{node_id}")\n'
                        "+async def get_pending_tasks(node_id: str, authenticated_node: str = Depends(verify_request)):\n"
                        "+    if authenticated_node != node_id:\n"
                        '+        raise HTTPException(status_code=403, detail="Cannot view pending tasks for another node")\n'
                    ),
                },
                {
                    "filename": "tests/test_hub_api.py",
                    "status": "modified",
                    "additions": 18,
                    "deletions": 0,
                    "changes": 18,
                    "patch": (
                        "@@ -779,0 +779,18 @@\n"
                        '+pending = client.get(f"/tasks/pending/{target_id}", headers=_auth_headers(target_priv, target_id, ""))\n'
                        "+self.assertEqual(pending.status_code, 200, pending.text)\n"
                    ),
                },
            ],
            pr_body="Adds a pending task endpoint with authentication checks and tests.",
        )
        response = self._post_webhook(
            _issue_comment_payload("@Hub-Sentinel review this PR", delivery_number=244),
            delivery_id="delivery-conflicting-finding",
        )
        self.assertEqual(response.status_code, 200)
        bridge_id = response.json()["bridge_id"]
        self._flush_context(response.json()["context_id"])

        token = self.service._generate_status_token(bridge_id, "node_target")
        status_response = self.client.post(
            "/bridge/status",
            json={
                "bridge_id": bridge_id,
                "status": "completed",
                "target_node_id": "node_target",
                "task_id": "task-conflicting-finding",
                "detail": (
                    "## Review Findings\n\n"
                    "The PR adds the pending tasks endpoint and supporting tests.\n\n"
                    "1. **New endpoint /tasks/pending/{node_id} has no authentication or authorization checks.** (`hub/main.py`): "
                    "The patch shows the endpoint is added without any dependency injection or middleware for verifying the caller's identity."
                ),
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(status_response.status_code, 200, status_response.text)
        self.assertEqual(len(self.github_session.posts), 0)
        self.assertEqual(self.service.github_writeback_metrics["last_suppressed_reason"], "ungrounded_finding")

    def test_status_callback_salvages_ungrounded_finding_into_publishable_summary(self):
        self._set_pr_review_package(
            [
                {
                    "filename": "hub/db.py",
                    "status": "modified",
                    "patch": (
                        "@@ -120,0 +120,8 @@\n"
                        "+def get_pending_tasks_for_provider(provider: str):\n"
                        "+    conn = _get_conn()\n"
                        "+    return _select_pending(conn, provider)\n"
                    ),
                },
                {
                    "filename": "tests/test_hub_api.py",
                    "status": "modified",
                    "patch": (
                        "@@ -779,0 +779,6 @@\n"
                        '+response = client.get("/tasks/pending/provider")\n'
                        "+self.assertEqual(response.status_code, 200)\n"
                    ),
                },
            ],
            pr_body="Adds provider pending-task retrieval and focused endpoint coverage.",
        )
        response = self._post_webhook(
            _issue_comment_payload("@Hub-Sentinel review this PR", delivery_number=246),
            delivery_id="delivery-salvage-ungrounded-finding",
        )
        self.assertEqual(response.status_code, 200)
        bridge_id = response.json()["bridge_id"]
        self._flush_context(response.json()["context_id"])

        token = self.service._generate_status_token(bridge_id, "node_target")
        status_response = self.client.post(
            "/bridge/status",
            json={
                "bridge_id": bridge_id,
                "status": "completed",
                "target_node_id": "node_target",
                "task_id": "task-salvage-ungrounded-finding",
                "detail": (
                    "## Review Findings\n\n"
                    "The PR adds provider pending-task retrieval and endpoint coverage.\n\n"
                    "Touched paths reviewed: `hub/db.py`, `tests/test_hub_api.py`\n\n"
                    "Risk areas checked: connection lifecycle, endpoint coverage\n\n"
                    "Checks performed: reviewed the changed diff for `hub/db.py`, checked the new `get_pending_tasks_for_provider` path and test coverage\n\n"
                    "Why no finding: The changed `get_pending_tasks_for_provider` flow and coverage look consistent aside from the unsupported claim below.\n\n"
                    "1. **Authentication is missing from the provider endpoint.** (`hub/db.py`): The patch adds a provider helper without any `verify_request` dependency."
                ),
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(status_response.status_code, 200, status_response.text)
        self.assertEqual(len(self.github_session.posts), 1)
        review_payload = self.github_session.posts[0]["json"]
        self.assertEqual(review_payload["event"], "COMMENT")
        self.assertIn("## Review Summary", review_payload["body"])
        self.assertNotIn("## Review Findings", review_payload["body"])
        self.assertNotIn("1. **Authentication is missing", review_payload["body"])
        self.assertEqual(self.service.github_writeback_metrics["last_suppressed_reason"], None)

    def test_status_callback_allows_finding_grounded_to_touched_path_and_patch(self):
        self._set_pr_review_package(
            [
                {
                    "filename": "node/mep_runtime.py",
                    "status": "modified",
                    "additions": 10,
                    "deletions": 1,
                    "changes": 11,
                    "patch": (
                        "@@ -226,7 +226,10 @@ async def run_forever(self) -> int:\n"
                        "+    from importlib.util import find_spec\n"
                        '+    if find_spec("websockets") is None:\n'
                        '+        raise ImportError("websockets not available")\n'
                        "+    from ws_connect import ws_connect\n"
                    ),
                },
                {
                    "filename": "node/ws_connect.py",
                    "status": "added",
                    "additions": 22,
                    "deletions": 0,
                    "changes": 22,
                    "patch": (
                        "@@ -0,0 +1,22 @@\n"
                        "+def ws_connect(uri: str, **kwargs):\n"
                        '+    import websockets\n'
                        "+    kwargs.setdefault(\"host\", parsed.hostname)\n"
                    ),
                },
            ],
            pr_body="Adds a shared websocket helper and switches the runtime to use it.",
        )
        response = self._post_webhook(
            _issue_comment_payload("@Hub-Sentinel review this PR", delivery_number=149),
            delivery_id="delivery-grounded-finding",
        )
        self.assertEqual(response.status_code, 200)
        bridge_id = response.json()["bridge_id"]
        self._flush_context(response.json()["context_id"])

        token = self.service._generate_status_token(bridge_id, "node_target")
        status_response = self.client.post(
            "/bridge/status",
            json={
                "bridge_id": bridge_id,
                "status": "completed",
                "target_node_id": "node_target",
                "task_id": "task-grounded-finding",
                "detail": (
                    "## Review Findings\n\n"
                    "The PR correctly narrows WebSocket connection handling to a shared helper.\n\n"
                    "1. **Import guard using `find_spec` may be redundant.** (`node/mep_runtime.py`): "
                    "The `find_spec` check duplicates the later `websockets` import in the helper and adds extra branching to `run_forever`.\n\n"
                    "Touched paths reviewed: `node/mep_runtime.py`, `node/ws_connect.py`"
                ),
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(status_response.status_code, 200, status_response.text)
        self.assertEqual(len(self.github_session.posts), 1)
        self.assertTrue(self.github_session.posts[0]["url"].endswith("/repos/WUAIBING/MEP/pulls/149/reviews"))
        review_payload = self.github_session.posts[0]["json"]
        self.assertEqual(review_payload["event"], "COMMENT")
        self.assertEqual(review_payload["comments"][0]["path"], "node/mep_runtime.py")
        self.assertEqual(review_payload["comments"][0]["line"], 226)
        self.assertEqual(review_payload["comments"][0]["side"], "RIGHT")
        self.assertIn("Import guard using `find_spec` may be redundant.", review_payload["comments"][0]["body"])

    def test_patch_added_line_number_requires_focus_term_match(self):
        patch = (
            "@@ -10,2 +10,3 @@\n"
            " context\n"
            "-old_call()\n"
            "+new_call()\n"
            "+other_line()\n"
        )

        self.assertEqual(
            self.service._patch_added_line_number(patch, "The `new_call` check is wrong"),
            11,
        )
        self.assertIsNone(
            self.service._patch_added_line_number(patch, "The `missing_identifier` check is wrong")
        )
        self.assertIsNone(
            self.service._patch_added_line_number(patch, "This finding has no backticked identifier")
        )

    def test_status_callback_skips_inline_comment_when_identifier_does_not_match_patch(self):
        self._set_pr_review_package(
            [
                {
                    "filename": "node/mep_runtime.py",
                    "status": "modified",
                    "additions": 4,
                    "deletions": 1,
                    "changes": 5,
                    "patch": (
                        "@@ -221,1 +221,4 @@\n"
                        "-    from ws_connect import ws_connect\n"
                        "+    from importlib.util import find_spec\n"
                        '+    if find_spec("websockets") is None:\n'
                        '+        raise ImportError("websockets not available")\n'
                        "+    from ws_connect import ws_connect\n"
                    ),
                },
            ],
            pr_body="Adds an import guard before wiring the shared websocket helper.",
        )
        response = self._post_webhook(
            _issue_comment_payload("@Hub-Sentinel review this PR", delivery_number=150),
            delivery_id="delivery-unmapped-inline-finding",
        )
        self.assertEqual(response.status_code, 200)
        bridge_id = response.json()["bridge_id"]
        self._flush_context(response.json()["context_id"])

        token = self.service._generate_status_token(bridge_id, "node_target")
        status_response = self.client.post(
            "/bridge/status",
            json={
                "bridge_id": bridge_id,
                "status": "completed",
                "target_node_id": "node_target",
                "task_id": "task-unmapped-inline-finding",
                "detail": (
                    "## Review Findings\n\n"
                    "1. **Missing identifier mapping is unsafe.** (`node/mep_runtime.py`): "
                    "The `missing_identifier` check is wrong and should not land on an unrelated added line.\n\n"
                    "Touched paths reviewed: `node/mep_runtime.py`"
                ),
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(status_response.status_code, 200, status_response.text)
        self.assertEqual(len(self.github_session.posts), 0)
        self.assertEqual(self.service.github_writeback_metrics["last_suppressed_reason"], "ungrounded_finding")

    def test_inline_review_comments_skips_ambiguous_path_reference(self):
        review_package = {
            "touched_paths": ["pkg_one/helpers.py", "pkg_two/helpers.py"],
            "changed_files": [
                {
                    "filename": "pkg_one/helpers.py",
                    "patch": "@@ -1,0 +1,2 @@\n+def missing_call():\n+    return 1\n",
                },
                {
                    "filename": "pkg_two/helpers.py",
                    "patch": "@@ -1,0 +1,2 @@\n+def missing_call():\n+    return 2\n",
                },
            ],
        }

        comments = self.service._inline_review_comments(
            (
                "## Review Findings\n\n"
                "1. **Helper path may be wrong.** (`helpers.py`): "
                "The `missing_call` symbol changes semantics across both helper modules."
            ),
            review_package,
        )

        self.assertEqual(comments, [])
        self.assertEqual(self.service.github_writeback_metrics["inline_comment_ambiguous_paths"], 1)

    def test_inline_review_comments_records_parse_miss_for_unparseable_findings_heading(self):
        comments = self.service._inline_review_comments(
            "## Review Findings\n\n- malformed finding that does not follow the numbered markdown contract",
            {},
        )

        self.assertEqual(comments, [])
        self.assertEqual(self.service.github_writeback_metrics["inline_comment_parse_misses"], 1)

    def test_status_callback_suppresses_summary_with_paths_but_no_code_evidence(self):
        self._set_pr_review_package(
            [
                {
                    "filename": "node/mep_runtime.py",
                    "status": "modified",
                    "additions": 40,
                    "deletions": 2,
                    "changes": 42,
                    "patch": (
                        "@@ -945,0 +945,29 @@\n"
                        "+def _record_pending_task_poll_failure(self, status: int, detail: str) -> None:\n"
                        "+    self.pending_task_recovery_metrics['last_poll_status'] = status\n"
                        "+    self.pending_task_recovery_metrics['last_poll_failure_detail'] = detail\n"
                    ),
                },
                {
                    "filename": "tests/test_node_runtime.py",
                    "status": "modified",
                    "additions": 20,
                    "deletions": 0,
                    "changes": 20,
                    "patch": (
                        "@@ -494,0 +494,20 @@\n"
                        "+def test_fetch_pending_tasks_uses_authenticated_get(self):\n"
                        "+    self.assertEqual(tasks, [{'id': 'task_pending'}])\n"
                    ),
                },
            ],
            pr_body="Adds pending-task recovery observability and focused runtime tests.",
        )
        response = self._post_webhook(
            _issue_comment_payload("@Hub-Sentinel review this PR", delivery_number=243),
            delivery_id="delivery-summary-with-paths-only",
        )
        self.assertEqual(response.status_code, 200)
        bridge_id = response.json()["bridge_id"]
        self._flush_context(response.json()["context_id"])

        token = self.service._generate_status_token(bridge_id, "node_target")
        status_response = self.client.post(
            "/bridge/status",
            json={
                "bridge_id": bridge_id,
                "status": "completed",
                "target_node_id": "node_target",
                "task_id": "task-summary-with-paths-only",
                "detail": (
                    "## Review Summary\n\n"
                    "The PR adds pending-task recovery observability with metrics and logging in mep_runtime.py, plus corresponding tests in test_node_runtime.py."
                ),
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(status_response.status_code, 200, status_response.text)
        self.assertEqual(len(self.github_session.posts), 0)
        self.assertEqual(self.service.github_writeback_metrics["last_suppressed_reason"], "summary_without_code_evidence")

    def test_status_callback_allows_summary_with_grounded_code_observation(self):
        self._set_pr_review_package(
            [
                {
                    "filename": "node/mep_runtime.py",
                    "status": "modified",
                    "additions": 40,
                    "deletions": 2,
                    "changes": 42,
                    "patch": (
                        "@@ -945,0 +945,29 @@\n"
                        "+def _record_pending_task_poll_failure(self, status: int, detail: str) -> None:\n"
                        "+    self.pending_task_recovery_metrics['last_poll_status'] = status\n"
                        "+    self.pending_task_recovery_metrics['last_poll_failure_detail'] = detail\n"
                    ),
                },
                {
                    "filename": "tests/test_node_runtime.py",
                    "status": "modified",
                    "additions": 20,
                    "deletions": 0,
                    "changes": 20,
                    "patch": (
                        "@@ -494,0 +494,20 @@\n"
                        "+def test_fetch_pending_tasks_uses_authenticated_get(self):\n"
                        "+    self.assertEqual(tasks, [{'id': 'task_pending'}])\n"
                    ),
                },
            ],
            pr_body="Adds pending-task recovery observability and focused runtime tests.",
        )
        response = self._post_webhook(
            _issue_comment_payload("@Hub-Sentinel review this PR", delivery_number=243),
            delivery_id="delivery-summary-with-observation",
        )
        self.assertEqual(response.status_code, 200)
        bridge_id = response.json()["bridge_id"]
        self._flush_context(response.json()["context_id"])

        token = self.service._generate_status_token(bridge_id, "node_target")
        status_response = self.client.post(
            "/bridge/status",
            json={
                "bridge_id": bridge_id,
                "status": "completed",
                "target_node_id": "node_target",
                "task_id": "task-summary-with-observation",
                "detail": (
                    "## Review Summary\n\n"
                    "The PR adds pending-task recovery observability in `node/mep_runtime.py` and corresponding tests in `tests/test_node_runtime.py`.\n\n"
                    "Observation: `_record_pending_task_poll_failure` now records `last_poll_status`, so malformed payloads will still surface `status=200` in the metrics.\n\n"
                    "Risk areas checked: metrics correctness, test coverage\n\n"
                    "Checks performed: verified `_record_pending_task_poll_failure` writes `last_poll_status`, checked `test_fetch_pending_tasks_uses_authenticated_get` covers the recovery path\n\n"
                    "Why no finding: The new metrics write is narrowly scoped and the changed test covers the intended recovery behavior."
                ),
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(status_response.status_code, 200, status_response.text)
        self.assertEqual(len(self.github_session.posts), 1)

    def test_status_callback_suppresses_summary_without_risk_coverage(self):
        self._set_pr_review_package(
            [
                {
                    "filename": "node/mep_runtime.py",
                    "status": "modified",
                    "additions": 40,
                    "deletions": 2,
                    "changes": 42,
                    "patch": (
                        "@@ -945,0 +945,29 @@\n"
                        "+def _record_pending_task_poll_failure(self, status: int, detail: str) -> None:\n"
                        "+    self.pending_task_recovery_metrics['last_poll_status'] = status\n"
                        "+    self.pending_task_recovery_metrics['last_poll_failure_detail'] = detail\n"
                    ),
                },
                {
                    "filename": "tests/test_node_runtime.py",
                    "status": "modified",
                    "additions": 20,
                    "deletions": 0,
                    "changes": 20,
                    "patch": (
                        "@@ -494,0 +494,20 @@\n"
                        "+def test_fetch_pending_tasks_uses_authenticated_get(self):\n"
                        "+    self.assertEqual(tasks, [{'id': 'task_pending'}])\n"
                    ),
                },
            ],
            pr_body="Adds pending-task recovery observability and focused runtime tests.",
        )
        response = self._post_webhook(
            _issue_comment_payload("@Hub-Sentinel review this PR", delivery_number=244),
            delivery_id="delivery-summary-without-risk-coverage",
        )
        self.assertEqual(response.status_code, 200)
        bridge_id = response.json()["bridge_id"]
        self._flush_context(response.json()["context_id"])

        token = self.service._generate_status_token(bridge_id, "node_target")
        status_response = self.client.post(
            "/bridge/status",
            json={
                "bridge_id": bridge_id,
                "status": "completed",
                "target_node_id": "node_target",
                "task_id": "task-summary-without-risk-coverage",
                "detail": (
                    "## Review Summary\n\n"
                    "The PR adds pending-task recovery observability in `node/mep_runtime.py` and corresponding tests in `tests/test_node_runtime.py`.\n\n"
                    "Observation: `_record_pending_task_poll_failure` now records `last_poll_status`, so malformed payloads will still surface `status=200` in the metrics."
                ),
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(status_response.status_code, 200, status_response.text)
        self.assertEqual(len(self.github_session.posts), 0)
        self.assertIn("action: retrying", self.notifier.calls[-1]["text"])
        self.assertEqual(self.service.github_writeback_metrics["last_suppressed_reason"], "summary_without_risk_coverage")

    def test_status_callback_suppresses_finding_grounded_only_to_context_lines(self):
        self._set_pr_review_package(
            [
                {
                    "filename": "hub/main.py",
                    "status": "modified",
                    "patch": (
                        " def existing_function():\n"
                        "     pass\n"
                        "+def new_function():\n"
                        "+    pass\n"
                    ),
                },
            ],
        )
        response = self._post_webhook(
            _issue_comment_payload("@Hub-Sentinel review this PR"),
            delivery_id="delivery-context-only-finding",
        )
        bridge_id = response.json()["bridge_id"]
        self._flush_context(response.json()["context_id"])

        token = self.service._generate_status_token(bridge_id, "node_target")
        status_response = self.client.post(
            "/bridge/status",
            json={
                "bridge_id": bridge_id,
                "status": "completed",
                "target_node_id": "node_target",
                "detail": (
                    "## Review Findings\n\n"
                    "1. **Hallucinated issue in `existing_function`.** (`hub/main.py`): "
                    "The `existing_function` is incorrectly implemented."
                ),
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(status_response.status_code, 200)
        self.assertEqual(len(self.github_session.posts), 0)
        self.assertEqual(self.service.github_writeback_metrics["last_suppressed_reason"], "finding_in_context_only")

    def test_status_callback_suppresses_observation_grounded_only_to_context_lines(self):
        self._set_pr_review_package(
            [
                {
                    "filename": "hub/main.py",
                    "status": "modified",
                    "patch": (
                        " def important_variable = 42\n"
                        "+def unrelated_change():\n"
                        "+    pass\n"
                    ),
                },
            ],
        )
        response = self._post_webhook(
            _issue_comment_payload("@Hub-Sentinel review this PR"),
            delivery_id="delivery-context-only-observation",
        )
        bridge_id = response.json()["bridge_id"]
        self._flush_context(response.json()["context_id"])

        token = self.service._generate_status_token(bridge_id, "node_target")
        status_response = self.client.post(
            "/bridge/status",
            json={
                "bridge_id": bridge_id,
                "status": "completed",
                "target_node_id": "node_target",
                "detail": (
                    "## Review Summary\n\n"
                    "Observation: This PR touches code near `important_variable`."
                ),
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(status_response.status_code, 200)
        self.assertEqual(len(self.github_session.posts), 0)
        self.assertEqual(self.service.github_writeback_metrics["last_suppressed_reason"], "observation_in_context_only")

    def test_status_callback_suppresses_summary_conflicting_with_changed_validation_logic(self):
        self._set_pr_review_package(
            [
                {
                    "filename": "clients/shared/mep_client.py",
                    "status": "modified",
                    "patch": (
                        "@@ -1177,0 +1177,12 @@\n"
                        "+def build_governance_metadata(classification: str, approval_status: Optional[str] = None) -> dict[str, Any]:\n"
                        "+    normalized_classification = classification.strip().lower() if isinstance(classification, str) else \"\"\n"
                        "+    if normalized_classification not in GOVERNANCE_CLASSIFICATIONS:\n"
                        "+        raise ValueError(f\"unsupported governance classification: {classification}\")\n"
                        "+    if approval_status is not None:\n"
                        "+        normalized_status = approval_status.strip().lower() if isinstance(approval_status, str) else \"\"\n"
                        "+        if normalized_status not in GOVERNANCE_APPROVAL_STATUSES:\n"
                        "+            raise ValueError(f\"unsupported governance approval status: {approval_status}\")\n"
                    ),
                },
            ],
            pr_body="Adds governance metadata validation for classification and approval status.",
        )
        response = self._post_webhook(
            _issue_comment_payload("@Hub-Sentinel review this PR"),
            delivery_id="delivery-summary-conflicts-with-validation-guard",
        )
        bridge_id = response.json()["bridge_id"]
        self._flush_context(response.json()["context_id"])

        token = self.service._generate_status_token(bridge_id, "node_target")
        status_response = self.client.post(
            "/bridge/status",
            json={
                "bridge_id": bridge_id,
                "status": "completed",
                "target_node_id": "node_target",
                "task_id": "task-summary-conflicts-with-validation-guard",
                "detail": (
                    "## Review Summary\n\n"
                    "`build_governance_metadata` does not validate `classification` or `approval_status`, so unsupported values can still flow through.\n\n"
                    "Observation: The helper still passes raw values through to the governance payload.\n\n"
                    "Touched paths reviewed: `clients/shared/mep_client.py`\n\n"
                    "Risk areas checked: governance metadata validation\n\n"
                    "Checks performed: compared `build_governance_metadata` against the changed validation branch\n\n"
                    "Changed identifiers verified: `build_governance_metadata`, `classification`, `approval_status`"
                ),
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(status_response.status_code, 200)
        self.assertEqual(len(self.github_session.posts), 0)
        self.assertIn("action: retrying", self.notifier.calls[-1]["text"])
        self.assertEqual(self.service.github_writeback_metrics["last_suppressed_reason"], "summary_conflicts_with_patch")

    def test_status_callback_allows_finding_grounded_to_changed_lines(self):
        self._set_pr_review_package(
            [
                {
                    "filename": "hub/main.py",
                    "status": "modified",
                    "patch": (
                        " def existing_function():\n"
                        "     pass\n"
                        "+def new_function_with_bug():\n"
                        "+    x = 1 / 0\n"
                    ),
                },
            ],
        )
        response = self._post_webhook(
            _issue_comment_payload("@Hub-Sentinel review this PR"),
            delivery_id="delivery-changed-line-finding",
        )
        bridge_id = response.json()["bridge_id"]
        self._flush_context(response.json()["context_id"])

        token = self.service._generate_status_token(bridge_id, "node_target")
        status_response = self.client.post(
            "/bridge/status",
            json={
                "bridge_id": bridge_id,
                "status": "completed",
                "target_node_id": "node_target",
                "detail": (
                    "## Review Findings\n\n"
                    "1. **Real bug in `new_function_with_bug`.** (`hub/main.py`): "
                    "Division by zero in the new function."
                ),
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(status_response.status_code, 200)
        self.assertEqual(len(self.github_session.posts), 1)
        self.assertIn("Real bug", self.github_session.posts[0]["json"]["body"])

    def test_status_callback_suppresses_speculative_finding(self):
        self._set_pr_review_package(
            [
                {
                    "filename": "node/mep_runtime.py",
                    "status": "modified",
                    "additions": 37,
                    "deletions": 4,
                    "changes": 41,
                    "patch": (
                        "@@ -14,12 +14,51 @@\n"
                        "+REQUIRED_PACKAGES = ['requests', 'cryptography', 'websockets']\n"
                        "+def _check_dependencies() -> None:\n"
                        "+    for pkg in REQUIRED_PACKAGES:\n"
                        "+        __import__(pkg)\n"
                    ),
                },
                {
                    "filename": "README.md",
                    "status": "modified",
                    "additions": 5,
                    "deletions": 4,
                    "changes": 9,
                    "patch": (
                        "@@ -117,10 +117,11 @@\n"
                        "+pip install requests websockets cryptography\n"
                        "+cd node && python mep_runtime.py run --hub-url https://mep-hub.silentcopilot.ai\n"
                    ),
                },
            ],
            pr_body="Improves onboarding and adds startup dependency validation.",
        )
        response = self._post_webhook(
            _issue_comment_payload("@Hub-Sentinel review this PR", delivery_number=123),
            delivery_id="delivery-speculative-finding",
        )
        self.assertEqual(response.status_code, 200)
        bridge_id = response.json()["bridge_id"]
        self._flush_context(response.json()["context_id"])

        token = self.service._generate_status_token(bridge_id, "node_target")
        status_response = self.client.post(
            "/bridge/status",
            json={
                "bridge_id": bridge_id,
                "status": "completed",
                "target_node_id": "node_target",
                "task_id": "task-speculative-finding",
                "detail": (
                    "## Review Findings\n\n"
                    "Reviewed PR #123 onboarding dependency validation.\n\n"
                    "1. **The dependency validation only checks `REQUIRED_PACKAGES`, but the rest of the validation flow may be incomplete.** (`node/mep_runtime.py`): "
                    "If intended for broader startup validation, this suggests incomplete implementation and potentially leaving other dependencies unvalidated."
                ),
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(status_response.status_code, 200, status_response.text)
        self.assertEqual(len(self.github_session.posts), 0)
        self.assertEqual(self.service.github_writeback_metrics["last_suppressed_reason"], "speculative_finding")

    def test_status_callback_suppresses_hashability_claim_from_allowlist_membership(self):
        self._set_pr_review_package(
            [
                {
                    "filename": "hub/main.py",
                    "status": "modified",
                    "patch": (
                        "@@ -1048,0 +1048,6 @@\n"
                        '+classification = governance.get("classification")\n'
                        "+if classification not in INTERBOT_GOVERNANCE_CLASSIFICATIONS:\n"
                        '+    raise HTTPException(status_code=400, detail="Inter-bot governance classification invalid")\n'
                    ),
                },
            ],
            pr_body="Adds governance validation for inter-bot payload classification.",
        )
        response = self._post_webhook(
            _issue_comment_payload("@Hub-Sentinel review this PR", delivery_number=124),
            delivery_id="delivery-hashability-finding",
        )
        self.assertEqual(response.status_code, 200)
        bridge_id = response.json()["bridge_id"]
        self._flush_context(response.json()["context_id"])

        token = self.service._generate_status_token(bridge_id, "node_target")
        status_response = self.client.post(
            "/bridge/status",
            json={
                "bridge_id": bridge_id,
                "status": "completed",
                "target_node_id": "node_target",
                "task_id": "task-hashability-finding",
                "detail": (
                    "## Review Findings\n\n"
                    "1. **`classification` can raise a `TypeError` during allowlist membership checks.** (`hub/main.py`): "
                    "If `governance.get(\"classification\")` returns `None`, the `classification not in "
                    "INTERBOT_GOVERNANCE_CLASSIFICATIONS` guard will treat it as unhashable and fail before the intended HTTPException."
                ),
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(status_response.status_code, 200, status_response.text)
        self.assertEqual(len(self.github_session.posts), 0)
        self.assertEqual(self.service.github_writeback_metrics["last_suppressed_reason"], "ungrounded_finding")

    def test_status_callback_posts_issue_comment_for_analysis_completion(self):
        response = self._post_webhook(
            _issue_comment_payload("@Hub-Sentinel analyze this PR", delivery_number=227),
            delivery_id="delivery-analysis",
        )
        self.assertEqual(response.status_code, 200)
        bridge_id = response.json()["bridge_id"]
        self._flush_context(response.json()["context_id"])

        token = self.service._generate_status_token(bridge_id, "node_target")
        status_response = self.client.post(
            "/bridge/status",
            json={
                "bridge_id": bridge_id,
                "status": "completed",
                "target_node_id": "node_target",
                "task_id": "task-2",
                "detail": "Analysis complete.",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(status_response.status_code, 200, status_response.text)
        self.assertEqual(len(self.github_session.posts), 1)
        self.assertTrue(self.github_session.posts[0]["url"].endswith("/repos/WUAIBING/MEP/issues/227/comments"))
        self.assertEqual(self.github_session.posts[0]["json"]["body"].splitlines()[0], "Analysis complete.")

    def test_status_callback_uses_actual_target_metadata_for_non_default_alias(self):
        self._configure_multi_target_aliases()
        response = self._post_webhook(
            _issue_comment_payload("@Elsaws Bot analyze this PR", delivery_number=228),
            delivery_id="delivery-elsaws-status",
        )
        self.assertEqual(response.status_code, 200, response.text)

        self._flush_context(response.json()["context_id"])

        self.assertEqual(len(self.submission.calls), 1)
        envelope = self.submission.calls[0]["envelope"]
        bridge_id = envelope["task"]["inputs"]["bridge_metadata"]["bridge_id"]
        token = self.service._generate_status_token(bridge_id, "node_elsaws")
        status_response = self.client.post(
            "/bridge/status",
            json={
                "bridge_id": bridge_id,
                "status": "completed",
                "target_node_id": "node_elsaws",
                "task_id": "task-elsaws",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(status_response.status_code, 200, status_response.text)
        self.assertIn("target: Elsaws Bot (node_elsaws)", self.notifier.calls[-1]["text"])
        self.assertEqual(len(self.github_session.posts), 1)
        self.assertTrue(self.github_session.posts[0]["url"].endswith("/repos/WUAIBING/MEP/issues/228/comments"))
        self.assertIn("Elsaws Bot completed the requested action.", self.github_session.posts[0]["json"]["body"])

    def test_status_callback_blocks_writeback_when_alias_is_not_allowlisted(self):
        self._configure_multi_target_aliases()
        self.config.github_writeback_aliases = {"Hub Sentinel"}
        self.config.github_writeback_login = "wuyanbingep-a11y"
        response = self._post_webhook(
            _issue_comment_payload("@Elsaws Bot analyze this PR", delivery_number=229),
            delivery_id="delivery-blocked-elsaws",
        )
        self.assertEqual(response.status_code, 200, response.text)

        self._flush_context(response.json()["context_id"])

        self.assertEqual(len(self.submission.calls), 1)
        envelope = self.submission.calls[0]["envelope"]
        bridge_id = envelope["task"]["inputs"]["bridge_metadata"]["bridge_id"]
        token = self.service._generate_status_token(bridge_id, "node_elsaws")
        status_response = self.client.post(
            "/bridge/status",
            json={
                "bridge_id": bridge_id,
                "status": "completed",
                "target_node_id": "node_elsaws",
                "task_id": "task-blocked",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(status_response.status_code, 409, status_response.text)
        self.assertEqual(
            status_response.json()["detail"],
            "GitHub writeback identity wuyanbingep-a11y is not allowed for target alias 'Elsaws Bot'. "
            "Allowed aliases: Hub Sentinel",
        )
        self.assertEqual(len(self.github_session.posts), 0)

    def test_status_callback_uses_alias_specific_github_token_for_non_default_alias(self):
        self._configure_multi_target_aliases()
        self.config.github_writeback_aliases = {"Hub Sentinel"}
        self.config.github_writeback_login = "bridge-writer"
        self.config.github_tokens_by_alias = {"Elsaws Bot": "elsaws-token"}
        self.config.github_logins_by_alias = {"Elsaws Bot": "wuyanbingep-a11y"}
        response = self._post_webhook(
            _issue_comment_payload("@Elsaws Bot analyze this PR", delivery_number=230),
            delivery_id="delivery-elsaws-token",
        )
        self.assertEqual(response.status_code, 200, response.text)

        self._flush_context(response.json()["context_id"])

        self.assertEqual(len(self.submission.calls), 1)
        envelope = self.submission.calls[0]["envelope"]
        bridge_id = envelope["task"]["inputs"]["bridge_metadata"]["bridge_id"]
        token = self.service._generate_status_token(bridge_id, "node_elsaws")
        status_response = self.client.post(
            "/bridge/status",
            json={
                "bridge_id": bridge_id,
                "status": "completed",
                "target_node_id": "node_elsaws",
                "task_id": "task-elsaws-token",
                "detail": "Elsaws analysis complete.",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(status_response.status_code, 200, status_response.text)
        self.assertEqual(len(self.github_session.posts), 1)
        self.assertEqual(
            self.github_session.posts[0]["headers"]["Authorization"],
            "Bearer elsaws-token",
        )
        self.assertTrue(self.github_session.posts[0]["url"].endswith("/repos/WUAIBING/MEP/issues/230/comments"))
        self.assertEqual(self.github_session.posts[0]["json"]["body"].splitlines()[0], "Elsaws analysis complete.")

    def test_non_maintainer_trigger_is_ignored_when_policy_enabled(self):
        payload = _issue_comment_payload("@Hub-Sentinel review this PR")
        payload["comment"]["author_association"] = "CONTRIBUTOR"
        payload["issue"]["author_association"] = "CONTRIBUTOR"
        response = self._post_webhook(payload, delivery_id="delivery-policy")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ignored")
        time.sleep(0.05)
        self.assertEqual(len(self.submission.calls), 0)

    def test_bot_sender_is_ignored_by_default_to_prevent_ping_pong_loops(self):
        payload = _issue_comment_payload(
            "@Hub-Sentinel review this PR",
            sender_login="hub-sentinel[bot]",
            sender_type="Bot",
        )
        response = self._post_webhook(payload, delivery_id="delivery-bot")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ignored")
        time.sleep(0.05)
        self.assertEqual(len(self.submission.calls), 0)

    def test_trusted_bot_sender_can_trigger_when_explicitly_allowlisted(self):
        self.config.trusted_bot_logins = {"hub-sentinel[bot]"}
        payload = _issue_comment_payload(
            "@Hub-Sentinel review this PR",
            sender_login="hub-sentinel[bot]",
            sender_type="Bot",
        )
        response = self._post_webhook(payload, delivery_id="delivery-trusted-bot")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "buffered")
        self._flush_context(response.json()["context_id"])
        self.assertEqual(len(self.submission.calls), 1)

    def test_bridge_output_marker_is_ignored_even_if_trigger_text_is_present(self):
        payload = _issue_comment_payload(
            "<!-- mep-bridge:output bridge_id=br-123 -->\n@Hub-Sentinel review this PR",
            sender_login="hub-sentinel[bot]",
            sender_type="Bot",
        )
        response = self._post_webhook(payload, delivery_id="delivery-output-marker")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ignored")
        time.sleep(0.05)
        self.assertEqual(len(self.submission.calls), 0)

    def test_pending_registration_raises_clear_operator_error(self):
        client = DefaultMEPSubmissionClient(self.config)
        client.session = _FakeRequestsSession(
            [
                _FakeResponse(
                    {
                        "status": "pending",
                        "node_id": client.node_id,
                    }
                )
            ]
        )

        with self.assertRaises(BridgeRegistrationPendingApprovalError) as ctx:
            client.ensure_registered()

        self.assertIn(client.node_id, str(ctx.exception))
        self.assertIn("pending admin approval", str(ctx.exception))
        self.assertEqual(len(client.session.posts), 1)
        self.assertTrue(client.session.posts[0]["url"].endswith("/register"))


if __name__ == "__main__":
    unittest.main()
