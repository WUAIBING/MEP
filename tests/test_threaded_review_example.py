import unittest

from scripts.threaded_review_example import build_stdio_soak_plan


class TestThreadedReviewExample(unittest.TestCase):
    def test_build_stdio_soak_plan_includes_human_handoff(self):
        plan = build_stdio_soak_plan(
            "review-soak-001",
            snapshot_limit=5,
            human_target_node="node_governor",
            human_target_alias="Governor",
        )

        labels = [label for label, _command in plan]
        commands = [command for _label, command in plan]

        self.assertEqual(labels[0], "Inspect cached thread state")
        self.assertIn("mepdmlist --context review-soak-001 --limit 5", commands[0])
        self.assertIn("mepdmsnapshot --context review-soak-001 --label start --limit 5", commands[1])
        self.assertTrue(any("--target-node node_governor" in command for command in commands))
        self.assertTrue(any("--target-alias Governor" in command for command in commands))
        self.assertEqual(labels[-1], "Write end evidence snapshot")
        self.assertIn("mepdmsnapshot --context review-soak-001 --label end --limit 5", commands[-1])

    def test_build_stdio_soak_plan_omits_human_handoff_without_target(self):
        plan = build_stdio_soak_plan("review-soak-001", snapshot_limit=3)

        labels = [label for label, _command in plan]
        commands = [command for _label, command in plan]

        self.assertNotIn("Escalate to the human governor", labels)
        self.assertTrue(all("--target-node" not in command for command in commands))
        self.assertTrue(any("--label mid --limit 3" in command for command in commands))

    def test_build_stdio_soak_plan_rejects_non_positive_snapshot_limit(self):
        with self.assertRaisesRegex(ValueError, "snapshot_limit must be a positive integer"):
            build_stdio_soak_plan("review-soak-001", snapshot_limit=0)
