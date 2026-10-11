"""Native PromptBuilder contracts and read-only replay, with zero model/API calls."""

import ast
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

from parlant.core.emissions import EmittedEvent
from parlant.core.engines.alpha.prompt_builder import BuiltInSection, PromptBuilder
from parlant.core.sessions import Event, EventKind, EventSource

from ..trajectory import DecisionFrame, redact
from .data_expert import (
    DataVisibleContextError, model_visible_frame, native_visible_calls, native_visible_facts,
)


def tool_event(call_id="call-1", result_id="result-1", quantity=1):
    payload = {"ok": True, "data": {"quantity": quantity}, "error": None}
    observation = {"tool_call_id": call_id, "tool_result_id": result_id,
                   "result": {"data": payload, "metadata": {"hidden_goal": "excluded"}}}
    emitted = EmittedEvent(EventSource.SYSTEM, EventKind.TOOL, "trace",
                           {"tool_calls": [{"tool_id": "fixture:get_order_details",
                            "arguments": {"order_id": "PUBLIC"}, "result": observation["result"]}]},
                           {"hidden_goal": "excluded"})
    return emitted, observation


def binding(observations, episode="independent"):
    return SimpleNamespace(
        call_parents={o["tool_call_id"]: "policy-" + o["tool_call_id"] for o in observations},
        steps={"policy-" + o["tool_call_id"]: {"episode_id": episode,
               "candidate_tool_name": "get_order_details"} for o in observations})


