"""CPU-only native history contracts; never open PostgreSQL or load a model."""
from copy import deepcopy
from datetime import datetime, timezone
import unittest

from parlant.core.engines.alpha.prompt_builder import PromptBuilder
from parlant.core.sessions import SessionDocumentStore

from .dev_protocol import generate_tasks
from .episode_state import native_history_documents


def check_history_contract(task):
    fixture = task["initial_db_fixture"]
    before = deepcopy(fixture)
    documents = native_history_documents(fixture, datetime(2026, 10, 11, tzinfo=timezone.utc))
    events = [SessionDocumentStore._deserialize_event(None, doc) for doc in documents.values()]
    assert fixture == before
    assert list(documents) == [raw["id"] for raw in fixture["trusted_history"]]
    for offset, (raw, doc, event) in enumerate(zip(fixture["trusted_history"], documents.values(), events)):
        assert doc["id"] == raw["id"] and doc["session_id"] == raw["session_id"]
        assert doc["source"] == raw["source"] and doc["kind"] == raw["kind"]
        assert doc["offset"] == offset and not doc["deleted"]
        assert doc["metadata"]["fixture_origin"] == raw["origin"]
        assert {k: doc["data"][k] for k in raw["data"]} == raw["data"]
        assert set(doc["data"]) - set(raw["data"]) <= {"participant"}
        assert "id" not in doc["data"]["participant"]
        rendered_event = PromptBuilder.adapt_event(event)
        assert fixture["trusted_context"]["customer_id"] not in rendered_event
    # Exercise the real builder for empty history too, not just adapt_event.
    rendered = PromptBuilder().add_interaction_history(events).build()
    message_rendered = PromptBuilder().add_interaction_history_for_message_generation(events).build()
    assert fixture["trusted_context"]["customer_id"] not in rendered + message_rendered
    bindings = []
    for row in fixture["tables"]["session_operations"]:
        eid = row.get("confirmation_event_id")
        if eid:
            assert eid in documents
            assert documents[eid]["source"] == "customer"
            bindings.append({"operation_id": row["operation_id"], "confirmation_event_id": eid})
    assert fixture == before
    return {"task_id": task["task_id"], "history_events": len(events),
            "native_deserializer_passed": True, "prompt_builder_passed": True,
            "message_prompt_builder_passed": True, "fixture_unmodified": True,
            "confirmation_bindings": bindings, "hidden_identity_absent": True}


class NativeHistoryTests(unittest.TestCase):
    def test_all_frozen_history_shapes_build_native_prompts(self):
        results = []
        for task in generate_tasks():
            with self.subTest(task_id=task["task_id"]):
                results.append(check_history_contract(task))
        self.assertEqual(len(results), 30)
        self.assertEqual(sum(r["history_events"] for r in results), 5)

    def test_untrusted_role_identity_and_duplicates_fail_closed(self):
        task = next(t for t in generate_tasks() if t["task_id"] == "PTDEV-F3-02")
        for key, value in (("source", "ai_agent"), ("session_id", "another_session"),
                           ("origin", "invented_confirmation"), ("kind", "tool")):
            with self.subTest(key=key):
                fixture = deepcopy(task["initial_db_fixture"])
                fixture["trusted_history"][0][key] = value
                with self.assertRaises(ValueError):
                    native_history_documents(fixture, datetime.now(timezone.utc))
        fixture = deepcopy(task["initial_db_fixture"])
        fixture["trusted_history"].append(deepcopy(fixture["trusted_history"][0]))
        with self.assertRaises(ValueError):
            native_history_documents(fixture, datetime.now(timezone.utc))
        fixture = deepcopy(task["initial_db_fixture"])
        fixture["trusted_history"][0]["data"]["participant"] = {"display_name": "untrusted"}
        with self.assertRaises(ValueError):
            native_history_documents(fixture, datetime.now(timezone.utc))


if __name__ == "__main__":
    unittest.main()
