"""Transactional native receipts and bounded single-instance delivery worker.

The SDK's PollingSessionListener observes committed native events directly;
there is no second emitter INSERT, processing dispatch, or model call here.
"""

import argparse
import asyncio
import logging
import os
from pathlib import Path
from uuid import UUID
from parlant.core.sessions import SessionId, EventKind, EventSource
from .business import plain
from .db import connect
from .recovery import receipt
from .settings import load_settings

LOG = logging.getLogger(__name__)


class Outbox:
    def __init__(self, store):
        self.store = store

    async def for_operation(self, operation_id):
        async with self.store._database.connection() as c:
            row = await (
                await c.execute(
                    "SELECT id FROM return_outbox WHERE operation_id=%s", (operation_id,)
                )
            ).fetchone()
            return str(row["id"]) if row else None

    async def for_submission_trace(self, sid, trace_id):
        async with self.store._database.connection() as c:
            # A trace alone is not a turn boundary. Resume/plugin callers can
            # reuse one; never select a submission before the latest user message.
            boundary = (
                await (
                    await c.execute(
                        "SELECT COALESCE(max(event_offset),-1) AS last_user_offset FROM parlant_events WHERE session_id=%s AND event_kind='message' AND event_source IN ('customer','customer_ui') AND NOT deleted",
                        (sid,),
                    )
                ).fetchone()
            )["last_user_offset"]
            events = await (
                await c.execute(
                    "SELECT doc FROM parlant_events WHERE session_id=%s AND trace_id=%s AND event_kind='tool' AND NOT deleted AND event_offset>%s ORDER BY event_offset DESC",
                    (sid, trace_id, boundary),
                )
            ).fetchall()
            for event in events:
                calls = event["doc"].get("data", {}).get("tool_calls", [])
                # A later (or same-batch) preparation needs its own ordinary
                # offer; the worker can deliver the earlier submission separately.
                if any(
                    tool.get("tool_id") == "retail-demo-business:check_return_eligibility"
                    and tool.get("result", {}).get("data", {}).get("ok") is True
                    for tool in calls
                ):
                    return None
                for tool in reversed(calls):
                    if tool.get("tool_id") not in ("retail-demo-business:create_return_request", "retail-demo-business:submit_confirmed_return"):
                        continue
                    result = tool.get("result", {}).get("data", {})
                    if result.get("ok") is not True:
                        return None
                    request = (result.get("data") or {}).get("request") or {}
                    try:
                        op = UUID(str(tool.get("arguments", {}).get("operation_id")))
                        request_id = UUID(str(request.get("id")))
                        if UUID(str(request.get("operation_id"))) != op:
                            return None
                    except (ValueError, TypeError):
                        return None
                    row = await (
                        await c.execute(
                            "SELECT o.id FROM return_outbox o LEFT JOIN parlant_events receipt ON receipt.id=o.receipt_event_id WHERE o.operation_id=%s AND o.request_id=%s AND o.session_id=%s AND (o.status<>'delivered' OR (receipt.trace_id=%s AND receipt.event_offset>%s AND NOT receipt.deleted))",
                            (op, request_id, sid, trace_id, boundary),
                        )
                    ).fetchone()
                    return str(row["id"]) if row else None
            return None

    async def deliver(self, outbox_id, trace_id=None):
        try:
            async with self.store._database.connection() as c:
                row = await (
                    await c.execute(
                        "SELECT * FROM return_outbox WHERE id=%s FOR UPDATE", (outbox_id,)
                    )
                ).fetchone()
                if not row:
                    raise ValueError("Outbox not found")
                if row["status"] == "delivered":
                    event = await (
                        await c.execute(
                            "SELECT doc FROM parlant_events WHERE id=%s", (row["receipt_event_id"],)
                        )
                    ).fetchone()
                    if not event:
                        raise ValueError(
                            "Delivered receipt was explicitly deleted; no automatic replacement"
                        )
                    return self.store._deserialize_event(event["doc"])
                if row["status"] == "failed":
                    raise ValueError(
                        "Notification exhausted retries; explicit manual retry required"
                    )
                if (
                    row["next_attempt_at"]
                    > (await (await c.execute("SELECT now() AS t")).fetchone())["t"]
                ):
                    raise ValueError("Notification is waiting for backoff")
                facts = await (
                    await c.execute(
                        "SELECT r.*,o.id AS op_id FROM return_requests r JOIN return_operations o ON o.id=r.operation_id JOIN session_operations b ON b.operation_id=o.id AND b.request_id=r.id JOIN parlant_sessions s ON s.id=b.session_id WHERE r.id=%s AND r.operation_id=%s AND b.session_id=%s AND s.customer_id=%s AND b.customer_id=%s",
                        (
                            row["request_id"],
                            row["operation_id"],
                            row["session_id"],
                            row["customer_id"],
                            row["customer_id"],
                        ),
                    )
                ).fetchone()
                if not facts:
                    raise ValueError("Owned original session/request association unavailable")
                result = {
                    "ok": True,
                    "data": {
                        "operation_id": str(row["operation_id"]),
                        "request": plain(facts),
                        "original_parameters": plain(
                            {k: facts[k] for k in ("order_id", "item_id", "quantity", "reason")}
                        ),
                    },
                }
                event, _ = await self.store.append_event_in_transaction(
                    c,
                    SessionId(row["session_id"]),
                    EventSource.AI_AGENT,
                    EventKind.MESSAGE,
                    trace_id or "outbox:" + str(row["id"]),
                    {
                        "message": receipt(result),
                        "participant": {
                            "id": "retail-persistent-demo",
                            "display_name": "持久化订单与退货演示客服",
                        },
                    },
                    {
                        "deterministic_operation_receipt": True,
                        "outbox_id": str(row["id"]),
                        "operation_id": str(row["operation_id"]),
                        "request_id": str(row["request_id"]),
                        "notification_type": row["notification_type"],
                    },
                )
                await c.execute(
                    "UPDATE return_outbox SET status='delivered',receipt_event_id=%s,completed_at=now(),attempts=attempts+1,total_attempts=total_attempts+1 WHERE id=%s",
                    (str(event.id), outbox_id),
                )
            # Post-commit crash hook; never controlled by tool/model arguments.
            marker = os.environ.get("DEMO_TEST_OUTBOX_AFTER_COMMIT")
            if marker and Path(marker).exists() and Path(marker).read_text().strip() == outbox_id:
                Path(marker).unlink()
                os._exit(78)
            return event
        except ValueError:
            raise
        except Exception as exc:
            await self.record_failure(outbox_id, exc)
            raise

    async def record_failure(self, outbox_id, error):
        # The failed delivery rolled back counter/event/delivery status together.
        # Persist failure separately; a crash before this leaves pending, safe to retry.
        detail = getattr(getattr(error, "diag", None), "message_primary", None)
        if not detail and isinstance(error, ValueError):
            detail = str(error)  # App-owned validation text, never a connection DSN.
        message = type(error).__name__ + ": " + (detail or "delivery transaction failed")
        async with self.store._database.connection() as c:
            changed = await (
                await c.execute(
                    "UPDATE return_outbox SET attempts=attempts+1,total_attempts=total_attempts+1,last_error=%s,next_attempt_at=now()+make_interval(secs=>LEAST(60,5*power(2,attempts))::int),status=CASE WHEN attempts+1>=max_attempts THEN 'failed' ELSE 'pending' END WHERE id=%s AND status='pending' RETURNING id",
                    (message[:1024], outbox_id),
                )
            ).fetchone()
            if changed:
                await c.execute(
                    "INSERT INTO return_outbox_errors(outbox_id,error) VALUES (%s,%s)",
                    (outbox_id, message[:1024]),
                )

    async def pending(self):
        async with self.store._database.connection() as c:
            if not (await (await c.execute("SELECT enabled FROM outbox_control")).fetchone())[
                "enabled"
            ]:
                return []
            return await (
                await c.execute(
                    "SELECT id,session_id FROM return_outbox WHERE status='pending' AND next_attempt_at<=now() ORDER BY next_attempt_at,id LIMIT 20"
                )
            ).fetchall()


