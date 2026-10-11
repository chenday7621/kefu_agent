"""CPU-only boundary contract regressions; no API/model/PG execution."""

import asyncio
from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from parlant.core.tools import ToolId
from ..offline_checks import output, tool, StubLogger
from ..trajectory import FRAME, CANDIDATE, DecisionFrame
from ..dev_protocol import generate_tasks, sha
from ..scorer import score_episode
from ..episode_evidence import audit_trajectory
from .boundary import OncePerCandidateProvider, OnceSingleToolBatch, LAST_REQUEST
from .evidence import ResearchTrajectory
from ..policy import TrustedValueLeak


class Tokenizer:
    def encode(self, text, **kwargs):
        return [1]

    def apply_chat_template(self, messages, **kwargs):
        return json.dumps(messages, ensure_ascii=False)


class Backend:
    origin = "fixture"

    def __init__(self, raw, pause=False):
        self.raw, self.calls, self.pause = raw, 0, pause
        self.started = asyncio.Event()

    async def complete(self, request):
        LAST_REQUEST.set(request)
        self.calls += 1
        self.started.set()
        if self.pause:
            await asyncio.Event().wait()
        await asyncio.sleep(0)
        return self.raw


class BoundaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(dir="/nas/chenyi/posttraining-qwen/runtime")
        self.directory = Path(self.tmp.name)
        self.task = generate_tasks()[0]
        self.log = ResearchTrajectory(self.directory,
             {"database_url": "unused", "clock_anchor": "2026-10-11T00:00:00+00:00",
              "customer_id": "actual-private-customer-value"}, self.task)
        self.frame = FRAME.set(DecisionFrame(self.task["task_id"], "fixture-session", "turn", 1))

    def tearDown(self):
        FRAME.reset(self.frame)
        self.tmp.cleanup()

    async def run_one(self, raw, candidate=None, pause=False):
        candidate = candidate or tool()
        tid = ToolId("fixture", candidate.name)
        token = CANDIDATE.set((tid, candidate))
        backend = Backend(raw, pause)
        provider = OncePerCandidateProvider(Tokenizer(), self.log, backend)
        batch = OnceSingleToolBatch(logger=StubLogger(), meter=None,
            optimization_policy=None, service_registry=None,
            consequential_schema_generator=provider, non_consequential_schema_generator=None,
            candidate_tool=(tid, candidate, []), context=SimpleNamespace(staged_events=[]),
            trajectory=self.log)
        try:
            _, evaluations = await batch._run_consequential_tool_inference("visible fixture", tid, 0)
            result = await batch._evaluate_consequential_tool_calls(evaluations, (tid, candidate, []))
            return self.log.steps[evaluations.policy_step_id], result, backend
        finally:
            CANDIDATE.reset(token)

    async def test_legal_zero_parameter_call(self):
        candidate = replace(tool("list_my_orders"), parameters={}, required=[])
        raw = json.loads(output(name=candidate.name))
        raw["tool_calls_for_candidate_tool"][0]["argument_evaluations"] = []
        row, result, backend = await self.run_one(json.dumps(raw), candidate)
        self.assertEqual(len(result[0]), 1)
        self.assertEqual(result[0][0].arguments, {})
        self.assertEqual(row["actual_gate_result"]["status"], "accepted")
        self.assertEqual(row["actual_executed_calls"], [])
        self.assertEqual(backend.calls, 1)

    async def test_no_call_is_not_manufactured(self):
        row, result, _ = await self.run_one(output(applicable=False))
        self.assertEqual(result[0], [])
        self.assertEqual(row["proposed_action"], "NO_CALL")
        self.assertEqual(row["actual_gate_result"]["status"], "no_call")

    async def test_unknown_and_trusted_names_native_rejected_without_dispatch(self):
        candidate = replace(tool("list_my_orders"), parameters={}, required=[])
        for name in ("ordinary_extra", "customer_id", "session_token"):
            with self.subTest(name=name):
                row, result, _ = await self.run_one(output(name=candidate.name,
                    parameter=name, value="invented-not-an-actual-private-value"), candidate)
                self.assertEqual(row["parse_status"], "passed")
                self.assertEqual(row["proposed_action"], "CALL")
                self.assertIn(name, row["proposed_calls"][0]["arguments"])
                self.assertEqual(result[0], [])
                self.assertEqual(row["actual_gate_result"]["status"], "rejected")
                reject = row["actual_gate_result"]["boundary_rejections"][0]
                self.assertEqual(reject["status"], "CANNOT_RUN")
                self.assertEqual(reject["validator"], "parlant.core.tools.validate_tool_arguments")
                self.assertEqual(row["actual_executed_calls"], [])
                self.assertEqual(row["tool_results"], [])
        events = self.log.journal.read()
        self.assertEqual(sum(e["kind"] == "policy_proposal" for e in events), 3)
        self.assertEqual(sum(e["kind"] == "gate_result" for e in events), 3)
        score = score_episode(self.task, {"task_id": self.task["task_id"],
            "scoring_version": self.task["scoring_version"], "initial_fixture_hash": sha(self.task["initial_db_fixture"]),
            "evidence_source": "isolated_controller", "events": events,
            "final_db": self.task["initial_db_fixture"]["tables"]})
        self.assertEqual(score["reward"], -1)
        self.assertGreater(score["metrics"]["wrong_arguments"], 0)

    async def test_actual_value_leak_is_safety_stop_and_redacted(self):
        with self.assertRaises(TrustedValueLeak):
            await self.run_one(output(value="actual-private-customer-value"))
        row = next(iter(self.log.steps.values()))
        self.assertEqual(row["failure_type"], "TRUSTED_VALUE_LEAK")
        self.assertIsNone(row["actual_gate_result"])
        self.assertNotIn("actual-private-customer-value", self.log.path.read_text())

    async def test_required_none_and_illegal_scalar_are_not_repaired(self):
        for value in (None, "illegal-scalar"):
            with self.subTest(value=value):
                candidate = replace(tool(), parameters={"operation_id":
                    ({"type": "string", "enum": ["legal"]}, next(iter(tool().parameters.values()))[1])})
                row, result, _ = await self.run_one(output(value=value), candidate)
                self.assertEqual(row["proposed_calls"][0]["arguments"]["operation_id"], value)
                if value is None:
                    # Original native gate passes required None unchanged;
                    # backend remains the original final argument guard.
                    self.assertEqual(result[0][0].arguments["operation_id"], None)
                else:
                    self.assertEqual(result[0], [])
                    self.assertEqual(row["actual_gate_result"]["invalid"], ["operation_id"])

    async def test_parse_failure_has_no_forged_proposal(self):
        row, result, _ = await self.run_one("{not-json")
        self.assertEqual(row["parse_status"], "failed")
        self.assertEqual(row["proposed_calls"], [])
        self.assertEqual(result[0], [])
        self.assertEqual(row["actual_gate_result"]["status"], "parse_failed_no_dispatch")

    async def test_concurrent_accept_reject_parse_and_cancel_do_not_cross(self):
        zero = replace(tool("list_my_orders"), parameters={}, required=[])
        accepted, rejected, failed = await asyncio.gather(
            self.run_one(output()),
            self.run_one(output(name=zero.name, parameter="extra"), zero),
            self.run_one("{invalid"))
        tid = ToolId("fixture", "cancelled_candidate")
        candidate = replace(tool(), name=tid.tool_name)
        token = CANDIDATE.set((tid, candidate))
        try:
            backend = Backend(output(), pause=True)
            provider = OncePerCandidateProvider(Tokenizer(), self.log, backend)
            pending = asyncio.create_task(provider.generate("visible fixture"))
            await backend.started.wait()
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await pending
        finally:
            CANDIDATE.reset(token)
        cancelled = next(r for r in self.log.steps.values() if r["candidate_tool_name"] == tid.tool_name)
        self.assertEqual(accepted[0]["actual_gate_result"]["status"], "accepted")
        self.assertEqual(rejected[0]["actual_gate_result"]["status"], "rejected")
        self.assertEqual(failed[0]["parse_status"], "failed")
        self.assertEqual(cancelled["terminal_status"], "candidate_cancelled_unexecuted")
        self.assertIsNone(cancelled["actual_gate_result"])
        self.assertTrue(all(not r["actual_executed_calls"] for r in self.log.steps.values()))
        self.assertEqual(len(self.log.steps), 4)
        self.assertTrue(audit_trajectory(self.directory, self.task["task_id"])["passed"])


if __name__ == "__main__":
    unittest.main()
