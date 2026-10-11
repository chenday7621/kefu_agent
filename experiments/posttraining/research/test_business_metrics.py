"""Evidence accounting regressions; no model, API or database calls."""

import unittest

from .business_metrics import account, response_flags


class BusinessMetricsTests(unittest.TestCase):
    def test_guard_dispatch_is_not_a_wire_call(self):
        tables = {name: [] for name in ("return_requests", "return_outbox", "order_items", "orders", "return_operations")}
        task = {"task_id": "synthetic-accounting", "family": "synthetic",
                "initial_db_fixture": {"tables": tables}, "visible_goal_fields": {}}
        call = {"tool_call_id": "call-1", "tool_name": "business:submit_confirmed_return", "arguments": {"operation_id": "bad"}}
        step = {"actual_executed_calls": [call]}
        events = [{"kind": "policy_proposal", "policy_step_id": "policy-1",
                   "proposed_calls": [{"tool_name": call["tool_name"], "arguments": call["arguments"]}]},
                  {"kind": "tool_result", "policy_step_id": "policy-1", "tool_call_id": "call-1", "tool_result_id": "result-1",
                   "result": {"ok": False, "error": "Tool call error"},
                   "raw_payload": {"metadata": {"error_details": "SESSION_FORBIDDEN"}}}]
        result = account(task, {"events": events, "final_db": tables}, {"policy-1": step}, [], [],
                         {"critical_evidence": [{"policy_step_id": "policy-1"}]}, {"database_dropped": True})
        self.assertEqual(result["native_dispatch_calls"], 1)
        self.assertEqual(result["actual_mcp_requests"], 0)
        self.assertEqual(len(result["session_guard_rejections"]), 1)
        self.assertFalse(result["mcp_ids_used_for_causal_attribution"])
        self.assertEqual(result["new_formal_request_ids"], [])

    def test_merely_mentioning_order_is_not_a_provider_error_flag(self):
        def snapshot(message):
            return {"events": [{"id": "agent-event", "kind": "message", "source": "ai_agent", "data": {"message": message}}]}
        self.assertEqual(response_flags([snapshot("订单编号：ORD-1")], {"ORD-1"}, set()), [])
        flagged = snapshot("operation_id：`ORD-1`，请回复确认退货 ORD-1")
        self.assertEqual(len(response_flags([flagged, flagged], {"ORD-1"}, set())), 1)
        self.assertEqual(response_flags([flagged], {"ORD-1"}, {"ORD-1"}), [])

    def test_incorrect_preview_is_separate_from_formal_request(self):
        tables = {name: [] for name in ("return_requests", "return_outbox", "order_items", "orders", "return_operations")}
        intent = {"order_id": "order", "item_id": "item", "quantity": 2, "reason": "unused"}
        task = {"task_id": "synthetic-preview", "family": "synthetic",
                "initial_db_fixture": {"tables": tables}, "visible_goal_fields": intent}
        operation = dict(intent, id="operation", quantity=5)
        call = {"tool_call_id": "call", "tool_name": "business:check_return_eligibility",
                "arguments": {key: operation[key] for key in intent}}
        events = [{"kind": "policy_proposal", "policy_step_id": "policy", "proposed_calls": [call]},
                  {"kind": "tool_result", "policy_step_id": "policy", "tool_call_id": "call", "tool_result_id": "result",
                   "result": {"ok": True, "data": {"operation": operation}}}]
        final = dict(tables, return_operations=[operation])
        result = account(task, {"events": events, "final_db": final}, {"policy": {"actual_executed_calls": [call]}},
                         [{"tool_name": "check_return_eligibility"}], [],
                         {"critical_evidence": [{"policy_step_id": "policy"}]}, {"database_dropped": True})
        self.assertEqual(result["new_prepared_previews"], 1)
        self.assertEqual(len(result["incorrect_previews"]), 1)
        self.assertEqual(result["new_formal_request_ids"], [])
        self.assertEqual(result["reservation_quantity_changes"], {})

    def test_fixture_requests_and_outbox_are_not_new_model_effects(self):
        tables = {name: [] for name in ("return_requests", "return_outbox", "order_items", "orders", "return_operations")}
        tables.update(return_requests=[{"id": "fixture-request"}], return_outbox=[{"id": "fixture-outbox", "status": "sent"}])
        task = {"task_id": "synthetic-initial", "family": "synthetic",
                "initial_db_fixture": {"tables": tables}, "visible_goal_fields": {}}
        result = account(task, {"events": [], "final_db": tables}, {}, [], [],
                         {"critical_evidence": []}, {"database_dropped": True})
        self.assertEqual(result["new_formal_request_ids"], [])
        self.assertEqual(result["new_outbox_rows"], 0)


if __name__ == "__main__":
    unittest.main()
