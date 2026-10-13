"""Synthetic new-template facts only; no DEV cases, model/API/DB execution."""

import unittest
from .expert import choose, parse_user_messages
from .data_scenarios import scenario
from .split_policy import register, new_groups


class ExpertTests(unittest.TestCase):
    def view(self, text, facts=None, agents=None):
        return {
            "user_messages": [{"event_id": "user-current", "message": text}],
            "agent_messages": agents or [],
            "tool_facts": facts or [],
        }

    def test_missing_reason_never_guessed_and_preview_permission_blocks_submit(self):
        view = self.view("仅为我生成预览。订单：ORD-X-A；明细：ITEM-X-A；数量：2。原因还未说明。")
        args, _, hard, _, _ = choose(view, "check_return_eligibility")
        self.assertIsNone(args)
        self.assertIsNone(choose(view, "submit_confirmed_return")[0])
        self.assertTrue(choose(view, "submit_confirmed_return")[2])
        self.assertNotIn("reason", parse_user_messages(view["user_messages"])[0])

    def test_product_id_resolved_only_after_real_visible_tool_fact(self):
        text = "订单：ORD-X-A。名称准确为“桦木托盘”；数量：2；原因：边角有擦痕。确认后提交。"
        view = self.view(text)
        self.assertEqual(choose(view, "get_order_details")[0], {"order_id": "ORD-X-A"})
        self.assertIsNone(choose(view, "check_return_eligibility")[0])
        fact = {
            "event_id": "explicit-result-id",
            "tool_name": "get_order_details",
            "data": {
                "order": {"id": "ORD-X-A", "status": "delivered"},
                "items": [
                    {
                        "id": "ITEM-X-A",
                        "product_name": "桦木托盘",
                        "returnable": True,
                        "available_return_quantity": 3,
                    }
                ],
            },
        }
        args, _, _, _, refs = choose(self.view(text, [fact]), "check_return_eligibility")
        self.assertEqual(
            args,
            {"order_id": "ORD-X-A", "item_id": "ITEM-X-A", "quantity": 2, "reason": "边角有擦痕"},
        )
        self.assertEqual(refs["item_id"], ("explicit-result-id", ["items", 0, "id"]))

    def test_no_confirmation_from_a_tool_result_or_wrong_user_phrase(self):
        text = "订单：ORD-X-A；明细：ITEM-X-A；数量：2；原因：边角有擦痕。"
        read = {
            "event_id": "read-result",
            "tool_name": "get_order_details",
            "data": {
                "order": {"id": "ORD-X-A", "status": "delivered"},
                "items": [
                    {
                        "id": "ITEM-X-A",
                        "product_name": "桦木托盘",
                        "returnable": True,
                        "available_return_quantity": 3,
                    }
                ],
            },
        }
        op = {
            "event_id": "preview-result",
            "tool_name": "check_return_eligibility",
            "data": {
                "operation_id": "op-visible",
                "preview": {
                    "order_id": "ORD-X-A",
                    "item_id": "ITEM-X-A",
                    "quantity": 2,
                    "reason": "边角有擦痕",
                },
            },
        }
        v = self.view(
            text,
            [read, op],
            [
                {
                    "event_id": "shown",
                    "message": "确认退货 op-visible 订单ORD-X-A 明细ITEM-X-A 数量2",
                }
            ],
        )
        self.assertIsNone(choose(v, "submit_confirmed_return")[0])
        v["user_messages"].append(
            {
                "event_id": "actual-user-confirm",
                "message": "确认退货 op-visible 订单ORD-X-A 明细ITEM-X-A 数量2",
            }
        )
        self.assertEqual(choose(v, "submit_confirmed_return")[0], {"operation_id": "op-visible"})

    def test_new_scenarios_match_registered_dimensions_without_future_visible_data(self):
        groups = register(new_groups())
        tasks = [scenario(g, i + 1) for i, g in enumerate(groups)]
        self.assertEqual(len(tasks), 6)
        self.assertNotIn("reason", tasks[4]["visible_goal_fields"])
        self.assertNotIn("quantity", tasks[5]["visible_goal_fields"])
        self.assertNotIn("quantity", tasks[3]["visible_goal_fields"])
        self.assertFalse(any(t["initial_db_fixture"]["tables"]["return_operations"] for t in tasks))
        self.assertTrue(all(t["episode_budget"]["max_policy_decisions"] == 30 for t in tasks))


if __name__ == "__main__":
    unittest.main()
