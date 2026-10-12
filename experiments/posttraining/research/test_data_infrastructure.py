"""No model/API: bounded initialization, real owned PG and exception priority."""

from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

import psycopg
from apps.retail_demo.db import connect

from .data_infrastructure import TrainingDatabase, DataStateUnknown, DataResources, record_errors
from .data_schedule import scheduled_tasks, VerifiedPrefixStop
from .resources import ResearchResources


class DataInfrastructureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = TemporaryDirectory(dir="/nas/chenyi/posttraining-qwen/runtime")
        cls.root = Path(cls.temp.name)
        (cls.root / "logs").mkdir()
        cls.run_id = "cpu-data-infra-" + uuid4().hex
        cls.resources = ResearchResources(cls.root, {"run_id": cls.run_id})
        cls.dsn = cls.resources.start_pg()
        tasks = Path("/nas/chenyi/posttraining-qwen/runs/20261011_145323/datasets/v2-scenario-definitions/SCENARIOS.jsonl")
        cls.task = json.loads(tasks.read_text().splitlines()[61])

    @classmethod
    def tearDownClass(cls):
        cls.resources.close()
        cls.temp.cleanup()

    def setUp(self):
        name = "ptdev_" + uuid4().hex[:16]
        self.db = TrainingDatabase(self.dsn, name, deepcopy(self.task), self.root / name,
                                  owned_marker=self.resources.marker, run_id=self.run_id)

    def tearDown(self):
        self.db.cleanup()

    def test_native_fixture_and_cleanup_idempotency(self):
        self.db.initialize_and_materialize()
        self.assertEqual(self.db.project()["orders"], self.task["initial_db_fixture"]["tables"]["orders"])
        self.db.cleanup()
        self.db.cleanup()
        self.assertTrue(json.loads((self.db.directory / "CLEANUP.json").read_text())["already_absent"])

    def test_metadata_timeout_after_commit_reconciles_without_reinserting_or_reanchoring(self):
        original = self.db.migrations
        seen = []
        def migrations():
            seen.append(self.db.anchor)
            if len(seen) == 1:
                raise psycopg.errors.ConnectionTimeout("Injected pre-native metadata timeout")
            return original()
        with patch.object(self.db, "migrations", side_effect=migrations), patch("time.sleep"):
            self.db.initialize_and_materialize()
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0], seen[1])
        self.assertEqual(len(self.db.project()["orders"]), len(self.task["initial_db_fixture"]["tables"]["orders"]))
        self.assertEqual(json.loads((self.db.directory / "DATA_INIT_ATTEMPT_0.json").read_text())["error"]["type"], "ConnectionTimeout")
        self.assertTrue(json.loads((self.db.directory / "MATERIALIZATION.json").read_text())["initialization_commit_reconciled_not_reinserted"])

    def test_connection_timeout_is_at_most_two_additional_attempts(self):
        with patch.object(self.db, "inspect", side_effect=psycopg.errors.ConnectionTimeout("Injected connection timeout")) as probe, \
             patch("time.sleep") as sleep:
            with self.assertRaises(psycopg.errors.ConnectionTimeout):
                self.db.initialize_and_materialize()
        self.assertEqual(probe.call_count, 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [1, 2])
        self.assertFalse(self.db.creation_started)

    def test_unknown_partial_fixture_not_replayed(self):
        self.db.initialize_and_materialize()
        with connect(self.db.url) as conn:
            conn.execute("DELETE FROM order_items WHERE id=%s", (self.task["initial_db_fixture"]["tables"]["order_items"][0]["id"],))
        with self.assertRaisesRegex(DataStateUnknown, "Partial or unexpected"):
            self.db.initialize_and_materialize()
        self.db.backup_failed_initialization()
        self.assertTrue((self.db.directory / "DATA_FAILED_INITIALIZATION.dump").exists())

    def test_changed_migration_record_blocks_before_fixture_or_retry(self):
        self.db.initialize_and_materialize()
        with connect(self.db.url) as conn:
            conn.execute("UPDATE schema_migrations SET sha256='invalid' WHERE version=(SELECT min(version) FROM schema_migrations)")
        with self.assertRaisesRegex(DataStateUnknown, "migration record"):
            self.db.initialize_and_materialize()

    def test_native_stage_marker_prevents_initialization_retry(self):
        (self.db.directory / "DATA_NATIVE_STAGE_STARTED.json").write_text("{}")
        with patch.object(self.db, "inspect") as inspect:
            with self.assertRaisesRegex(DataStateUnknown, "stage started"):
                self.db.initialize_and_materialize()
        inspect.assert_not_called()

    def test_only_exact_owned_residual_connection_is_terminated(self):
        self.db.initialize_and_materialize()
        residual = psycopg.connect(self.db.url, autocommit=True)
        try:
            residual.execute("SELECT 1")
            self.db.cleanup()
            with self.assertRaises(psycopg.OperationalError):
                residual.execute("SELECT 1")
        finally:
            residual.close()

    def test_foreign_connection_remains_usable_when_cleanup_refuses(self):
        self.db.initialize_and_materialize()
        foreign = psycopg.connect(self.db.url, application_name="foreign-test-client", autocommit=True)
        try:
            foreign.execute("SELECT 1")
            with self.assertRaisesRegex(DataStateUnknown, "Foreign client"):
                self.db.cleanup()
            self.assertEqual(foreign.execute("SELECT 1").fetchone()[0], 1)
        finally:
            foreign.close()

    def test_cluster_shutdown_refuses_foreign_client_without_signalling_it(self):
        self.db.initialize_and_materialize()
        foreign = psycopg.connect(self.db.url, application_name="foreign-test-client", autocommit=True)
        resources = DataResources(self.root, {"run_id": self.run_id})
        resources.admin_dsn, resources.pg_running = self.dsn, True
        try:
            foreign.execute("SELECT 1")
            with self.assertRaisesRegex(DataStateUnknown, "foreign clients"):
                resources.close()
            self.assertEqual(foreign.execute("SELECT 1").fetchone()[0], 1)
        finally:
            foreign.close()