class NativeVisibleContextTests(unittest.TestCase):
    def test_real_str_enum_keys_history_and_staged_preserve_ids_and_body(self):
        first, a = tool_event()
        second, b = tool_event("call-2", "result-2", 2)
        history = Event("native-history", first.source, first.kind, datetime.now(timezone.utc),
                        0, first.trace_id, first.data, first.metadata, False)
        prompt = PromptBuilder().add_interaction_history([history]).add_staged_tool_events([second])
        self.assertIsInstance(BuiltInSection.INTERACTION_HISTORY, str)
        self.assertIn(BuiltInSection.INTERACTION_HISTORY, prompt.props)
        self.assertNotIn("__INTERACTION_HISTORY__", prompt.props)
        frame = DecisionFrame("independent", "S", "T", 2, observations=(a, b))
        facts = native_visible_facts(prompt, frame, binding((a, b)))
        self.assertEqual([f["event_id"] for f in facts], ["result-1", "result-2"])
        self.assertEqual([f["tool_call_id"] for f in facts], ["call-1", "call-2"])
        self.assertEqual([f["parent_policy_step_id"] for f in facts], ["policy-call-1", "policy-call-2"])
        self.assertEqual([f["data"] for f in facts], [{"quantity": 1}, {"quantity": 2}])
        self.assertNotIn("hidden_goal", json.dumps(facts))

    def test_same_result_in_history_and_staged_does_not_duplicate(self):
        emitted, observation = tool_event()
        prompt = PromptBuilder().add_staged_tool_events([emitted, emitted])
        frame = DecisionFrame("independent", "S", "T", 2, observations=(observation, observation))
        self.assertEqual(len(native_visible_facts(prompt, frame, binding((observation,)))), 1)

    def test_no_preceding_results_is_valid_empty_input(self):
        prompt = PromptBuilder().add_interaction_history([])
        frame = DecisionFrame("independent", "S", "T", 1)
        self.assertEqual(native_visible_facts(prompt, frame, binding(())), [])

    def test_missing_sections_with_results_is_a_contract_error(self):
        _, observation = tool_event()
        frame = DecisionFrame("independent", "S", "T", 2, observations=(observation,))
        with self.assertRaisesRegex(DataVisibleContextError, "sections missing"):
            native_visible_facts(PromptBuilder(), frame, binding((observation,)))

    def test_bad_json_and_wrong_section_type_never_silently_become_empty(self):
        for value in (["not-json"], "not-a-native-list", [None]):
            prompt = PromptBuilder().add_section(BuiltInSection.INTERACTION_HISTORY,
                "{interaction_events}", {"interaction_events": value})
            with self.assertRaises(DataVisibleContextError):
                native_visible_calls(prompt)

    def test_visible_result_without_exact_binding_or_with_different_body_fails(self):
        emitted, observation = tool_event()
        prompt = PromptBuilder().add_staged_tool_events([emitted])
        frame = DecisionFrame("independent", "S", "T", 2)
        with self.assertRaisesRegex(DataVisibleContextError, "attribution"):
            native_visible_facts(prompt, frame, binding(()))
        wrong = deepcopy(observation)
        wrong["result"]["data"]["data"]["quantity"] = 4
        frame = DecisionFrame("independent", "S", "T", 2, observations=(wrong,))
        with self.assertRaisesRegex(DataVisibleContextError, "attribution"):
            native_visible_facts(prompt, frame, binding((wrong,)))

    def test_cross_episode_results_are_rejected(self):
        emitted, observation = tool_event()
        frame = DecisionFrame("independent", "S", "T", 2, observations=(observation,))
        with self.assertRaisesRegex(DataVisibleContextError, "Cross-episode"):
            native_visible_facts(PromptBuilder().add_staged_tool_events([emitted]),
                                 frame, binding((observation,), "other"))

    def test_nonvisible_future_result_and_private_sections_are_not_expert_input(self):
        emitted, observation = tool_event()
        _, future = tool_event("future", "future-result", 99)
        prompt = PromptBuilder().add_staged_tool_events([emitted])
        prompt.add_section("PRIVATE_TEST_METADATA", "not model evidence",
                           {"future": [json.dumps({"data": {"tool_calls": [
                               {"tool_id": "fixture:get_order_details", "arguments": {},
                                "result": future["result"]["data"]}]}})]})
        frame = DecisionFrame("independent", "S", "T", 2, observations=(observation, future))
        facts = native_visible_facts(prompt, frame, binding((observation, future)))
        self.assertEqual([f["event_id"] for f in facts], ["result-1"])
        self.assertNotIn("99", json.dumps(facts))

    def test_trusted_fields_remain_redacted_and_input_is_unmodified(self):
        emitted, observation = tool_event()
        observation["result"]["data"]["data"]["session_token"] = "private-fixture-token"
        original = deepcopy(observation)
        facts = native_visible_facts(PromptBuilder().add_staged_tool_events([emitted]),
             DecisionFrame("independent", "S", "T", 2, observations=(observation,)),
             binding((observation,)))
        self.assertEqual(observation, original)
        self.assertEqual(facts[0]["data"], redact(original["result"]["data"]["data"]))
        self.assertNotIn("private-fixture-token", json.dumps(facts))

    def test_bound_identity_is_masked_like_actual_policy_prompt(self):
        emitted, observation = tool_event()
        observation["result"]["data"]["data"]["bound_customer_id"] = "private-identity-fixture"
        prompt = PromptBuilder().add_staged_tool_events([emitted])
        facts = native_visible_facts(prompt,
             DecisionFrame("independent", "S", "T", 2, observations=(observation,)),
             binding((observation,)))
        self.assertNotIn("private-identity-fixture", redact(prompt.build()))
        self.assertNotIn("private-identity-fixture", json.dumps(facts))
        self.assertEqual(facts[0]["data"]["bound_customer_id"], "[REDACTED]")

    def test_input_reference_copy_preserves_raw_result_and_exact_lineage(self):
        _, observation = tool_event()
        observation["result"]["data"]["data"]["bound_customer_id"] = "private-identity-fixture"
        original = deepcopy(observation)
        frame = DecisionFrame("independent", "S", "T", 2, observations=(observation,))
        visible = model_visible_frame(frame)
        self.assertEqual(frame.observations[0], original)
        self.assertEqual(visible.observations[0]["tool_call_id"], original["tool_call_id"])
        self.assertEqual(visible.observations[0]["tool_result_id"], original["tool_result_id"])
        self.assertEqual(visible.episode_id, frame.episode_id)
        self.assertEqual(visible.parent_ids, frame.parent_ids)
        self.assertEqual(visible.observations[0]["result"]["data"]["data"]["bound_customer_id"], "[REDACTED]")

    def test_readonly_first_data_attempt_recovers_all24_omitted_decisions(self):
        directory = Path("/nas/chenyi/posttraining-qwen/runs/20261011_164724_PT_B0_R1_CONTRACTFIX/datasets/verified-episodes/PTTRAIN-810001")
        if not directory.exists():
            self.skipTest("Historical NAS evidence is not bundled in Git")
        journal = next((directory / "trajectories").glob("*.jsonl"))
        original_hash = hashlib.sha256(journal.read_bytes()).hexdigest()
        steps = {r["policy_step_id"]: r for r in map(json.loads, journal.read_text().splitlines())}
        experts = {r["policy_step_id"]: r for r in map(json.loads, (directory / "EXPERT_RECORDS.jsonl").read_text().splitlines())}
        parents = {c["tool_call_id"]: pid for pid, row in steps.items() for c in row["actual_executed_calls"]}
        trajectory = SimpleNamespace(steps=steps, call_parents=parents)
        recovered = 0
        for pid, row in steps.items():
            if not row["relevant_tool_observations"]:
                continue
            # Decode the already recorded native history list for CPU fixture
            # reconstruction only. The production reader uses native props.
            template_prefix = PromptBuilder._INTERACTION_BODY.split("{interaction_events}", 1)[0]
            text = row["prompt_messages"][1]["content"]
            encoded = ast.literal_eval(text.split(template_prefix, 1)[1].split("###", 1)[0].strip())
            prompt = PromptBuilder().add_section(BuiltInSection.INTERACTION_HISTORY,
                     PromptBuilder._INTERACTION_BODY, {"interaction_events": encoded})
            frame = DecisionFrame(row["episode_id"], row["session_id"], row["turn_id"],
                                  row["engine_iteration"], observations=tuple(row["relevant_tool_observations"]))
            facts = native_visible_facts(prompt, frame, trajectory)
            self.assertTrue(facts)
            self.assertFalse(any(f["source_kind"] == "native_tool_result"
                                 for f in experts[pid]["preceding_visible_facts"]))
            for fact in facts:
                observed = next(o for o in row["relevant_tool_observations"]
                                if o["tool_result_id"] == fact["event_id"])
                self.assertEqual(fact["data"], observed["result"]["data"]["data"])
                self.assertEqual(fact["parent_policy_step_id"], parents[fact["tool_call_id"]])
            recovered += 1
        self.assertEqual(recovered, 24)
        self.assertEqual(original_hash, hashlib.sha256(journal.read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()
