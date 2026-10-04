"""Model-free runtime checks of the V0.2 gate, bridge and emission path."""

import asyncio
from dataclasses import dataclass
import json
from types import SimpleNamespace
import unittest

import parlant.sdk as p
from parlant.core.emission.event_publisher import EventPublisher
from parlant.core.engines.alpha.prompt_builder import PromptBuilder
from parlant.core.emissions import EmittedEvent
from parlant.core.sessions import EventKind, EventSource
from tau2.data_model.message import ToolMessage
from tau2.runner import build_environment

from v02_bridge import ToolBridge
from v02_guard import ExecutionGuard, TOOL_POLICY, WRITE_TOOLS, derive_product_fact
from v02_observe import Observer, install_parlant_hooks
from v02_reply import check_reply


def sample_guard():
    return ExecutionGuard(build_environment("retail").get_tools())


def prime(g, session="s", orders=("#W1000001",), user="sample_user", register=True):
    if register:
        g.register(session)
    g.user_message(session, "My name is Ada Lovelace and zip code 12345.")
    args = {"first_name": "Ada", "last_name": "Lovelace", "zip": "12345"}
    assert g.check(session, "find_user_id_by_name_zip", args)[0] == "forwarded_to_official"
    g.official_result(session, "find_user_id_by_name_zip", args, user, "auth-call")
    g.official_result(session, "get_user_details", {"user_id": user},
                      {"user_id": user, "orders": list(orders),
                       "payment_methods": {"card_one": {"last_four": "1357"}}}, "user-call")
    for oid in orders:
        g.official_result(session, "get_order_details", {"order_id": oid},
                          {"order_id": oid, "user_id": user, "status": "pending",
                           "items": [{"item_id": "old_item", "product_id": "product_one"}],
                           "payment_history": [{"transaction_type": "payment", "payment_method_id": "card_one"}]},
                          f"order-{oid}")
    return g


