import unittest
from unittest.mock import patch

import main as hub_main


class TestHubGhostReconcile(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._original_connected = dict(hub_main.connected_nodes)
        hub_main.connected_nodes.clear()

    async def asyncTearDown(self):
        hub_main.connected_nodes.clear()
        hub_main.connected_nodes.update(self._original_connected)

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


if __name__ == "__main__":
    unittest.main()
