"""Local, model-free tests for V0.1 configuration and bridge invariants."""

import asyncio
import json
import unittest

import parlant.sdk as p
from tau2.data_model.message import ToolMessage

from bridge import AdapterError, SECTION_TOOLS, ToolBridge
from business_config import GUIDANCE, coverage
from observe import Observer, result_summary


class ConfigurationTests(unittest.TestCase):
    def test_every_official_tool_has_a_rule_path(self):
        from tau2.runner import build_environment
        names = [tool.name for tool in build_environment("retail").get_tools()]
        report = coverage(names, SECTION_TOOLS)
        self.assertEqual(report["missing"], [])
        self.assertIn("guidance:default_address", report["associations"]["modify_user_address"])
        self.assertIn("policy:Modify pending order", report["associations"]["modify_pending_order_address"])

    def test_product_count_uses_returned_available_field(self):
        data = {"variants": {"one": {"available": True}, "two": {"available": False},
                             "three": {"available": True}, "four": {}}}
        summary = result_summary("get_product_details", data)
        self.assertEqual((summary["variants_total"], summary["available_true"],
                          summary["available_field_count"]), (4, 2, 3))
        self.assertTrue(any("available field is true" in item.action for item in GUIDANCE))


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_ticket_identity_and_result_return(self):
        observer = Observer()
        observer.set_task("synthetic")
        bridge = ToolBridge(2, observer)
        session = "synthetic-session"
        outgoing = bridge.register(session)
        arguments = {"order_id": "synthetic-order"}
        observer.register_parlant_call(session, "parlant-call-1", "get_order_details", arguments)
        context = p.ToolContext(agent_id="synthetic", session_id=session, customer_id="synthetic")
        work = asyncio.create_task(bridge.invoke(context, "get_order_details", arguments))
        ticket = await asyncio.to_thread(outgoing.get, True, 1)
        self.assertEqual(ticket.parlant_call_id, "parlant-call-1")
        self.assertNotEqual(ticket.ticket_id, ticket.call.id)
        bridge.fulfill(ticket, ToolMessage(role="tool", id=ticket.call.id,
                                           content=json.dumps({"status": "pending"})))
        result = await work
        self.assertEqual(result.data["status"], "pending")
        kinds = [row["kind"] for row in observer.task_records("synthetic")]
        self.assertEqual(kinds, ["parlant_call_started", "bridge_ticket_created",
                                 "official_result_fulfilled", "bridge_result_returned"])

    async def test_write_is_not_replayed(self):
        bridge = ToolBridge(2)
        session = "synthetic-write-session"
        outgoing = bridge.register(session)
        context = p.ToolContext(agent_id="synthetic", session_id=session, customer_id="synthetic")
        arguments = {"user_id": "synthetic-user", "address1": "A"}
        first = asyncio.create_task(bridge.invoke(context, "modify_user_address", arguments))
        ticket = await asyncio.to_thread(outgoing.get, True, 1)
        with self.assertRaises(AdapterError):
            await bridge.invoke(context, "modify_user_address", arguments)
        self.assertTrue(outgoing.empty())
        bridge.fulfill(ticket, ToolMessage(role="tool", id=ticket.call.id,
                                           content=json.dumps({"address": {"address1": "A"}})))
        await first


if __name__ == "__main__":
    unittest.main()
