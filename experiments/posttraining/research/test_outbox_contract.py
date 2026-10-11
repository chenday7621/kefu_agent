"""CPU regression for the specifically authorized native error type guards."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
import unittest

from apps.retail_demo.outbox import Outbox


class OutboxContractTests(unittest.IsolatedAsyncioTestCase):
    def outbox(self, payload, tool="submit_confirmed_return"):
        class Cursor:
            async def fetchone(self):
                return {"last_user_offset": 0}

            async def fetchall(self):
                return [
                    {
                        "doc": {
                            "data": {
                                "tool_calls": [
                                    {
                                        "tool_id": "retail-demo-business:" + tool,
                                        "arguments": {"operation_id": "11111111-1111-1111-1111-111111111111"},
                                        "result": {"data": payload},
                                    }
                                ]
                            }
                        }
                    }
                ]

        class Conn:
            async def execute(self, *args):
                return Cursor()

        class DB:
            @asynccontextmanager
            async def connection(self):
                yield Conn()

        return Outbox(SimpleNamespace(_database=DB()))

    async def test_actual_native_string_failure_returns_no_receipt(self):
        self.assertIsNone(await self.outbox("Tool call error").for_submission_trace(
                "isolated-session", "explicit-trace"
            ))

    async def test_existing_structured_rejection_returns_no_receipt(self):
        value = await self.outbox(
            {"ok": False, "error": {"code": "SESSION_FORBIDDEN"}, "data": None}
        ).for_submission_trace("isolated-session", "explicit-trace")
        self.assertIsNone(value)

    async def test_failed_preparation_string_returns_no_receipt(self):
        self.assertIsNone(await self.outbox("Tool call error", "check_return_eligibility").for_submission_trace(
                "isolated-session", "explicit-trace"
            ))

    async def test_non_success_payloads_cannot_create_a_receipt(self):
        for payload in (None, [], ["error"], {}, {"ok": True}, {"ok": False},
                        {"ok": True, "data": {}}, {"ok": True, "data": {"request": {}}}):
            with self.subTest(payload=payload):
                self.assertIsNone(await self.outbox(payload).for_submission_trace(
                    "isolated-session", "explicit-trace"))

    async def test_unsupported_success_structure_is_not_silently_swallowed(self):
        for payload in ({"ok": True, "data": ["invalid"]},
                        {"ok": True, "data": {"request": ["invalid"]}}):
            with self.subTest(payload=payload):
                with self.assertRaises(AttributeError):
                    await self.outbox(payload).for_submission_trace(
                        "isolated-session", "explicit-trace")


if __name__ == "__main__":
    unittest.main()
