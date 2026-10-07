"""Native Store objects supplied directly to SDK Server, with async ownership."""

from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime, timezone
from psycopg import AsyncConnection, IntegrityError
from psycopg.types.json import Jsonb
from parlant.core.common import IdGenerator, generate_id, ItemNotFoundError, UniqueId
from parlant.core.customers import CustomerDocumentStore
from parlant.core.context_variables import ContextVariableDocumentStore
from parlant.core.sessions import SessionDocumentStore, Event, EventId, EventKind, EventSource
from .postgres_documents import PostgresDocumentDatabase
from .business import BusinessError


class PostgresSessionStore(SessionDocumentStore):
    async def append_event_in_transaction(
        self, c, session_id, source, kind, trace_id, data, metadata=None, creation_utc=None
    ):
        row = await (
            await c.execute(
                "UPDATE parlant_sessions SET next_offset=next_offset+1 WHERE id=%s RETURNING doc,next_offset-1 AS event_offset",
                (str(session_id),),
            )
        ).fetchone()
        if not row:
            raise ItemNotFoundError(UniqueId(str(session_id)), message="Session not found")
        event = Event(
            id=EventId(generate_id()),
            source=source,
            kind=kind,
            offset=row["event_offset"],
            creation_utc=creation_utc or datetime.now(timezone.utc),
            trace_id=trace_id,
            data=data,
            metadata=metadata or {},
            deleted=False,
        )
        await c.execute(
            "INSERT INTO parlant_events(id,doc) VALUES (%s,%s)",
            (str(event.id), Jsonb(self._serialize_event(event, session_id))),
        )
        return event, row["doc"]

    async def create_event(
        self, session_id, source, kind, trace_id, data, metadata={}, creation_utc=None
    ):
        # Row-level counter, event INSERT and actual customer confirmation commit
        # atomically. Different coroutine/store objects cannot reuse an offset.
        # Replace only a submission acknowledgement from this processing trace.
        # Ordinary answers and explicit subsequent queries retain native behavior.
        if (
            source == EventSource.AI_AGENT
            and kind == EventKind.MESSAGE
            and not metadata.get("deterministic_operation_receipt")
            and not metadata.get("query_response")
        ):
            from .outbox import Outbox

            outbox = Outbox(self)
            oid = await outbox.for_submission_trace(str(session_id), trace_id)
            if oid:
                return await outbox.deliver(oid, trace_id=trace_id)
        async with self._database.connection() as c:
            event, session_doc = await self.append_event_in_transaction(
                c, session_id, source, kind, trace_id, data, metadata, creation_utc
            )
            document = self._serialize_event(event, session_id)
            if (
                kind == EventKind.MESSAGE
                and source == EventSource.CUSTOMER
                and isinstance(data, dict)
                and data.get("message", "").strip().startswith("确认退货 ")
            ):
                try:
                    await c.execute("SELECT confirm_session_operation(%s)", (str(event.id),))
                except IntegrityError as exc:
                    raise BusinessError(
                        "CONFIRMATION_REJECTED",
                        "确认未保存或未授权：" + str(exc.diag.message_primary),
                    ) from exc
            if kind == EventKind.TOOL:
                await capture_prepared(c, document, session_doc["customer_id"])
        return event

    async def update_event(self, session_id, event_id, params):
        original = await self.read_event(session_id, event_id)
        if original.metadata.get("outbox_id"):
            # A generator's streaming updater must not rewrite a fixed DB receipt.
            return original
        return await super().update_event(session_id, event_id, params)

    async def read_event(self, session_id, event_id):
        await self.read_session(session_id)
        doc = await self._event_collection.find_one(
            {"id": {"$eq": str(event_id)}, "session_id": {"$eq": str(session_id)}}
        )
        if not doc:
            raise ItemNotFoundError(UniqueId(str(event_id)), message="Event not found")
        return self._deserialize_event(doc)

    async def delete_session(self, session_id):
        async with self._database.connection() as c:
            await c.execute(
                "SELECT id FROM parlant_sessions WHERE id=%s FOR UPDATE", (str(session_id),)
            )
            await c.execute("DELETE FROM parlant_events WHERE session_id=%s", (str(session_id),))
            await c.execute("DELETE FROM parlant_sessions WHERE id=%s", (str(session_id),))


async def capture_prepared(c, event, customer_id):
    for call in event.get("data", {}).get("tool_calls", []):
        if not str(call.get("tool_id", "")).endswith(":check_return_eligibility"):
            continue
        result = call.get("result", {}).get("data", {})
        if not isinstance(result, dict) or not result.get("ok"):
            continue
        opid = result.get("data", {}).get("operation_id")
        if not opid:
            continue
        # Bind the returned operation only when it is owned by the session's
        # trusted customer. No session_id is supplied as an MCP tool parameter.
        await c.execute(
            """INSERT INTO session_operations(session_id,operation_id,customer_id,prepared_event_id,last_event_offset)
            SELECT %s,id,customer_id,%s,%s FROM return_operations WHERE id=%s AND customer_id=%s
            ON CONFLICT(operation_id) DO UPDATE SET prepared_event_id=COALESCE(session_operations.prepared_event_id,EXCLUDED.prepared_event_id),last_event_offset=GREATEST(session_operations.last_event_offset,EXCLUDED.last_event_offset)
            WHERE session_operations.session_id=EXCLUDED.session_id""",
            (event["session_id"], event["id"], event["offset"], opid, customer_id),
        )


@asynccontextmanager
async def single_instance(url):
    async with await AsyncConnection.connect(url, connect_timeout=5, autocommit=True) as connection:
        locked = await (await connection.execute("SELECT pg_try_advisory_lock(8810,3)")).fetchone()
        if not locked[0]:
            raise RuntimeError("Only one Parlant retail-demo instance may use this database")
        yield


@asynccontextmanager
async def open_stores(url, exclusive=False):
    async with AsyncExitStack() as stack:
        if exclusive:
            await stack.enter_async_context(single_instance(url))
        databases = {
            name: await stack.enter_async_context(PostgresDocumentDatabase(url, name))
            for name in ("sessions", "customers", "context_variables")
        }
        sessions = await stack.enter_async_context(PostgresSessionStore(databases["sessions"]))
        customers = await stack.enter_async_context(
            CustomerDocumentStore(IdGenerator(), databases["customers"])
        )
        variables = await stack.enter_async_context(
            ContextVariableDocumentStore(IdGenerator(), databases["context_variables"])
        )
        yield sessions, customers, variables
