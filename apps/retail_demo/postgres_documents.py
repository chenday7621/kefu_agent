"""PostgreSQL implementation of Parlant's DocumentDatabase extension interface.

Native document stores keep SDK serialization, metadata, pagination and filters.
All I/O is psycopg async, committed before return, without any local fallback.
"""

from contextlib import asynccontextmanager
import hashlib
from psycopg import AsyncConnection, sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from parlant.core.persistence.common import Cursor, ObjectId, SortDirection
from parlant.core.persistence.document_database import (
    DocumentDatabase,
    DocumentCollection,
    FindResult,
    InsertResult,
    UpdateResult,
    DeleteResult,
    identity_loader,
)

TABLES = {
    "sessions": {
        "sessions": "parlant_sessions",
        "events": "parlant_events",
        "metadata": "parlant_session_metadata",
    },
    "customers": {
        "customers": "parlant_customers",
        "customer_tag_associations": "parlant_customer_tags",
        "metadata": "parlant_customer_metadata",
    },
    "context_variables": {
        "variables": "parlant_variables",
        "variable_tag_associations": "parlant_variable_tags",
        "values": "parlant_variable_values",
        "metadata": "parlant_variable_metadata",
    },
}


def field_expr(table, name, native=False):
    generated = {
        "parlant_sessions": {
            "customer_id": "customer_id",
            "agent_id": "agent_id",
            "creation_utc": "creation_utc",
        },
        "parlant_events": {
            "session_id": "session_id",
            "offset": "event_offset",
            "kind": "event_kind",
            "source": "event_source",
            "trace_id": "trace_id",
            "deleted": "deleted",
        },
    }
    if name == "id":
        return sql.Identifier("id") if native else sql.SQL("to_jsonb(id)")
    if name in generated.get(table, {}):
        return (
            sql.Identifier(generated[table][name])
            if native
            else sql.SQL("to_jsonb({})").format(sql.Identifier(generated[table][name]))
        )
    return sql.SQL("(doc -> {})").format(sql.Literal(name))


def where_sql(table, filters):
    parts = []
    params = []
    for field, conditions in filters.items():
        if field in ("$and", "$or"):
            nested = [where_sql(table, x) for x in conditions]
            parts.append(
                sql.SQL("({})").format(
                    sql.SQL(" AND " if field == "$and" else " OR ").join([x[0] for x in nested])
                )
                if nested
                else sql.SQL("TRUE" if field == "$and" else "FALSE")
            )
            for _, values in nested:
                params.extend(values)
            continue
        expr = field_expr(table, field, native=True)
        typed = isinstance(expr, sql.Identifier)
        placeholder = sql.SQL("%s" if typed else "%s::jsonb")
        for operator, value in conditions.items():
            if operator in ("$in", "$nin"):
                if not value:
                    parts.append(sql.SQL("FALSE" if operator == "$in" else "TRUE"))
                    continue
                clause = sql.SQL("{} {} ({})").format(
                    expr,
                    sql.SQL("IN" if operator == "$in" else "NOT IN"),
                    sql.SQL(",").join([placeholder] * len(value)),
                )
                params.extend(v if typed else Jsonb(v) for v in value)
            else:
                comparisons = {
                    "$eq": "=",
                    "$ne": "<>",
                    "$gt": ">",
                    "$gte": ">=",
                    "$lt": "<",
                    "$lte": "<=",
                }
                if operator not in comparisons:
                    raise ValueError("Unsupported native filter operator")
                clause = sql.SQL("{} {} {}").format(
                    expr, sql.SQL(comparisons[operator]), placeholder
                )
                params.append(value if typed else Jsonb(value))
            parts.append(clause)
    return sql.SQL(" AND ").join(parts) if parts else sql.SQL("TRUE"), params


