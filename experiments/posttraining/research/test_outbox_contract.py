"""Read-only CPU reproduction of the frozen native failure-payload mismatch."""

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
                                        "arguments": {"operation_id": "invalid-visible-input"},
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

    async def test_actual_native_string_failure_raises_in_frozen_outbox(self):
        with self.assertRaisesRegex(AttributeError, "has no attribute 'get'"):
            await self.outbox("Tool call error").for_submission_trace(
                "isolated-session", "explicit-trace"
            )

    async def test_existing_structured_rejection_returns_no_receipt(self):
        value = await self.outbox(
            {"ok": False, "error": {"code": "SESSION_FORBIDDEN"}, "data": None}
        ).for_submission_trace("isolated-session", "explicit-trace")
        self.assertIsNone(value)

    async def test_failed_preparation_string_has_the_same_native_contract_problem(self):
        with self.assertRaisesRegex(AttributeError, "has no attribute 'get'"):
            await self.outbox("Tool call error", "check_return_eligibility").for_submission_trace(
                "isolated-session", "explicit-trace"
            )


if __name__ == "__main__":
    unittest.main()
