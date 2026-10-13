"""Independent negative tests for data admission and visible user selection."""

from copy import deepcopy
import unittest

from .data_audit import verify_visible_action
from .data_user import resolved_user_fields


class DataContractTests(unittest.TestCase):
    def view(self):
        return {"preceding_visible_facts": [{"event_id": "user-visible", "source_kind": "native_user_message",
                 "source_message": "订单：ORD-EXAMPLE；明细：ITEM-EXAMPLE；数量：1；原因：尺寸不合适。",
                 "data": {"order_id": "ORD-EXAMPLE", "item_id": "ITEM-EXAMPLE", "quantity": 1,
                          "reason": "尺寸不合适"}}]}

    def test_whole_episode_success_cannot_justify_wrong_step_no_call(self):
        policy = {"policy_step_id": "step", "candidate_tool_name": "get_order_details", "proposed_action": "NO_CALL"}
        with self.assertRaisesRegex(ValueError, "visible constraints"):
            verify_visible_action(policy, self.view(), [])
        policy["proposed_action"] = "CALL"
        self.assertTrue(verify_visible_action(policy, self.view(), []))

    def test_submit_without_actual_confirmation_is_rejected(self):
        view = self.view()
        view["preceding_visible_facts"].append({"source_kind": "native_tool_result", "tool_name": "check_return_eligibility",
            "data": {"operation_id": "visible-operation", "preview": deepcopy(view["preceding_visible_facts"][0]["data"])}})
        policy = {"policy_step_id": "step", "candidate_tool_name": "submit_confirmed_return", "proposed_action": "CALL"}
        with self.assertRaises(ValueError):
            verify_visible_action(policy, view, [{"kind": "policy_proposal", "policy_step_id": "step", "sequence": 3}])
        policy["proposed_action"] = "NO_CALL"
        self.assertTrue(verify_visible_action(policy, view, [{"kind": "policy_proposal", "policy_step_id": "step", "sequence": 3}]))

    def test_visible_confirmation_does_not_override_preview_only_permission(self):
        view = self.view()
        view["preceding_visible_facts"][0]["source_message"] += "只授权生成退货预览。"
        view["preceding_visible_facts"].append({"source_kind": "native_tool_result", "tool_name": "check_return_eligibility",
            "data": {"operation_id": "visible-operation", "preview": deepcopy(view["preceding_visible_facts"][0]["data"])}})
        policy = {"policy_step_id": "step", "candidate_tool_name": "submit_confirmed_return", "proposed_action": "CALL"}
        events = [{"kind": "confirmation_recorded", "operation_id": "visible-operation", "sequence": 2},
                  {"kind": "policy_proposal", "policy_step_id": "step", "sequence": 3}]
        with self.assertRaises(ValueError):
            verify_visible_action(policy, view, events)

    def test_user_selector_cannot_guess_without_actual_observation(self):
        known = {"order_selector": "latest_delivered", "product_name": "独立示例物品", "quantity_selector": "all_available"}
        self.assertNotIn("order_id", resolved_user_fields(known, []))
        event = {"kind": "tool_result", "result": {"ok": True, "data": {"orders": [
            {"id": "ORD-A", "status": "delivered", "delivered_at": "2026-01-01"},
            {"id": "ORD-B", "status": "delivered", "delivered_at": "2026-01-02"}]}}}
        resolved = resolved_user_fields(known, [event])
        self.assertEqual(resolved["order_id"], "ORD-B")
        self.assertNotIn("item_id", resolved)
        self.assertNotIn("quantity", resolved)
        self.assertNotIn("order_id", known)


if __name__ == "__main__":
    unittest.main()
