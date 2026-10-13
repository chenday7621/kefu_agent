"""Public contract/causality checks for independent TRAIN expert only."""

from copy import deepcopy
import unittest

from .data_expert import choose
from .data_audit import verify_visible_action


class ExpertContracts(unittest.TestCase):
    def view(self):
        return {"user_messages": [{"event_id": "native-user", "message": "订单：ORD-INDEPENDENT；明细：ITEM-INDEPENDENT；数量：1；原因：边角有擦痕。"}],
                "agent_messages": [], "tool_facts": [
                    {"event_id": "native-read", "source_kind": "native_tool_result", "tool_name": "get_order_details",
                     "data": {"order": {"id": "ORD-INDEPENDENT", "status": "delivered"},
                              "items": [{"id": "ITEM-INDEPENDENT", "returnable": True, "available_return_quantity": 2}]}},
                    {"event_id": "native-preview", "source_kind": "native_tool_result", "tool_name": "check_return_eligibility",
                     "data": {"operation_id": "independent-operation", "order_id": "ORD-INDEPENDENT",
                              "item_id": "ITEM-INDEPENDENT", "quantity": 1, "reason": "边角有擦痕",
                              "confirmation_phrase": "确认退货 independent-operation 订单ORD-INDEPENDENT 明细ITEM-INDEPENDENT 数量1"}}]}

    def test_flat_public_preview_never_creates_duplicate(self):
        view = self.view()
        args, _, hard, _, _ = choose(view, "check_return_eligibility")
        self.assertIsNone(args)
        self.assertTrue(hard)
        args, _, hard, _, _ = choose(view, "submit_confirmed_return")
        self.assertIsNone(args)
        self.assertTrue(hard)

    def test_only_actual_shown_phrase_plus_customer_event_allows_submit(self):
        view = self.view()
        phrase = view["tool_facts"][-1]["data"]["confirmation_phrase"]
        view["agent_messages"].append({"event_id": "actual-agent", "message": phrase})
        self.assertIsNone(choose(view, "submit_confirmed_return")[0])
        view["user_messages"].append({"event_id": "actual-confirm", "message": phrase})
        args, _, _, facts, refs = choose(view, "submit_confirmed_return")
        self.assertEqual(args, {"operation_id": "independent-operation"})
        self.assertEqual(refs["operation_id"], ("native-preview", ["operation_id"]))
        policy = {"policy_step_id": "decision", "candidate_tool_name": "submit_confirmed_return", "proposed_action": "CALL"}
        events = [{"sequence": 2, "kind": "confirmation_recorded", "operation_id": "independent-operation"},
                  {"sequence": 3, "kind": "policy_proposal", "policy_step_id": "decision"}]
        self.assertTrue(verify_visible_action(policy, {"preceding_visible_facts": facts}, events))

    def test_preview_permission_survives_later_confirmation_text(self):
        view = self.view()
        view["user_messages"][0]["message"] += "只授权生成退货预览。"
        phrase = view["tool_facts"][-1]["data"]["confirmation_phrase"]
        view["agent_messages"].append({"event_id": "actual-agent", "message": phrase})
        view["user_messages"].append({"event_id": "actual-confirm", "message": phrase})
        original = deepcopy(view)
        self.assertIsNone(choose(view, "submit_confirmed_return")[0])
        self.assertEqual(view, original)


if __name__ == "__main__":
    unittest.main()
