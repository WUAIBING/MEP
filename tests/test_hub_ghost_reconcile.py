import unittest
from unittest.mock import patch

import main as hub_main


class TestHubGhostReconcile(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._original_connected = dict(hub_main.connected_nodes)
        self._original_metrics = dict(hub_main.registry_reconcile_metrics)
        hub_main.connected_nodes.clear()
        hub_main.registry_reconcile_metrics.update(
            {
                "runs": 0,
                "candidates_scanned": 0,
                "reconciled_total": 0,
                "skipped_recent_total": 0,
                "last_run_at": None,
                "last_run_candidates": 0,
                "last_run_reconciled": 0,
                "last_run_skipped_recent": 0,
                "last_reconciled_at": None,
            }
        )

    async def asyncTearDown(self):
        hub_main.connected_nodes.clear()
        hub_main.connected_nodes.update(self._original_connected)
        hub_main.registry_reconcile_metrics.clear()
        hub_main.registry_reconcile_metrics.update(self._original_metrics)

    async def test_reconcile_forces_offline_for_stale_live_entry(self):
        hub_main.connected_nodes["node_live"] = object()

        def _fake_search(*, availability, **kwargs):
            if availability == "online":
                return [
                    {"node_id": "node_live", "availability": "online"},
                    {"node_id": "node_ghost", "availability": "online"},
                ]
            return []

        def _fake_get_registry(node_id):
            if node_id == "node_ghost":
                return {"node_id": "node_ghost", "availability": "online"}
            return None

        with (
            patch.object(hub_main.db, "search_registry", side_effect=_fake_search),
            patch.object(hub_main.db, "get_registry", side_effect=_fake_get_registry),
            patch.object(hub_main.db, "update_registry_availability") as update_mock,
            patch.object(hub_main, "log_event"),
        ):
            await hub_main._reconcile_live_registry_presence()

        update_mock.assert_called_once()
        self.assertEqual(update_mock.call_args.args[0], "node_ghost")
        self.assertEqual(update_mock.call_args.args[1], "offline")
        self.assertEqual(hub_main.registry_reconcile_metrics["last_run_reconciled"], 1)
        self.assertEqual(hub_main.registry_reconcile_metrics["reconciled_total"], 1)

    async def test_reconcile_skips_non_live_or_already_offline(self):
        def _fake_search(*, availability, **kwargs):
            if availability == "busy":
                return [{"node_id": "node_not_live_anymore", "availability": "busy"}]
            return []

        with (
            patch.object(hub_main.db, "search_registry", side_effect=_fake_search),
            patch.object(
                hub_main.db,
                "get_registry",
                return_value={"node_id": "node_not_live_anymore", "availability": "offline"},
            ),
            patch.object(hub_main.db, "update_registry_availability") as update_mock,
            patch.object(hub_main, "log_event"),
        ):
            await hub_main._reconcile_live_registry_presence()

        update_mock.assert_not_called()
        self.assertEqual(hub_main.registry_reconcile_metrics["last_run_reconciled"], 0)

    async def test_reconcile_skips_recent_live_entries(self):
        now = 1000.0

        def _fake_search(*, availability, **kwargs):
            if availability == "online":
                return [{"node_id": "node_recent", "availability": "online"}]
            return []

        with (
            patch.object(hub_main.db, "search_registry", side_effect=_fake_search),
            patch.object(
                hub_main.db,
                "get_registry",
                return_value={"node_id": "node_recent", "availability": "online", "updated_at": now - 5},
            ),
            patch.object(hub_main.db, "update_registry_availability") as update_mock,
            patch.object(hub_main, "log_event"),
            patch.object(hub_main, "REGISTRY_RECONCILE_STALE_SECONDS", 30.0),
            patch("main.time.time", return_value=now),
        ):
            await hub_main._reconcile_live_registry_presence()

        update_mock.assert_not_called()
        self.assertEqual(hub_main.registry_reconcile_metrics["last_run_skipped_recent"], 1)
        self.assertEqual(hub_main.registry_reconcile_metrics["skipped_recent_total"], 1)

    async def test_health_exposes_reconcile_metrics(self):
        health = await hub_main.health_check()
        metrics = health["metrics"]["registry_reconcile"]
        self.assertIn("runs", metrics)
        self.assertIn("last_run_reconciled", metrics)
        self.assertIn("last_run_skipped_recent", metrics)


if __name__ == "__main__":
    unittest.main()