class PostgresDocumentDatabase(DocumentDatabase):
    def __init__(self, url, namespace):
        self.url = url
        self.namespace = namespace
        self.active = False

    async def __aenter__(self):
        self.active = True
        try:
            async with self.connection() as c:
                await c.execute("SELECT 1")
        except BaseException:
            self.active = False
            raise
        return self

    async def __aexit__(self, *args):
        self.active = False

    @asynccontextmanager
    async def connection(self):
        if not self.active:
            raise RuntimeError("PostgreSQL document database lifecycle is closed")
        async with await AsyncConnection.connect(
            self.url,
            row_factory=dict_row,
            connect_timeout=5,
            options="-c search_path=retail_demo,public -c timezone=UTC",
        ) as c:
            yield c

    async def create_collection(self, name, schema):
        return await self.get_or_create_collection(name, schema, identity_loader)

    async def get_collection(self, name, schema, document_loader):
        return await self.get_or_create_collection(name, schema, document_loader)

    async def get_or_create_collection(self, name, schema, document_loader):
        table = TABLES[self.namespace][name]  # closed allow-list, never model SQL identifiers
        async with self.connection() as c:
            r = await (
                await c.execute("SELECT to_regclass(%s) AS table_name", (f"retail_demo.{table}",))
            ).fetchone()
            if not r["table_name"]:
                raise RuntimeError("Run retail_demo.db setup before opening native stores")
        return PostgresCollection(self, table, document_loader)

    async def delete_collection(self, name):
        table = TABLES[self.namespace][name]
        async with self.connection() as c:
            await c.execute(sql.SQL("DELETE FROM {}").format(sql.Identifier(table)))