class DataControlTests(unittest.TestCase):
    def test_cleanup_exception_does_not_replace_primary(self):
        with TemporaryDirectory(dir="/nas/chenyi/posttraining-qwen/runtime") as directory:
            first = psycopg.errors.ConnectionTimeout("original timeout")
            try:
                try:
                    raise first
                finally:
                    record_errors(directory, first, [RuntimeError("cleanup failed")])
            except psycopg.errors.ConnectionTimeout as actual:
                self.assertIs(actual, first)
            row = json.loads((Path(directory) / "DATA_LIFECYCLE_ERRORS.json").read_text())
            self.assertEqual(row["primary"]["type"], "ConnectionTimeout")
            self.assertEqual(row["cleanup"][0]["type"], "RuntimeError")

    def test_cleanup_only_failure_still_raises(self):
        with TemporaryDirectory(dir="/nas/chenyi/posttraining-qwen/runtime") as directory:
            with self.assertRaisesRegex(RuntimeError, "cleanup"):
                record_errors(directory, None, [RuntimeError("cleanup failure")])

    def test_actual_collector_retains_primary_and_cleanup_exceptions(self):
        from types import SimpleNamespace
        from .data_controller import collect
        with TemporaryDirectory(dir="/nas/chenyi/posttraining-qwen/runtime") as directory:
            config = {"collection_directory": directory, "budget_directory": "unused",
                      "authorization": {}, "prices": {}, "admin_dsn": "unused",
                      "owned_cluster_marker": "unused", "run_id": "unit"}
            def database(*args, **kwargs):
                Path(args[3]).mkdir()
                return SimpleNamespace(initialize_and_materialize=lambda: None,
                    backup_failed_initialization=lambda: None,
                    cleanup=lambda: (_ for _ in ()).throw(RuntimeError("cleanup failure")))
            ledger = SimpleNamespace(summary=lambda: {"unknown_usage_requests": 0, "estimated_or_reserved_usd": 1.0})
            with patch("experiments.posttraining.research.budget.BudgetLedger", return_value=ledger), \
                 patch("experiments.posttraining.research.data_controller.TrainingDatabase", side_effect=database), \
                 patch("experiments.posttraining.research.data_controller.run_episode", side_effect=psycopg.errors.ConnectionTimeout("primary failure")):
                with self.assertRaisesRegex(psycopg.errors.ConnectionTimeout, "primary failure"):
                    collect(config, [{"episode_id": "owned-unit"}])
            record = json.loads((Path(directory) / "owned-unit/DATA_LIFECYCLE_ERRORS.json").read_text())
            self.assertEqual(record["primary"]["type"], "ConnectionTimeout")
            self.assertEqual(record["cleanup"][0]["type"], "RuntimeError")

    def test_schedule_must_preserve_same_tasks_and_internal_split_order(self):
        import hashlib
        with TemporaryDirectory(dir="/nas/chenyi/posttraining-qwen/runtime") as directory:
            path = Path(directory) / "ADDENDUM.json"
            tasks = [{"episode_id": i, "split": s} for i, s in (("T1", "train"), ("T2", "train"), ("V1", "sft_valid"), ("V2", "sft_valid"))]
            for order, passes in ((["T1", "V1", "V2", "T2"], True), (["T2", "V1", "V2", "T1"], False)):
                path.write_text(json.dumps({"version": "data.collection.addendum.v1", "user_authorized": True, "execution_order": order}))
                config = {"data_collection_addendum": str(path), "data_collection_addendum_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                if passes:
                    self.assertEqual([t["episode_id"] for t in scheduled_tasks(config, tasks)], order)
                else:
                    with self.assertRaisesRegex(ValueError, "within a split"):
                        scheduled_tasks(config, tasks)

    def test_prefix_does_not_start_assembly_before_both_episode_minimums(self):
        with TemporaryDirectory(dir="/nas/chenyi/posttraining-qwen/runtime") as directory:
            root = Path(directory)
            (root / "EPISODES.jsonl").write_text("")
            stop = VerifiedPrefixStop({"collection_directory": directory}, {}, root)
            with patch("experiments.posttraining.research.data_schedule.assemble") as assembly:
                self.assertFalse(stop())
            assembly.assert_not_called()


if __name__ == "__main__":
    unittest.main()