class GuardTests(unittest.TestCase):
    def test_registry_matches_official_and_includes_profile_address(self):
        g = sample_guard()
        self.assertEqual(set(g.tool_schemas), set(TOOL_POLICY))
        self.assertIn("modify_user_address", WRITE_TOOLS)

    def test_auth_order_and_cross_session_isolation(self):
        g = sample_guard()
        g.register("a")
        self.assertEqual(g.check("a", "get_user_details", {"user_id": "self_reported"})[1],
                         "authenticate_first_with_email_or_name_and_zip")
        g.user_message("a", "My order is #W1000001 and user ID is self_reported")
        self.assertEqual(g.check("a", "get_order_details", {"order_id": "#W1000001"})[0], "blocked")
        self.assertEqual(g.check("a", "find_user_id_by_email", {"email": "invented@example.com"})[0], "blocked")
        g.user_message("a", "Use ada@example.com")
        a = {"email": "ada@example.com"}
        self.assertEqual(g.check("a", "find_user_id_by_email", a)[0], "forwarded_to_official")
        g.official_result("a", "find_user_id_by_email", a, "ada_user", "auth-1")
        self.assertEqual(g.check("a", "get_user_details", {"user_id": "ada_user"})[0], "forwarded_to_official")
        self.assertEqual(g.check("a", "get_user_details", {"user_id": "other_user"})[0], "blocked")
        self.assertEqual(g.check("a", "get_order_details", {"order_id": "#W1000001"})[0], "blocked")
        g.official_result("a", "get_user_details", {"user_id": "ada_user"},
                          {"user_id": "ada_user", "orders": ["#W1000001"]}, "user-1")
        self.assertEqual(g.check("a", "get_order_details", {"order_id": "#W1000001"})[0], "forwarded_to_official")
        self.assertEqual(g.check("a", "get_order_details", {"order_id": "#W9999999"})[0], "blocked")
        g.register("b")
        self.assertEqual(g.check("b", "get_order_details", {"order_id": "#W1000001"})[0], "blocked")
        g.user_message("a", "My email is now second@example.com")
        second_lookup = {"email": "second@example.com"}
        g.official_result("a", "find_user_id_by_email", second_lookup, "another_user", "auth-2")
        self.assertIsNone(g.state("a").authenticated_user_id)
        self.assertEqual(g.check("a", "get_order_details", {"order_id": "#W1000001"})[0], "blocked")
        g.unregister("a")
        g.register("a")
        self.assertIsNone(g.state("a").authenticated_user_id)

    def test_nested_placeholder_and_incomplete_product_result(self):
        g = prime(sample_guard())
        g.state("s").orders["#W1000001"]["status"] = "delivered"
        g.user_message("s", "Please exchange the old item for the purple item.")
        g.assistant_message("s", "I can exchange order #W1000001 old_item for the purple option. Please confirm.")
        g.user_message("s", "Yes, please proceed.")
        args = {"order_id": "#W1000001", "item_ids": ["old_item"],
                "new_item_ids": ["<<__missing__>>"], "payment_method_id": "card_one"}
        self.assertEqual(g.check("s", "exchange_delivered_order_items", args)[1], "missing_or_placeholder_parameter")
        args["new_item_ids"] = ["new_item"]
        self.assertEqual(g.check("s", "exchange_delivered_order_items", args)[1],
                         "new_item_not_in_completed_available_product_result")

    def test_confirmation_scope_failure_recovery_and_unknown(self):
        g = prime(sample_guard())
        args = {"order_id": "#W1000001", "reason": "no longer needed"}
        g.user_message("s", "Cancel order #W1000001 because it is no longer needed")
        self.assertEqual(g.check("s", "cancel_pending_order", args)[1],
                         "no_current_explicit_confirmation_for_displayed_action")
        g.assistant_message("s", "I can cancel order #W1000001 for no longer needed. Please confirm.")
        g.user_message("s", "Yes, but only if it is free")
        self.assertEqual(g.check("s", "cancel_pending_order", args)[0], "blocked")
        g.assistant_message("s", "I can cancel order #W1000001 for no longer needed. Please confirm.")
        g.user_message("s", "Yes, please proceed")
        self.assertEqual(g.check("s", "cancel_pending_order", args)[0], "forwarded_to_official")
        decision, reason, evidence = g.check("s", "cancel_pending_order", args)
        g.record_attempt("s", "cancel_pending_order", args, decision, reason, evidence,
                         parlant_call_id="p1", batch=0, iteration=0, official_call_id="o1")
        g.official_result("s", "cancel_pending_order", args, "Error: Non-pending order cannot be cancelled", "o1")
        self.assertEqual(g.state("s").operations[next(iter(g.state("s").operations))].status, "failed_no_write")
        self.assertEqual(g.check("s", "cancel_pending_order", args)[0], "blocked")
        g.assistant_message("s", "I can cancel order #W1000001 for no longer needed. Please confirm.")
        g.user_message("s", "Yes, please proceed")
        self.assertEqual(g.check("s", "cancel_pending_order", args)[0], "forwarded_to_official")
        decision, reason, evidence = g.check("s", "cancel_pending_order", args)
        g.record_attempt("s", "cancel_pending_order", args, decision, reason, evidence,
                         parlant_call_id="p2", batch=0, iteration=0, official_call_id="o2")
        g.unknown_result("s", "cancel_pending_order", args)
        self.assertEqual(g.check("s", "cancel_pending_order", args)[0], "blocked")
        self.assertEqual(g.check("s", "cancel_pending_order", {"order_id": "#W1000001", "reason": "ordered by mistake"})[1],
                         "resource_has_unresolved_unknown_write")

    def test_payment_difference_is_not_whole_order_payment_consent(self):
        g = prime(sample_guard())
        g.user_message("s", "Change just the product and charge the price difference to card_one.")
        g.assistant_message("s", "I can change the item on order #W1000001 and use card_one for the difference. Please confirm.")
        g.user_message("s", "Yes, please proceed")
        decision = g.check("s", "modify_pending_order_payment", {"order_id": "#W1000001", "payment_method_id": "card_one"})
        self.assertEqual(decision[0], "blocked")

    def test_item_change_uses_returned_variant_and_displayed_difference(self):
        g = prime(sample_guard())
        g.state("s").orders["#W1000001"]["items"][0]["price"] = 5.00
        g.official_result("s", "get_product_details", {"product_id": "product_one"},
                          {"product_id": "product_one", "name": "Sample Product",
                           "variants": {"new_item": {"available": True, "price": 6.00,
                                                     "options": {"color": "purple"}}}}, "product-call")
        args = {"order_id": "#W1000001", "item_ids": ["old_item"],
                "new_item_ids": ["new_item"], "payment_method_id": "card_one"}
        g.user_message("s", "Change my item to purple and use card_one for the difference.")
        g.assistant_message("s", "I can change the item in order #W1000001 from old_item to purple new_item, with a $2.00 difference on card_one. Please confirm.")
        g.user_message("s", "Yes, please proceed")
        self.assertEqual(g.check("s", "modify_pending_order_items", args)[1],
                         "computed_price_difference_not_displayed_in_confirmed_plan")
        g.assistant_message("s", "I can change the item in order #W1000001 from old_item to purple new_item, with a $1.00 difference on card_one. Please confirm.")
        g.user_message("s", "Yes, please proceed")
        decision, _, evidence = g.check("s", "modify_pending_order_items", args)
        self.assertEqual(decision, "forwarded_to_official")
        self.assertEqual(evidence["computed_difference"], "1.0")

    def test_stale_and_ambiguous_confirmation_are_rejected(self):
        g = prime(sample_guard(), orders=("#W1000001", "#W1000002"))
        args = {"order_id": "#W1000001", "reason": "no longer needed"}
        g.user_message("s", "Cancel #W1000001 because no longer needed")
        g.assistant_message("s", "I can cancel #W1000001 for no longer needed. Please confirm.")
        g.user_message("s", "Maybe yes")
        self.assertEqual(g.check("s", "cancel_pending_order", args)[0], "blocked")
        g.assistant_message("s", "I can cancel #W1000001 for no longer needed. Please confirm.")
        g.user_message("s", "Yes, cancel #W1000002 instead")
        self.assertEqual(g.check("s", "cancel_pending_order", args)[0], "blocked")
        g.assistant_message("s", "I can cancel #W1000001 for no longer needed. Please confirm.")
        g.user_message("s", "Yes, please proceed")
        self.assertEqual(g.check("s", "cancel_pending_order", args)[0], "forwarded_to_official")
        g.user_message("s", "Actually change the target")
        self.assertEqual(g.check("s", "cancel_pending_order", args)[0], "blocked")

    def test_order_and_profile_address_require_distinct_plans(self):
        g = prime(sample_guard())
        address = {"address1": "42 Oak Road", "address2": "", "city": "Boston", "state": "MA", "country": "USA", "zip": "02110"}
        g.user_message("s", "Change my default profile address to 42 Oak Road, Boston, MA 02110, USA.")
        g.assistant_message("s", "I can change your default profile address to 42 Oak Road, Boston, MA 02110, USA. Please confirm.")
        g.user_message("s", "Yes, please proceed")
        self.assertEqual(g.check("s", "modify_user_address", {"user_id": "sample_user", **address})[0], "forwarded_to_official")
        self.assertEqual(g.check("s", "modify_pending_order_address", {"order_id": "#W1000001", **address})[0], "blocked")
        g.assistant_message("s", "I can change the order shipping address for #W1000001 to 42 Oak Road, Boston, MA 02110, USA. Please confirm.")
        g.user_message("s", "Yes, please proceed")
        self.assertEqual(g.check("s", "modify_pending_order_address", {"order_id": "#W1000001", **address})[0], "forwarded_to_official")

    def test_independent_order_writes_and_success_reuse(self):
        g = prime(sample_guard(), orders=("#W1000001", "#W1000002"))
        g.user_message("s", "Cancel orders #W1000001 and #W1000002 because no longer needed")
        g.assistant_message("s", "I can cancel #W1000001 and #W1000002 for no longer needed. Please confirm.")
        g.user_message("s", "Yes, proceed with both orders")
        for n, oid in enumerate(("#W1000001", "#W1000002")):
            args = {"order_id": oid, "reason": "no longer needed"}
            decision, reason, evidence = g.check("s", "cancel_pending_order", args)
            self.assertEqual(decision, "forwarded_to_official")
            g.record_attempt("s", "cancel_pending_order", args, decision, reason, evidence,
                             parlant_call_id=f"p{n}", batch=0, iteration=0, official_call_id=f"o{n}")
            g.official_result("s", "cancel_pending_order", args,
                              {"order_id": oid, "user_id": "sample_user", "status": "cancelled"}, f"o{n}")
        self.assertEqual(g.check("s", "cancel_pending_order", {"order_id": "#W1000001", "reason": "no longer needed"})[0], "cached_reuse")

    def test_derived_count_and_reply_repair_across_sizes_and_incomplete_data(self):
        for flags in ([True, False, True], [False, True, True, False, True], [True]):
            data = {"product_id": "p", "name": "Sample Product",
                    "variants": {str(i): {"available": flag} for i, flag in reversed(list(enumerate(flags)))}}
            fact = derive_product_fact(data, "product-call")
            self.assertEqual(fact["available_true"], sum(flags))
            g = sample_guard(); g.register("s"); g.state("s").product_facts["p"] = fact
            reply, evidence = check_reply(g, "s", f"Sample Product has 99 available variants.")
            self.assertIn(f"{sum(flags)} available variants", reply)
            self.assertTrue(evidence["corrections"])
        incomplete = derive_product_fact({"product_id": "p", "variants": {"a": {"available": True}, "b": {}}}, "x")
        self.assertFalse(incomplete["complete"])
        self.assertIsNone(incomplete["available_true"])
        unknown_guard = sample_guard(); unknown_guard.register("u")
        incomplete["product_name"] = "Unknown Product"
        unknown_guard.state("u").product_facts["p"] = incomplete
        reply, evidence = check_reply(unknown_guard, "u", "Unknown Product has 7 available variants.")
        self.assertIn("unknown", reply)
        self.assertTrue(evidence["corrections"])
        g = sample_guard(); g.register("s")
        g.state("s").product_facts = {
            "a": derive_product_fact({"product_id": "a", "name": "Product Alpha", "variants": {"1": {"available": True}}}, "call-a"),
            "b": derive_product_fact({"product_id": "b", "name": "Product Beta", "variants": {"1": {"available": True}, "2": {"available": True}}}, "call-b"),
        }
        reply, _ = check_reply(g, "s", "Product Alpha has 9 available variants. Product Beta has 9 available variants.")
        self.assertIn("Product Alpha has 1 available", reply)
        self.assertIn("Product Beta has 2 available", reply)

    def test_availability_ratio_preserves_total_and_corrects_both_fields(self):
        g = sample_guard(); g.register("s")
        fact = derive_product_fact({"product_id": "p", "name": "Sample Product",
                                    "variants": {str(i): {"available": i < 3} for i in range(5)}}, "source-p")
        g.state("s").product_facts["p"] = fact
        correct, evidence = check_reply(g, "s", "Sample Product has 3 of its 5 variants currently available.")
        self.assertEqual(correct, "Sample Product has 3 of its 5 variants currently available.")
        self.assertEqual(evidence["corrections"], [])
        corrected, evidence = check_reply(g, "s", "Sample Product has 5 of its 3 variants currently available.")
        self.assertEqual(corrected, "Sample Product has 3 of its 5 variants currently available.")
        self.assertEqual({x["field"] for x in evidence["corrections"]}, {"available_true", "variants_total"})
        text, evidence = check_reply(g, "s", "I can change these orders. They can no longer be modified or cancelled after a change. Please confirm.")
        self.assertEqual(text, "I can change these orders. They can no longer be modified or cancelled after a change. Please confirm.")
        self.assertEqual(evidence["corrections"], [])

    def test_failed_blocked_and_unknown_cannot_be_claimed_complete(self):
        g = prime(sample_guard())
        text, evidence = check_reply(g, "s", "Done. Your return for #W1000001 has been submitted successfully.")
        self.assertIn("not completed", text)
        self.assertTrue(evidence["corrections"])
        text, _ = check_reply(g, "s", "Your card ending in 9999 was charged.")
        self.assertNotIn("9999", text)
        g.state("s").successes.append({"tool_name": "return_delivered_order_items", "order_id": "#W1000001",
                                        "official_call_id": "official-success"})
        text, evidence = check_reply(g, "s", "Your return for #W1000001 has been submitted successfully.")
        self.assertIn("submitted successfully", text)
        self.assertFalse(evidence["corrections"])
        text, evidence = check_reply(g, "s", "The return request for order #W1000001 has been submitted.")
        self.assertEqual(text, "The return request for order #W1000001 has been submitted.")
        self.assertFalse(evidence["corrections"])
        text, evidence = check_reply(g, "s", "Your return for #W1000002 has been submitted successfully.")
        self.assertIn("not completed", text)