class PostgresCollection(DocumentCollection):
    def __init__(self, database, table, loader):
        self.database = database
        self.table = table
        self.loader = loader

    async def load(self, doc):
        loaded = await self.loader(doc)
        if loaded is None:
            raise RuntimeError("Native document loader rejected a document; refusing silent loss")
        if loaded != doc:
            # The native customer store itself creates v0.1 tag associations,
            # then upgrades them to v0.2 in its supplied loader. Honor native
            # conversions, persisting once with a compare-and-swap so an older
            # read cannot overwrite a concurrent document update.
            async with self.database.connection() as c:
                row = await (
                    await c.execute(
                        sql.SQL("UPDATE {} SET doc=%s WHERE id=%s AND doc=%s RETURNING doc").format(
                            sql.Identifier(self.table)
                        ),
                        (Jsonb(loaded), str(doc["id"]), Jsonb(doc)),
                    )
                ).fetchone()
            if not row:
                return await self.find_one({"id": {"$eq": str(doc["id"])}})
        return loaded

    async def find(self, filters, limit=None, cursor=None, sort_direction=None):
        clause, params = where_sql(self.table, filters)
        direction = sort_direction or SortDirection.ASC
        order = sql.SQL(" DESC" if direction == SortDirection.DESC else " ASC")
        created = sql.SQL(
            "creation_utc"
            if self.table == "parlant_sessions"
            else "COALESCE(doc->>'creation_utc','')"
        )
        if cursor:
            clause = sql.SQL("({}) AND ({},id) {} (%s,%s)").format(
                clause, created, sql.SQL("<" if direction == SortDirection.DESC else ">")
            )
            params.extend([cursor.creation_utc, str(cursor.id)])
        table = sql.Identifier(self.table)
        # Event retrieval uses offset, including imported out-of-time-order events.
        sort = (
            sql.SQL("event_offset ASC")
            if self.table == "parlant_events"
            else sql.SQL("{}{},id{}").format(created, order, order)
        )
        async with self.database.connection() as c:
            count = (
                await (
                    await c.execute(
                        sql.SQL("SELECT count(*) AS n FROM {} WHERE {}").format(table, clause),
                        params,
                    )
                ).fetchone()
            )["n"]
            query = sql.SQL("SELECT doc FROM {} WHERE {} ORDER BY {}").format(table, clause, sort)
            if limit is not None:
                query += sql.SQL(" LIMIT %s")
                params.append(limit)
            rows = await (await c.execute(query, params)).fetchall()
        docs = [await self.load(row["doc"]) for row in rows]
        more = limit is not None and count > len(docs)
        next_cursor = (
            Cursor(str(docs[-1].get("creation_utc", "")), ObjectId(str(docs[-1]["id"])))
            if more and docs
            else None
        )
        return FindResult(docs, count, more, next_cursor)

    async def find_one(self, filters, sort=None):
        clause, params = where_sql(self.table, filters)
        order = (
            sql.SQL(",").join(
                [
                    sql.SQL("{} {}").format(
                        field_expr(self.table, name),
                        sql.SQL("DESC" if direction == SortDirection.DESC else "ASC"),
                    )
                    for name, direction in sort
                ]
            )
            if sort
            else sql.SQL("id ASC")
        )
        async with self.database.connection() as c:
            row = await (
                await c.execute(
                    sql.SQL("SELECT doc FROM {} WHERE {} ORDER BY {} LIMIT 1").format(
                        sql.Identifier(self.table), clause, order
                    ),
                    params,
                )
            ).fetchone()
        return await self.load(row["doc"]) if row else None

    async def ensure_indexes(self, indexes):
        for index in indexes:
            expressions = sql.SQL(",").join(
                [
                    sql.SQL("({}) {}").format(
                        field_expr(self.table, name, native=True),
                        sql.SQL("DESC" if direction == SortDirection.DESC else "ASC"),
                    )
                    for name, direction in index.fields
                ]
            )
            signature = repr((self.table, index.fields, index.unique)).encode()
            name = "parlant_idx_" + hashlib.sha256(signature).hexdigest()[:18]
            async with self.database.connection() as c:
                await c.execute(
                    sql.SQL("CREATE {} INDEX IF NOT EXISTS {} ON {} ({})").format(
                        sql.SQL("UNIQUE" if index.unique else ""),
                        sql.Identifier(name),
                        sql.Identifier(self.table),
                        expressions,
                    )
                )

    async def insert_one(self, document):
        async with self.database.connection() as c:
            await c.execute(
                sql.SQL("INSERT INTO {}(id,doc) VALUES (%s,%s)").format(sql.Identifier(self.table)),
                (str(document["id"]), Jsonb(document)),
            )
        return InsertResult(True)

    async def update_one(self, filters, params, upsert=False):
        clause, values = where_sql(self.table, filters)
        async with self.database.connection() as c:
            row = await (
                await c.execute(
                    sql.SQL("SELECT id,doc FROM {} WHERE {} ORDER BY id LIMIT 1 FOR UPDATE").format(
                        sql.Identifier(self.table), clause
                    ),
                    values,
                )
            ).fetchone()
            if row:
                updated = {**row["doc"], **params}
                await c.execute(
                    sql.SQL("UPDATE {} SET doc=%s WHERE id=%s").format(sql.Identifier(self.table)),
                    (Jsonb(updated), row["id"]),
                )
                return UpdateResult(True, 1, int(updated != row["doc"]), updated)
            if upsert:
                await c.execute(
                    sql.SQL("INSERT INTO {}(id,doc) VALUES (%s,%s)").format(
                        sql.Identifier(self.table)
                    ),
                    (str(params["id"]), Jsonb(params)),
                )
                return UpdateResult(True, 0, 1, params)
        return UpdateResult(True, 0, 0, None)

    async def delete_one(self, filters):
        clause, params = where_sql(self.table, filters)
        async with self.database.connection() as c:
            row = await (
                await c.execute(
                    sql.SQL(
                        "DELETE FROM {} WHERE id=(SELECT id FROM {} WHERE {} ORDER BY id LIMIT 1) RETURNING doc"
                    ).format(sql.Identifier(self.table), sql.Identifier(self.table), clause),
                    params,
                )
            ).fetchone()
        return DeleteResult(True, int(row is not None), row["doc"] if row else None)
