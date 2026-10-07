"""Narrow closeout checks; no demo orders, applications or model calls.

Query regressions execute PostgreSQL against transaction-local temporary tables
and roll back. They do not replace the prior full Store/MCP/Outbox evidence.
"""

import copy
from contextlib import asynccontextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import UUID
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from parlant.core.app_modules.sessions import SessionModule
from parlant.core.background_tasks import BackgroundTaskService
from parlant.core.loggers import LogLevel, StdoutLogger
from parlant.core.tracer import LocalTracer
from ..coordination import SessionCoordinator
from ..outbox import Outbox
from ..postgres_documents import PostgresDocumentDatabase
from ..settings import load_settings


class CoordinationCompatibility(unittest.TestCase):
    def native_pair(self):
        tasks = BackgroundTaskService(StdoutLogger(LocalTracer(), LogLevel.ERROR))
        module = SessionModule.__new__(SessionModule)
        module._background_task_service = tasks
        return module, tasks

    def test_current_native_interface_installs(self):
        module, tasks = self.native_pair()
        coordinator = SessionCoordinator()
        coordinator.install(module, tasks)
        self.assertIs(coordinator.tasks, tasks)

    def test_incompatible_version_signature_tag_or_task_map_fails_before_install(self):
        async def changed_process(session, extra):
            pass

        async def changed_dispatch(session):
            pass

        for mismatch in ("version", "signature", "tag", "task_map", "task_service"):
            with self.subTest(mismatch=mismatch):
                module, tasks = self.native_pair()
                if mismatch == "signature":
                    module._process_session = changed_process
                elif mismatch == "tag":
                    module.dispatch_processing_task = changed_dispatch
                elif mismatch == "task_map":
                    tasks._tasks = None
                elif mismatch == "task_service":
                    module._background_task_service = object()
                coordinator = SessionCoordinator()
                original = module._process_session
                with patch(
                    "apps.retail_demo.coordination.VERSION",
                    "future" if mismatch == "version" else "3.3.2",
                ):
                    with self.assertRaisesRegex(RuntimeError, "refusing to continue"):
                        coordinator.install(module, tasks)
                self.assertIsNone(coordinator.tasks)
                self.assertEqual(module._process_session, original)


class SubmissionScopeSQL(unittest.IsolatedAsyncioTestCase):
    async def test_only_current_matching_submission_can_replace_message(self):
        settings = load_settings()
        connection = await AsyncConnection.connect(settings.database_url, row_factory=dict_row)
        try:
            await connection.execute(
                "CREATE TEMP TABLE parlant_events(id text PRIMARY KEY,session_id text,trace_id text,event_kind text,event_source text,deleted boolean,event_offset bigint,doc jsonb) ON COMMIT DROP"
            )
            await connection.execute(
                "CREATE TEMP TABLE return_outbox(id uuid PRIMARY KEY,operation_id uuid,request_id uuid,session_id text,status text,receipt_event_id text) ON COMMIT DROP"
            )
            await connection.execute("SET LOCAL search_path=pg_temp,retail_demo,public")

            @asynccontextmanager
            async def temporary_transaction():
                yield connection

            database = PostgresDocumentDatabase(settings.database_url, "sessions")
            database.connection = temporary_transaction
            outbox = Outbox(SimpleNamespace(_database=database))
            op = UUID("11111111-1111-4111-8111-111111111111")
            rid = UUID("22222222-2222-4222-8222-222222222222")
            oid = UUID("33333333-3333-4333-8333-333333333333")
            other = "44444444-4444-4444-8444-444444444444"
            base_tool = {
                "tool_id": "retail-demo-business:create_return_request",
                "arguments": {"operation_id": str(op)},
                "result": {
                    "data": {
                        "ok": True,
                        "data": {"request": {"id": str(rid), "operation_id": str(op)}},
                    }
                },
            }
            prep = {
                "tool_id": "retail-demo-business:check_return_eligibility",
                "result": {"data": {"ok": True}},
            }

            async def event(
                eid, offset, kind, data, source="ai_agent", trace="current", deleted=False
            ):
                await connection.execute(
                    "INSERT INTO parlant_events VALUES(%s,'s',%s,%s,%s,%s,%s,%s)",
                    (eid, trace, kind, source, deleted, offset, Jsonb({"data": data})),
                )

            cases = (
                "current_pending",
                "later_user_same_trace",
                "later_preparation",
                "same_batch_preparation",
                "wrong_service",
                "wrong_request",
                "wrong_operation",
                "deleted_tool",
                "later_failed_submission",
                "delivered_previous_turn",
                "delivered_other_trace",
                "delivered_current_turn",
            )
            for case in cases:
                with self.subTest(case=case):
                    await connection.execute("DELETE FROM parlant_events")
                    await connection.execute("DELETE FROM return_outbox")
                    tool = copy.deepcopy(base_tool)
                    if case == "wrong_service":
                        tool["tool_id"] = "unrelated:create_return_request"
                    elif case == "wrong_request":
                        tool["result"]["data"]["data"]["request"]["id"] = other
                    elif case == "wrong_operation":
                        tool["result"]["data"]["data"]["request"]["operation_id"] = other
                    await event(
                        "user", 10, "message", {"message": "confirmation fixture"}, "customer"
                    )
                    await event(
                        "tool",
                        20,
                        "tool",
                        {
                            "tool_calls": [tool, prep]
                            if case == "same_batch_preparation"
                            else [tool]
                        },
                        deleted=case == "deleted_tool",
                    )
                    if case == "later_user_same_trace":
                        await event(
                            "next-user",
                            30,
                            "message",
                            {"message": "ordinary later question"},
                            "customer",
                        )
                    elif case == "later_preparation":
                        await event("next-tool", 25, "tool", {"tool_calls": [prep]})
                    elif case == "later_failed_submission":
                        failed = copy.deepcopy(base_tool)
                        failed["result"]["data"] = {"ok": False}
                        await event("next-tool", 25, "tool", {"tool_calls": [failed]})
                    delivered = case.startswith("delivered_")
                    if delivered:
                        await event(
                            "receipt",
                            5 if case == "delivered_previous_turn" else 21,
                            "message",
                            {"message": "receipt fixture"},
                            trace="old" if case == "delivered_other_trace" else "current",
                        )
                    await connection.execute(
                        "INSERT INTO return_outbox VALUES(%s,%s,%s,'s',%s,%s)",
                        (
                            oid,
                            op,
                            rid,
                            "delivered" if delivered else "pending",
                            "receipt" if delivered else None,
                        ),
                    )
                    actual = await outbox.for_submission_trace("s", "current")
                    self.assertEqual(
                        actual,
                        str(oid) if case in ("current_pending", "delivered_current_turn") else None,
                    )
        finally:
            await connection.rollback()
            await connection.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