class RuntimePathTests(unittest.IsolatedAsyncioTestCase):
    async def test_bridge_product_result_keeps_raw_official_data_and_adds_agent_fact(self):
        g = sample_guard(); bridge = ToolBridge(2, guard=g); q = bridge.register("s")
        ctx = p.ToolContext(agent_id="test", session_id="s", customer_id="test")
        work = asyncio.create_task(bridge.invoke(ctx, "get_product_details", {"product_id": "synthetic-product"}))
        ticket = await asyncio.to_thread(q.get, True, 1)
        raw = {"product_id": "synthetic-product", "name": "Sample Product",
               "variants": {"a": {"available": True}, "b": {"available": False}}}
        official = ToolMessage(role="tool", id=ticket.call.id, content=json.dumps(raw))
        bridge.fulfill(ticket, official)
        result = await work
        self.assertEqual(json.loads(official.content), raw)
        self.assertEqual(result.data["_v02_derived_fact"]["available_true"], 1)
        self.assertEqual(result.data["variants"], raw["variants"])

    async def test_related_write_is_rechecked_after_first_result(self):
        g = sample_guard(); bridge = ToolBridge(2, guard=g); q = bridge.register("s")
        prime(g, register=False)
        g.user_message("s", "Cancel order #W1000001 because no longer needed and change the order payment method to card_one")
        g.assistant_message("s", "I can cancel order #W1000001 for no longer needed and change the order payment method to card_one. Please confirm.")
        g.user_message("s", "Yes, please proceed")
        ctx = p.ToolContext(agent_id="test", session_id="s", customer_id="test")
        cancel = {"order_id": "#W1000001", "reason": "no longer needed"}
        payment = {"order_id": "#W1000001", "payment_method_id": "card_one"}
        first = asyncio.create_task(bridge.invoke(ctx, "cancel_pending_order", cancel))
        ticket = await asyncio.to_thread(q.get, True, 1)
        second = asyncio.create_task(bridge.invoke(ctx, "modify_pending_order_payment", payment))
        await asyncio.sleep(0)
        self.assertTrue(q.empty())
        bridge.fulfill(ticket, ToolMessage(role="tool", id=ticket.call.id,
                                           content=json.dumps({"order_id": "#W1000001", "user_id": "sample_user", "status": "cancelled"})))
        await first
        blocked = await second
        self.assertTrue(blocked.data["v02_local_block"])
        self.assertEqual(blocked.data["error"], "latest_official_order_status_not_eligible")
        self.assertTrue(q.empty())
        reused = await bridge.invoke(ctx, "cancel_pending_order", cancel)
        self.assertEqual(reused.metadata["v02_source"], "cached_official_result")
        self.assertTrue(q.empty())

    async def test_block_has_no_official_ticket_and_forwarded_result_maps(self):
        g = sample_guard(); observer = Observer(); observer.set_task("synthetic")
        bridge = ToolBridge(2, observer, g)
        q = bridge.register("s")
        ctx = p.ToolContext(agent_id="test", session_id="s", customer_id="test")
        blocked = await bridge.invoke(ctx, "get_order_details", {"order_id": "#W1000001"})
        self.assertTrue(blocked.data["v02_local_block"])
        self.assertTrue(q.empty())
        g.user_message("s", "Use ada@example.com")
        work = asyncio.create_task(bridge.invoke(ctx, "find_user_id_by_email", {"email": "ada@example.com"}))
        ticket = await asyncio.to_thread(q.get, True, 1)
        bridge.fulfill(ticket, ToolMessage(role="tool", id=ticket.call.id, content="ada_user"))
        result = await work
        self.assertEqual(result.data, "ada_user")
        self.assertEqual(g.state("s").authenticated_user_id, "ada_user")
        events = g.state("s").events
        self.assertIsNone(next(e for e in events if e.get("decision") == "blocked")["official_call_id"])
        self.assertEqual(next(e for e in events if e.get("decision") == "forwarded_to_official")["official_call_id"], ticket.call.id)

    async def test_actual_event_publisher_sends_corrected_text(self):
        g = sample_guard(); g.register("s")
        fact = derive_product_fact({"product_id": "p", "name": "Sample Product",
                                    "variants": {"a": {"available": True}, "b": {"available": False}}}, "official-p")
        g.state("s").product_facts["p"] = fact
        observer = Observer(); observer.set_task("synthetic")
        install_parlant_hooks(observer, g)

        class Store:
            def __init__(self): self.data = None
            async def create_event(self, **kwargs):
                self.data = kwargs["data"]
                return SimpleNamespace(id="persisted")

        store = Store()
        publisher = EventPublisher(SimpleNamespace(id="agent", name="Agent"), store, "s")
        await publisher.emit_message_event(trace_id="trace", data={"message": "For Sample Product, 8 of its 9 variants are currently available.",
                                                          "participant": {"id": "agent", "display_name": "Agent"}})
        self.assertEqual(store.data["message"], "For Sample Product, 1 of its 2 variants are currently available.")
        self.assertTrue(any(r["kind"] == "reply_pre_emission_check" and r["corrections"]
                            for r in observer.task_records("synthetic")))

    async def test_agent_side_derived_field_is_in_real_prompt_builder_tool_section(self):
        fact = derive_product_fact({"product_id": "p", "name": "Sample Product",
                                    "variants": {"a": {"available": True}, "b": {"available": False}}}, "official-p")
        event = EmittedEvent(source=EventSource.SYSTEM, kind=EventKind.TOOL, trace_id="trace",
                             data={"tool_calls": [{"tool_id": "retail:get_product_details", "arguments": {"product_id": "p"},
                                                   "result": {"data": {"product_id": "p", "_v02_derived_fact": fact}}}]},
                             metadata=None)
        prompt = PromptBuilder().add_staged_tool_events([event]).build()
        self.assertIn("_v02_derived_fact", prompt)
        self.assertIn("official-p", prompt)
        self.assertIn('"available_true": 1', prompt)


if __name__ == "__main__":
    unittest.main()
