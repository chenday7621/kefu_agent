"""Dataset CPU-origin/provenance contract uses the unchanged exact schema."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from copy import deepcopy
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import jsonschema
from ..dev_protocol import generate_tasks
from ..trajectory import DecisionFrame
from .data_app import ExpertTrajectory


class OriginTests(unittest.TestCase):
    def test_expert_fixture_origin_and_supervision_provenance_are_separate(self):
        task = deepcopy(generate_tasks()[0])
        task["split"] = "train"
        with TemporaryDirectory(dir="/nas/chenyi/posttraining-qwen/runtime") as d:
            logger = ExpertTrajectory(Path(d), {"database_url": "unused",
                "clock_anchor": "2026-10-11T00:00:00+00:00"}, task)
            pid = logger.begin(DecisionFrame(task["task_id"], "S", "T", 1),
                "fixture:list_my_orders", "list_my_orders", [], "fixture", {},
                "Qwen/Qwen3-8B", "locked", "synthetic_rule_expert")
            self.assertEqual(logger.steps[pid]["generation_origin"], "fixture")
            schema = json.loads(Path("experiments/posttraining/logger_schema.json").read_text())
            jsonschema.validate(logger.steps[pid], schema)
            with self.assertRaisesRegex(ValueError, "visible rule expert"):
                logger.begin(DecisionFrame(task["task_id"], "S", "T", 1),
                    "fixture:list_my_orders", "list_my_orders", [], "fixture", {},
                    "Qwen/Qwen3-8B", "locked", "local_qwen")


class AcquisitionBudgetTests(unittest.TestCase):
    def test_reserve_stop_before_fixture_creation(self):
        from .data_controller import collect, DataCollectionBudgetStop
        with TemporaryDirectory(dir="/nas/chenyi/posttraining-qwen/runtime") as d:
            config = {"collection_directory": d, "budget_directory": "unused",
                      "authorization": {}, "prices": {}}
            ledger = SimpleNamespace(summary=lambda: {"estimated_or_reserved_usd": 7.8,
                                                       "unknown_usage_requests": 0})
            with patch("experiments.posttraining.research.budget.BudgetLedger", return_value=ledger), \
                 patch("experiments.posttraining.research.data_controller.TrainingDatabase") as database:
                with self.assertRaises(DataCollectionBudgetStop):
                    collect(config, [{"episode_id": "unstarted-private-test"}])
                database.assert_not_called()

    def test_unknown_usage_is_a_protocol_pause_not_a_known_budget_prefix(self):
        from .data_controller import collect, DataCollectionBudgetStop
        with TemporaryDirectory(dir="/nas/chenyi/posttraining-qwen/runtime") as d:
            config = {"collection_directory": d, "budget_directory": "unused",
                      "authorization": {}, "prices": {}}
            ledger = SimpleNamespace(summary=lambda: {"estimated_or_reserved_usd": 1.0,
                                                       "unknown_usage_requests": 1})
            with patch("experiments.posttraining.research.budget.BudgetLedger", return_value=ledger), \
                 patch("experiments.posttraining.research.data_controller.TrainingDatabase") as database:
                try:
                    collect(config, [{"episode_id": "unstarted-private-test"}])
                except RuntimeError as error:
                    self.assertNotIsInstance(error, DataCollectionBudgetStop)
                    self.assertIn("UNKNOWN_USAGE", str(error))
                else:
                    self.fail("Unresolved provider usage must pause")
                database.assert_not_called()


if __name__ == "__main__":
    unittest.main()
