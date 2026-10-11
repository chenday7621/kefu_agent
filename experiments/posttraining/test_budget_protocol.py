"""No-model checks of execution units and the single-field V2 overlay."""
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from .budget_protocol import BUDGET, create_v2, planned_counts, verify_execution_bundle
from .dev_protocol import generate_tasks, write_bundle


class BudgetTests(unittest.TestCase):
    def test_full_family_paths_select_minimum_uniform_budget(self):
        paths = [planned_counts(t) for t in generate_tasks()]
        self.assertEqual(max(p["candidate_decisions"] for p in paths), BUDGET)
        self.assertEqual([p["task_id"] for p in paths if p["candidate_decisions"] == BUDGET],
                         ["PTDEV-F2-07", "PTDEV-F2-10"])
        for p in paths:
            self.assertEqual(p["candidate_decisions"], 6*p["joint_decision_rounds"])
            self.assertEqual(p["candidate_decisions"], p["expected_mcp_calls"]+p["expected_no_call_decisions"])

    def test_v2_changes_only_budget_and_retains_v1(self):
        with TemporaryDirectory() as tmp:
            v1, v2 = Path(tmp)/"v1", Path(tmp)/"v2"
            write_bundle(v1)
            before = {p.name: p.read_bytes() for p in v1.iterdir()}
            create_v2(v1, v2)
            tasks = verify_execution_bundle(v2)
            original = generate_tasks()
            restored = deepcopy(tasks)
            for t in restored:
                t["episode_budget"]["max_policy_decisions"] = 24
            self.assertEqual(restored, original)
            self.assertEqual(before, {p.name: p.read_bytes() for p in v1.iterdir()})
            tasks[0]["user_goal"]["target"]["reason"] = "forbidden goal change"
            (v2/"DEV_TASKS.jsonl").write_text("".join(json.dumps(t)+"\n" for t in tasks))
            with self.assertRaises(ValueError):
                verify_execution_bundle(v2)

    def test_scored_task_failure_is_complete_but_protocol_stop_is_not(self):
        from .dev_controller import validate_completion
        for reward in (1.0, 0.4, 0.0, -1.0):
            score = {"reward": reward, "metrics": {"task_success": reward == 1.0}}
            validate_completion(score, False)
            if reward == 1.0:
                validate_completion(score, True)
            else:
                with self.assertRaises(RuntimeError):
                    validate_completion(score, True)


if __name__ == "__main__":
    unittest.main()