class OutboxWorker:
    def __init__(self, store, coordinator):
        self.outbox = Outbox(store)
        self.coordinator = coordinator
        self.task = None

    def start(self):
        self.task = asyncio.create_task(self.run(), name="retail-outbox")

    async def stop(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

    async def run(self):
        while True:
            try:
                for row in await self.outbox.pending():
                    sid = row["session_id"]
                    gate = self.coordinator.locks[sid]
                    if gate.locked() or self.coordinator.processing(sid):
                        continue
                    async with gate:
                        if not self.coordinator.processing(sid):
                            try:
                                await self.outbox.deliver(str(row["id"]))
                            except ValueError as exc:
                                await self.outbox.record_failure(str(row["id"]), exc)
                            except Exception:
                                LOG.exception("Outbox delivery failed: %s", row["id"])
            except Exception:
                LOG.exception("Outbox polling failed; PostgreSQL required")
            await asyncio.sleep(1)


def main():
    parser = argparse.ArgumentParser(
        description="Worker follows Parlant lifecycle; commands control its persisted pause state."
    )
    parser.add_argument("command", choices=("status", "pause", "resume", "retry"))
    parser.add_argument("--id", help="Failed outbox UUID for explicit retry")
    args = parser.parse_args()
    with connect(load_settings().database_url) as c:
        if args.command in ("pause", "resume"):
            c.execute("UPDATE outbox_control SET enabled=%s", (args.command == "resume",))
        elif args.command == "retry":
            if not args.id:
                parser.error("retry requires --id")
            row = c.execute(
                "UPDATE return_outbox SET status='pending',attempts=0,next_attempt_at=now(),manual_retries=manual_retries+1 WHERE id=%s AND status='failed' RETURNING id",
                (args.id,),
            ).fetchone()
            if not row:
                raise SystemExit("Not found or not failed; delivered receipts cannot be requeued.")
        state = c.execute("SELECT enabled FROM outbox_control").fetchone()
        counts = c.execute(
            "SELECT status,count(*) AS count FROM return_outbox GROUP BY status ORDER BY status"
        ).fetchall()
        rows = c.execute(
            "SELECT id,operation_id,request_id,session_id,status,attempts,total_attempts,max_attempts,next_attempt_at,last_error,receipt_event_id,completed_at,manual_retries FROM return_outbox WHERE status<>'delivered' ORDER BY created_at LIMIT 50"
        ).fetchall()
        import json

        print(
            json.dumps(
                plain(
                    {
                        "worker_enabled": state["enabled"],
                        "counts": counts,
                        "backlog_and_failures": rows,
                    }
                ),
                ensure_ascii=False,
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
