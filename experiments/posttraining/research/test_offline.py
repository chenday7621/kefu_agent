"""Targeted new boundaries only: no model load/API/PG/Oracle episode."""

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from ..trajectory import TrajectoryLogger, FRAME, CANDIDATE, DecisionFrame
from ..offline_checks import tool, output, StubLogger
from parlant.core.tools import ToolId
from .budget import BudgetLedger, BudgetStop
from .formatting import CompletionCollator, completion_record
from .user_controller import ScriptedUser
from .boundary import OncePerCandidateProvider, OnceSingleToolBatch, LAST_REQUEST

AUTH = {"approved_total_usd": 10, "eval_reserve_usd": 2}
PRICES = {"hit": 0, "miss": 0, "output": 1e6}


class BudgetTests(unittest.TestCase):
    def test_reserve_retained_on_unknown_failure_and_eval_protected(self):
        with TemporaryDirectory() as d:
            ledger = BudgetLedger(d, AUTH, PRICES)
            request = {"max_tokens": 3, "messages": [], "model": "deepseek-chat"}
            first = ledger.reserve("b0", "draft", request)
            ledger.settle(first, status="failed")
            ledger.reserve("train", "draft", request)
            with self.assertRaises(BudgetStop):
                ledger.reserve("train", "draft", request)
            ledger.reserve("sft_eval", "draft", request)
            self.assertEqual(ledger.summary()["estimated_or_reserved_usd"], 9)
            self.assertEqual(ledger.summary()["unknown_usage_requests"], 3)

    def test_cross_thread_reservations_cannot_overspend(self):
        with TemporaryDirectory() as d:
            ledger = BudgetLedger(d, AUTH, PRICES)

            def reserve(_):
                try:
                    ledger.reserve("b0", "draft", {"max_tokens": 1, "messages": []})
                    return 1
                except BudgetStop:
                    return 0

            with ThreadPoolExecutor(max_workers=12) as pool:
                self.assertEqual(sum(pool.map(reserve, range(12))), 8)
            self.assertEqual(ledger.summary()["estimated_or_reserved_usd"], 8)

    def test_settlement_once_and_known_usage_releases_reserve(self):
        with TemporaryDirectory() as d:
            ledger = BudgetLedger(d, AUTH, PRICES)
            rid = ledger.reserve("b0", "draft", {"max_tokens": 3, "messages": []})
            ledger.settle(
                rid, status="completed", usage={"prompt_tokens": 1, "completion_tokens": 1}
            )
            self.assertEqual(ledger.summary()["estimated_or_reserved_usd"], 1)
            with self.assertRaises(ValueError):
                ledger.settle(rid, status="completed")

    def test_unknown_maximum_and_reduced_eval_reserve_denied(self):
        with TemporaryDirectory() as d:
            ledger = BudgetLedger(d, AUTH, PRICES)
            with self.assertRaises(BudgetStop):
                ledger.reserve("b0", "draft", {})
            with self.assertRaises(ValueError):
                BudgetLedger(d, dict(AUTH, eval_reserve_usd=1), PRICES)


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_exact_policy_class_routes_local(self):
        from unittest.mock import patch
        from .provider import ReferenceNLP
        from apps.retail_demo.nlp import DemoDeepSeek
        from parlant.core.engines.alpha.tool_calling.single_tool_batch import SingleToolBatchSchema
        from parlant.core.engines.alpha.canned_response_generator import CannedResponseDraftSchema

        with TemporaryDirectory() as d:
            obj = ReferenceNLP.__new__(ReferenceNLP)
            obj.policy = object()
            obj.config = {"phase": "b0", "budget_directory": d}
            obj.ledger = BudgetLedger(d, AUTH, PRICES)
            seen = []

            async def original(self, schema, hints):
                seen.append(schema)

                async def no_call(*args, **kwargs):
                    raise AssertionError("No API calls in routing test")

                return SimpleNamespace(
                    _client=SimpleNamespace(
                        max_retries=2,
                        chat=SimpleNamespace(completions=SimpleNamespace(create=no_call)),
                    )
                )

            with patch.object(DemoDeepSeek, "get_schematic_generator", original):
                self.assertIs(await obj.get_schematic_generator(SingleToolBatchSchema), obj.policy)
                response = await obj.get_schematic_generator(CannedResponseDraftSchema)
                self.assertIsNot(response, obj.policy)
                impostor = type("SingleToolBatchSchema", (), {})
                self.assertIsNot(await obj.get_schematic_generator(impostor), obj.policy)
                self.assertEqual(response._client.max_retries, 0)
                self.assertEqual(seen, [CannedResponseDraftSchema, impostor])
                self.assertEqual(obj.ledger.summary()["unique_requests"], 0)


