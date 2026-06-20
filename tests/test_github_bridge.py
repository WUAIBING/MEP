import hashlib
import hmac
import json
import os
import sys
import tempfile
import time
import asyncio
import unittest

from fastapi.testclient import TestClient

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from bridge.github_to_mep import (  # noqa: E402
    BridgeConfig,
    BridgeRegistrationPendingApprovalError,
    BridgeStore,
    DefaultMEPSubmissionClient,
    GitHubToMEPBridgeService,
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
    def __init__(self, payload: dict, *, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http_{self.status_code}")

    def json(self):
        return self._payload


class _FakeRequestsSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.posts = []

    def post(self, url, **kwargs):
        self.posts.append({"url": url, **kwargs})
        if not self.responses:
            raise AssertionError("No fake responses remaining")
        return self.responses.pop(0)


def _build_config(tmp_dir: str) -> BridgeConfig:
    return BridgeConfig(
        hub_url="http://hub.example.test",
        key_path=os.path.join(tmp_dir, "bridge_identity.pem"),
        sqlite_path=os.path.join(tmp_dir, "bridge.sqlite3"),
        webhook_secret="github-secret",
        target_node_id="node_target",
        target_alias="Hub Sentinel",
        trigger_aliases=["Hub-Sentinel"],
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
        self.service = GitHubToMEPBridgeService(
            self.config,
            store=self.store,
            submission_client=self.submission,
            notifier=self.notifier,
        )
        self.client = TestClient(create_app(config=self.config, service=self.service))

    def tearDown(self):
        self.client.close()

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

    def test_status_callback_requires_valid_token_and_updates_existing_message(self):
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
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(status_response.status_code, 200, status_response.text)

        self.assertEqual(len(self.notifier.calls), 2)
        final_message = self.notifier.calls[-1]
        self.assertTrue(final_message["editing"])
        self.assertIn("status: completed", final_message["text"])
        self.assertIn("action: approved", final_message["text"])

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