class FormatTests(unittest.TestCase):
    def test_prompt_and_padding_only_are_masked_when_eos_is_pad(self):
        t = SimpleNamespace(encode=lambda *a, **k: [7, 8], convert_tokens_to_ids=lambda _: 99)
        one = completion_record(t, [1, 2], {}, 10)
        two = completion_record(t, [3], {}, 10)
        batch = CompletionCollator(99)([one, two])
        self.assertEqual(batch["labels"][0].tolist(), [-100, -100, 7, 8, 99])
        self.assertEqual(batch["labels"][1].tolist(), [-100, 7, 8, 99, -100])
        self.assertEqual(batch["attention_mask"][1].tolist(), [1, 1, 1, 1, 0])

    def test_no_target_or_history_truncation(self):
        t = SimpleNamespace(encode=lambda *a, **k: [7, 8], convert_tokens_to_ids=lambda _: 99)
        with self.assertRaises(ValueError):
            completion_record(t, [1, 2], {}, 4)


class UserTests(unittest.TestCase):
    def user(self):
        return ScriptedUser(
            "opening", {"order_id": "O", "item_id": "I", "quantity": 1, "reason": "R"}, [], "F1"
        )

    def test_wrong_latest_and_missing_phrase_are_not_confirmed(self):
        user = self.user()
        op = {"id": "op", "order_id": "O", "item_id": "I", "quantity": 1, "reason": "R"}
        d = {"snapshot": op, "visible_confirmation_phrase": "actual phrase"}
        self.assertIsNone(user.confirmation([d, dict(d, snapshot=dict(op, item_id="wrong"))]))
        self.assertIsNone(user.confirmation([{"snapshot": op}]))
        self.assertEqual(user.confirmation([d])["message"], "actual phrase")
        self.assertIsNone(user.confirmation([d]))

    def test_future_followup_is_not_visible_until_actual_trigger(self):
        user = ScriptedUser(
            "opening",
            {"order_id": "O"},
            [
                {
                    "trigger": "clarification_requested",
                    "field": "quantity",
                    "sets": {"quantity": 2},
                    "message": "two",
                }
            ],
            "F2",
        )
        self.assertIsNone(user.react([{"sequence": 1, "kind": "user_message"}]))
        self.assertNotIn("quantity", user.visible)
        self.assertEqual(
            user.react([{"sequence": 2, "kind": "clarification_requested", "field": "quantity"}])[
                "message"
            ],
            "two",
        )
        self.assertEqual(user.visible["quantity"], 2)


class GenerationTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        from transformers import AutoTokenizer

        cls.tokenizer = AutoTokenizer.from_pretrained(
            os.environ["POSTTRAIN_MODEL_PATH"], local_files_only=True
        )

    async def test_bad_parse_native_gate_has_no_call_and_no_regeneration(self):
        with TemporaryDirectory() as d:

            class Fake:
                origin = "fixture"
                calls = 0

                async def complete(self, request):
                    LAST_REQUEST.set(request)
                    self.calls += 1
                    return "{invalid"

            fake = Fake()
            logger = TrajectoryLogger(Path(d))
            provider = OncePerCandidateProvider(self.tokenizer, logger, fake)
            tid = ToolId("fixture", "submit_confirmed_return")
            candidate = tool()
            batch = OnceSingleToolBatch(
                logger=StubLogger(),
                meter=None,
                optimization_policy=None,
                service_registry=None,
                consequential_schema_generator=provider,
                non_consequential_schema_generator=None,
                candidate_tool=(tid, candidate, []),
                context=SimpleNamespace(staged_events=[]),
                trajectory=logger,
            )
            frame = FRAME.set(DecisionFrame("E", "S", "T", 1))
            token = CANDIDATE.set((tid, candidate))
            try:
                _, evaluations = await batch._run_consequential_tool_inference("prompt", tid, 0)
                result = await batch._evaluate_consequential_tool_calls(
                    evaluations, (tid, candidate, [])
                )
                self.assertEqual(result[0], [])
                with self.assertRaises(RuntimeError):
                    await provider.generate("prompt")
                self.assertEqual(fake.calls, 1)
                self.assertEqual(len(logger.steps), 1)
                row = next(iter(logger.steps.values()))
                self.assertEqual(row["parse_status"], "failed")
                self.assertIsNone(row["parsed_schema_output"])
                self.assertEqual(row["actual_executed_calls"], [])
            finally:
                FRAME.reset(frame)
                CANDIDATE.reset(token)

    async def test_parser_failure_is_scoreable_without_forged_proposal(self):
        from .evidence import ResearchTrajectory
        from ..dev_protocol import generate_tasks, sha
        from ..scorer import score_episode

        task = generate_tasks()[0]
        with TemporaryDirectory() as d:

            class Fake:
                origin = "fixture"

                async def complete(self, request):
                    LAST_REQUEST.set(request)
                    return "{bad"

            logger = ResearchTrajectory(
                Path(d),
                {
                    "database_url": "host=127.0.0.1 dbname=unused",
                    "clock_anchor": "2026-10-11T00:00:00+00:00",
                },
                task,
            )
            provider = OncePerCandidateProvider(self.tokenizer, logger, Fake())
            tid = ToolId("fixture", "submit_confirmed_return")
            candidate = tool()
            batch = OnceSingleToolBatch(
                logger=StubLogger(),
                meter=None,
                optimization_policy=None,
                service_registry=None,
                consequential_schema_generator=provider,
                non_consequential_schema_generator=None,
                candidate_tool=(tid, candidate, []),
                context=SimpleNamespace(staged_events=[]),
                trajectory=logger,
            )
            f = FRAME.set(DecisionFrame(task["task_id"], "S", "T", 1))
            c = CANDIDATE.set((tid, candidate))
            try:
                _, evaluations = await batch._run_consequential_tool_inference("prompt", tid, 0)
                await batch._evaluate_consequential_tool_calls(evaluations, (tid, candidate, []))
            finally:
                FRAME.reset(f)
                CANDIDATE.reset(c)
            events = logger.journal.read()
            self.assertEqual([e["kind"] for e in events], ["policy_failure"])
            score = score_episode(
                task,
                {
                    "task_id": task["task_id"],
                    "scoring_version": task["scoring_version"],
                    "initial_fixture_hash": sha(task["initial_db_fixture"]),
                    "evidence_source": "isolated_controller",
                    "events": events,
                    "final_db": task["initial_db_fixture"]["tables"],
                },
            )
            self.assertEqual(score["reward"], 0.0)
            self.assertEqual(
                next(iter(logger.steps.values()))["actual_gate_result"]["status"],
                "parse_failed_no_dispatch",
            )

    async def test_concurrent_candidates_keep_explicit_request_ids(self):
        with TemporaryDirectory() as d:

            class Fake:
                origin = "fixture"

                async def complete(self, request):
                    LAST_REQUEST.set(request)
                    await asyncio.sleep(0)
                    return output(name=request["tools"][0]["function"]["name"], applicable=False)

            logger = TrajectoryLogger(Path(d))
            provider = OncePerCandidateProvider(self.tokenizer, logger, Fake())

            async def generate(index):
                tid = ToolId("fixture", "candidate" + str(index))
                candidate = tool(name=tid.tool_name)
                f = FRAME.set(DecisionFrame("E", "S", "T", 1))
                c = CANDIDATE.set((tid, candidate))
                try:
                    return await provider.generate("same native snapshot")
                finally:
                    FRAME.reset(f)
                    CANDIDATE.reset(c)

            results = await asyncio.gather(*(generate(i) for i in range(6)))
            self.assertEqual(len({r.policy_step_id for r in results}), 6)
            self.assertEqual(
                {r.content.name for r in results}, {"candidate" + str(i) for i in range(6)}
            )
            self.assertEqual(
                {r["prompt_messages"][1]["content"] for r in logger.steps.values()},
                {"same native snapshot"},
            )


if __name__ == "__main__":
    unittest.main()
