"""One business layer used by MCP and the trusted local chat confirmation adapter."""

from datetime import datetime, timedelta
from decimal import Decimal
from functools import wraps
from uuid import UUID, uuid4
import psycopg
from psycopg.types.json import Jsonb
from .db import connect


class BusinessError(Exception):
    def __init__(self, code, message, details=None):
        self.code, self.message, self.details = code, message, details or {}


def plain(value):
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    if isinstance(value, (datetime, UUID)):
        return value.isoformat() if isinstance(value, datetime) else str(value)
    return value


def structured(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        try:
            return {"ok": True, "data": plain(fn(*args, **kwargs)), "error": None}
        except BusinessError as e:
            return {
                "ok": False,
                "data": None,
                "error": {"code": e.code, "message": e.message, "details": plain(e.details)},
            }
        except psycopg.IntegrityError:
            return {
                "ok": False,
                "data": None,
                "error": {
                    "code": "INTEGRITY_CONFLICT",
                    "message": "数据库约束拒绝了此操作；请查询现有申请，不要更换操作键重复提交。",
                    "details": {},
                },
            }
        except psycopg.Error:
            return {
                "ok": False,
                "data": None,
                "error": {
                    "code": "DATABASE_UNAVAILABLE",
                    "message": "数据库暂不可用；同次创建重试必须复用原 operation_id。",
                    "details": {},
                },
            }

    return wrapped


def identifier(value):
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise BusinessError("INVALID_ID", "申请/操作编号格式无效。")


def validate_quantity(quantity):
    if type(quantity) is not int or not 1 <= quantity <= 1000:
        raise BusinessError("INVALID_QUANTITY", "数量必须是 1–1000 的整数。")


def validate_reason(reason):
    if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 500:
        raise BusinessError("INVALID_REASON", "退货原因须为 1–500 字的非空文本。")
    return reason.strip()


def confirmation_phrase(op):
    return f"确认退货 {op['id']} 订单{op['order_id']} 明细{op['item_id']} 数量{op['quantity']}"


class RetailService:
    def __init__(self, database_url, customer_id, require_session_binding=False):
        self.database_url, self.customer_id = database_url, customer_id
        self.require_session_binding = require_session_binding

    def _order(self, conn, order_id, lock=False):
        row = conn.execute(
            "SELECT *, now() AS server_now FROM orders WHERE id=%s AND customer_id=%s"
            + (" FOR UPDATE" if lock else ""),
            (order_id, self.customer_id),
        ).fetchone()
        if not row:
            raise BusinessError("ORDER_NOT_FOUND_OR_FORBIDDEN", "订单不存在或不属于当前演示客户。")
        return row

    def _item(self, conn, order_id, item_id, lock=False):
        order = self._order(conn, order_id, lock)
        item = conn.execute(
            "SELECT * FROM order_items WHERE id=%s AND order_id=%s"
            + (" FOR UPDATE" if lock else ""),
            (item_id, order_id),
        ).fetchone()
        if not item:
            raise BusinessError("ITEM_NOT_IN_ORDER", "指定明细不属于该订单。")
        return order, item

    def _eligible(self, conn, order, item, quantity):
        if order["status"] != "delivered":
            raise BusinessError("ORDER_NOT_DELIVERED", "仅已送达订单可申请退货。")
        if order["delivered_at"] > order["server_now"] or order["delivered_at"] < order[
            "server_now"
        ] - timedelta(days=30):
            raise BusinessError("RETURN_WINDOW_EXPIRED", "订单不在送达后 30 天的演示退货窗口内。")
        if not item["returnable"]:
            raise BusinessError("ITEM_NOT_RETURNABLE", "此明细标记为不可退。")
        available = item["quantity"] - item["reserved_return_quantity"]
        if quantity > available:
            raise BusinessError(
                "QUANTITY_EXCEEDS_AVAILABLE",
                "退货数量超过可退数量。",
                {"available_quantity": available},
            )
        existing = conn.execute(
            "SELECT id FROM return_requests WHERE item_id=%s", (item["id"],)
        ).fetchone()
        if existing:
            raise BusinessError(
                "RETURN_ALREADY_PENDING",
                "该明细已有待处理申请；首版不允许重复申请。",
                {"request_id": existing["id"]},
            )
        return available

    def _logs(self, conn, operation_id):
        return conn.execute(
            "SELECT action,details,created_at FROM operation_logs WHERE operation_id=%s AND customer_id=%s ORDER BY id",
            (operation_id, self.customer_id),
        ).fetchall()

    @structured
    def list_my_orders(self):
        with connect(self.database_url) as conn:
            rows = conn.execute(
                "SELECT id,status,created_at,delivered_at,currency FROM orders WHERE customer_id=%s ORDER BY created_at DESC,id",
                (self.customer_id,),
            ).fetchall()
            return {
                "bound_customer_id": self.customer_id,
                "orders": rows,
                "source": "postgresql/synthetic_demo",
            }

    @structured
    def get_order_details(self, order_id):
        with connect(self.database_url) as conn:
            order = self._order(conn, order_id)
            order.pop("server_now")
            items = conn.execute(
                "SELECT *, quantity-reserved_return_quantity AS available_return_quantity FROM order_items WHERE order_id=%s ORDER BY id",
                (order_id,),
            ).fetchall()
            return {
                "order": order,
                "items": items,
                "policy": "仅送达后30天内且returnable=true的明细可退；每条明细最多一份待处理申请。金额仅为申请金额，不代表退款。",
            }

    def issue_tool_context(self, session_id):
        """Called only by the application's trusted native ToolContext adapter."""
        with connect(self.database_url) as conn:
            owned = conn.execute(
                "SELECT id FROM parlant_sessions WHERE id=%s AND customer_id=%s AND agent_id='retail-persistent-demo'",
                (session_id, self.customer_id),
            ).fetchone()
            if not owned:
                raise BusinessError("SESSION_FORBIDDEN", "工具上下文不属于当前会话客户。")
            token = uuid4()
            conn.execute(
                "INSERT INTO tool_session_contexts(id,session_id,customer_id) VALUES (%s,%s,%s)",
                (token, session_id, self.customer_id),
            )
            return str(token)

    @structured
    def check_return_eligibility(self, order_id, item_id, quantity, reason, session_token=None, replaces_operation_id=None):
        validate_quantity(quantity)
        reason = validate_reason(reason)
        with connect(self.database_url) as conn:
            capability = None
            if session_token:
                capability = conn.execute(
                    "SELECT * FROM tool_session_contexts WHERE id=%s AND customer_id=%s AND expires_at>now() FOR UPDATE",
                    (identifier(session_token), self.customer_id),
                ).fetchone()
                if not capability:
                    raise BusinessError("SESSION_CONTEXT_INVALID", "可信会话上下文无效或已过期。")
                if capability["preparation"]:
                    prepared = capability["preparation"]
                    if (order_id, item_id, quantity, reason) != tuple(
                        prepared[k] for k in ("order_id", "item_id", "quantity", "reason")
                    ):
                        raise BusinessError("IDEMPOTENCY_CONFLICT", "同一准备上下文不能修改参数。")
                    if prepared.get("replaces_operation_id") != replaces_operation_id:
                        raise BusinessError("IDEMPOTENCY_CONFLICT", "同一准备上下文不能修改替代关系。")
                    return prepared
            if replaces_operation_id and not capability:
                raise BusinessError("SESSION_CONTEXT_REQUIRED", "修改原操作须有可信会话上下文。")
            if capability:
                # Same lock order as native confirmation and submission: session,
                # then operation/item. Preparation is not authorization.
                conn.execute("SELECT id FROM parlant_sessions WHERE id=%s FOR UPDATE", (capability["session_id"],))
            if replaces_operation_id:
                previous = conn.execute(
                    "SELECT o.* FROM return_operations o JOIN session_operations b ON b.operation_id=o.id WHERE o.id=%s AND b.session_id=%s AND b.customer_id=%s FOR UPDATE OF o",
                    (identifier(replaces_operation_id), capability["session_id"], self.customer_id),
                ).fetchone()
                if not previous:
                    raise BusinessError("SESSION_FORBIDDEN", "不能替代其他会话的操作。")
                if previous["superseded_by"] or conn.execute("SELECT 1 FROM return_requests WHERE operation_id=%s", (previous["id"],)).fetchone():
                    raise BusinessError("OPERATION_NOT_REPLACEABLE", "已提交或已被替代的操作不能再修改。")
            order, item = self._item(conn, order_id, item_id, lock=True)
            available = self._eligible(conn, order, item, quantity)
            op = conn.execute(
                "INSERT INTO return_operations(id,customer_id,order_id,item_id,quantity,reason,quoted_unit_price_cents) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                (
                    uuid4(),
                    self.customer_id,
                    order_id,
                    item_id,
                    quantity,
                    reason,
                    item["unit_price_cents"],
                ),
            ).fetchone()
            conn.execute(
                "INSERT INTO operation_logs(operation_id,customer_id,action,details) VALUES (%s,%s,'prepared',%s)",
                (
                    op["id"],
                    self.customer_id,
                    Jsonb(
                        {
                            "order_id": order_id,
                            "item_id": item_id,
                            "quantity": quantity,
                            "reason": reason,
                        }
                    ),
                ),
            )
            amount = item["unit_price_cents"] * quantity
            result = {
                "eligible": True,
                "replaces_operation_id": replaces_operation_id,
                "operation_id": op["id"],
                "order_id": order_id,
                "item_id": item_id,
                "product_name": item["product_name"],
                "variant": item["variant"],
                "quantity": quantity,
                "available_quantity": available,
                "reason": reason,
                "unit_price_cents": item["unit_price_cents"],
                "amount_cents": amount,
                "amount_display": f"{Decimal(amount) / 100:.2f} CNY",
                "currency": "CNY",
                "confirmation_phrase": confirmation_phrase(op),
                "expires_at": op["expires_at"],
                "status": "awaiting_explicit_confirmation",
                "meaning": "仅准备确认内容，尚未创建申请或退款。",
            }
            if replaces_operation_id:
                conn.execute("UPDATE return_operations SET superseded_by=%s WHERE id=%s", (op["id"], identifier(replaces_operation_id)))
            if capability:
                # A changed draft of the same line replaces its unsubmitted
                # predecessors; a different line is an independent operation.
                conn.execute(
                    "UPDATE return_operations o SET superseded_by=%s FROM session_operations b WHERE o.id=b.operation_id AND b.session_id=%s AND o.order_id=%s AND o.item_id=%s AND o.id<>%s AND o.superseded_by IS NULL AND (o.quantity<>%s OR o.reason<>%s) AND NOT EXISTS(SELECT 1 FROM return_requests r WHERE r.operation_id=o.id)",
                    (op["id"], capability["session_id"], order_id, item_id, op["id"], quantity, reason),
                )
                conn.execute(
                    "INSERT INTO session_operations(session_id,operation_id,customer_id,last_event_offset) SELECT id,%s,customer_id,next_offset-1 FROM parlant_sessions WHERE id=%s AND customer_id=%s",
                    (op["id"], capability["session_id"], self.customer_id),
                )
                conn.execute(
                    "UPDATE tool_session_contexts SET operation_id=%s,preparation=%s WHERE id=%s",
                    (op["id"], Jsonb(plain(result)), capability["id"]),
                )
            return result

    @structured
    def confirm_operation(self, operation_id, text, source):
        """Legacy operator/test helper, not an MCP tool. App authorization uses
        confirm_session_operation on the actual persisted native customer event.
        This stamp alone cannot authorize the app/MCP submission path.
        """
        operation_id = identifier(operation_id)
        with connect(self.database_url) as conn:
            op = conn.execute(
                "SELECT *,now() AS server_now FROM return_operations WHERE id=%s AND customer_id=%s FOR UPDATE",
                (operation_id, self.customer_id),
            ).fetchone()
            if not op:
                raise BusinessError("OPERATION_NOT_FOUND", "操作不存在或不属于当前演示客户。")
            if op["superseded_by"]:
                raise BusinessError("OPERATION_SUPERSEDED", "操作内容已被替代。")
            if text.strip() != confirmation_phrase(op):
                raise BusinessError(
                    "CONFIRMATION_MISMATCH", "请原样复制工具准备的确认口令，包含订单、明细和数量。"
                )
            if op["expires_at"] < op["server_now"]:
                raise BusinessError(
                    "OPERATION_EXPIRED", "确认已过期，请重新检查资格并确认新的内容。"
                )
            if not op["confirmed_at"]:
                conn.execute(
                    "UPDATE return_operations SET confirmed_at=now(),confirmation_source=%s WHERE id=%s",
                    (source, operation_id),
                )
                conn.execute(
                    "INSERT INTO operation_logs(operation_id,customer_id,action,details) VALUES (%s,%s,'confirmed',%s)",
                    (
                        operation_id,
                        self.customer_id,
                        Jsonb({"source": source, "confirmation_text": text.strip()}),
                    ),
                )
            return {"operation_id": operation_id, "confirmed": True}

    def _submission_operation(self, conn, operation_id, session_token=None):
        # Serialize changed preparations, actual confirmations and first submit.
        capability = None
        if session_token:
            capability = conn.execute(
                "SELECT * FROM tool_session_contexts WHERE id=%s AND customer_id=%s AND expires_at>now()",
                (identifier(session_token), self.customer_id),
            ).fetchone()
            if not capability:
                raise BusinessError("SESSION_CONTEXT_INVALID", "可信会话上下文无效或已过期。")
        binding = conn.execute(
            "SELECT * FROM session_operations WHERE operation_id=%s AND customer_id=%s",
            (operation_id, self.customer_id),
        ).fetchone()
        if capability and (not binding or binding["session_id"] != capability["session_id"]):
            raise BusinessError("SESSION_FORBIDDEN", "操作不属于当前可信会话。")
        if binding:
            owned = conn.execute(
                "SELECT id FROM parlant_sessions WHERE id=%s AND customer_id=%s AND agent_id='retail-persistent-demo' FOR UPDATE",
                (binding["session_id"], self.customer_id),
            ).fetchone()
            if not owned:
                raise BusinessError("SESSION_FORBIDDEN", "操作的原会话不存在或不属于当前客户。")
        if binding:
            binding = conn.execute("SELECT * FROM session_operations WHERE operation_id=%s", (operation_id,)).fetchone()
        op = conn.execute(
            "SELECT *,now() AS server_now FROM return_operations WHERE id=%s AND customer_id=%s FOR UPDATE",
            (operation_id, self.customer_id),
        ).fetchone()
        if not op:
            raise BusinessError("OPERATION_NOT_FOUND", "请使用后端准备的原 operation_id。")
        return op, binding

    @structured
    def submit_confirmed_return(self, operation_id, session_token=None):
        """Submit the immutable server snapshot; an operation reference is not authorization."""
        operation_id = identifier(operation_id)
        if not session_token:
            raise BusinessError("SESSION_CONTEXT_REQUIRED", "提交须由服务端可信会话上下文发起。")
        with connect(self.database_url) as conn:
            op, binding = self._submission_operation(conn, operation_id, session_token)
            return self._submit_snapshot(conn, op, binding)

    @structured
    def create_return_request(self, operation_id, order_id, item_id, quantity, reason, session_token=None):
        """Strict legacy business interface; not registered as a model/MCP tool."""
        validate_quantity(quantity)
        reason = validate_reason(reason)
        operation_id = identifier(operation_id)
        with connect(self.database_url) as conn:
            op, binding = self._submission_operation(conn, operation_id, session_token)
            if (order_id, item_id, quantity, reason) != (op["order_id"], op["item_id"], op["quantity"], op["reason"]):
                raise BusinessError("IDEMPOTENCY_CONFLICT", "同一操作键不能用于不同订单、明细、数量或原因。")
            return self._submit_snapshot(conn, op, binding)

    def _submit_snapshot(self, conn, op, binding):
        operation_id = op["id"]
        order_id, item_id, quantity, reason = (op[k] for k in ("order_id", "item_id", "quantity", "reason"))
        old = conn.execute("SELECT * FROM return_requests WHERE operation_id=%s", (operation_id,)).fetchone()
        if old:
            return {"request": old, "operation_logs": self._logs(conn, operation_id), "idempotent_replay": True, "meaning": "退货申请已提交；不表示退款到账。"}
        if op["superseded_by"]:
            raise BusinessError("OPERATION_SUPERSEDED", "该未提交内容已被重新准备的内容替代，须展示并确认新操作。")
        if self.require_session_binding or binding:
            ev = (binding or {}).get("confirmation_event") or {}
            if not (
                binding and binding["confirmation_event_id"]
                and ev.get("id") == binding["confirmation_event_id"]
                and ev.get("source") == "customer" and ev.get("kind") == "message"
                and ev.get("session_id") == binding["session_id"]
                and (ev.get("data") or {}).get("message", "").strip() == confirmation_phrase(op)
                and op["confirmation_source"] == "parlant-human-message:" + binding["session_id"]
            ):
                raise BusinessError("CONFIRMATION_EVENT_REQUIRED", "缺少与原参数绑定的真实用户确认事件，不能提交。")
        if not op["confirmed_at"]:
            raise BusinessError(
                "CONFIRMATION_REQUIRED", "尚未收到用户的准确确认口令；不能创建申请。"
            )
        if op["expires_at"] < op["server_now"]:
            raise BusinessError("OPERATION_EXPIRED", "操作已过期，请重新检查并确认。")
        order, item = self._item(conn, order_id, item_id, lock=True)
        self._eligible(conn, order, item, quantity)
        if item["unit_price_cents"] != op["quoted_unit_price_cents"]:
            raise BusinessError("ORDER_FACTS_CHANGED", "价格事实已变化，请重新检查并确认。")
        row = conn.execute(
            "INSERT INTO return_requests(id,operation_id,customer_id,order_id,item_id,quantity,reason,amount_cents) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
            (
                uuid4(),
                operation_id,
                self.customer_id,
                order_id,
                item_id,
                quantity,
                reason,
                item["unit_price_cents"] * quantity,
            ),
        ).fetchone()
        conn.execute(
            "INSERT INTO operation_logs(operation_id,customer_id,action,details) VALUES (%s,%s,'submitted',%s)",
            (
                operation_id,
                self.customer_id,
                Jsonb(
                    {
                        "request_id": str(row["id"]),
                        "quantity": quantity,
                        "amount_cents": row["amount_cents"],
                    }
                ),
            ),
        )
        return {
            "request": row,
            "operation_logs": self._logs(conn, operation_id),
            "idempotent_replay": False,
            "meaning": "退货申请已提交；不表示退款到账。",
        }

    @structured
    def get_return_request(self, request_id):
        with connect(self.database_url) as conn:
            row = conn.execute(
                "SELECT * FROM return_requests WHERE id=%s AND customer_id=%s",
                (identifier(request_id), self.customer_id),
            ).fetchone()
            if not row:
                raise BusinessError(
                    "REQUEST_NOT_FOUND_OR_FORBIDDEN", "申请不存在或不属于当前演示客户。"
                )
            return {
                "request": row,
                "operation_logs": self._logs(conn, row["operation_id"]),
                "meaning": "仅显示申请状态，不表示退款到账。",
            }

    @structured
    def get_operation_result(self, operation_id):
        """Read-only recovery, even after the original confirmation expires."""
        with connect(self.database_url) as conn:
            op = conn.execute(
                "SELECT *,now() AS server_now FROM return_operations WHERE id=%s AND customer_id=%s",
                (identifier(operation_id), self.customer_id),
            ).fetchone()
            if not op:
                raise BusinessError("OPERATION_NOT_FOUND", "操作不存在或不属于当前演示客户。")
            request = conn.execute(
                "SELECT * FROM return_requests WHERE operation_id=%s AND customer_id=%s",
                (op["id"], self.customer_id),
            ).fetchone()
            binding = conn.execute(
                "SELECT session_id,confirmation_event_id FROM session_operations WHERE operation_id=%s AND customer_id=%s",
                (op["id"], self.customer_id),
            ).fetchone()
            expired = op["expires_at"] < op["server_now"]
            state = (
                "submitted"
                if request
                else (
                    "superseded_not_submitted"
                    if op["superseded_by"]
                    else "expired_not_submitted"
                    if expired
                    else (
                        "confirmed_not_submitted" if op["confirmed_at"] else "awaiting_confirmation"
                    )
                )
            )
            return {
                "operation_id": op["id"],
                "state": state,
                "request": request,
                "confirmed": bool(op["confirmed_at"]),
                "expired": expired,
                "retry_allowed": not request
                and not op["superseded_by"]
                and not expired
                and bool(op["confirmed_at"])
                and bool(binding and binding["confirmation_event_id"]),
                "original_parameters": {
                    "operation_id": str(op["id"]),
                    "order_id": op["order_id"],
                    "item_id": op["item_id"],
                    "quantity": op["quantity"],
                    "reason": op["reason"],
                },
                "binding": binding,
                "operation_logs": self._logs(conn, op["id"]),
                "meaning": "submitted仅表示申请提交，尚未退款。",
            }

    @structured
    def session_operation(self, session_id, operation_id=None):
        with connect(self.database_url) as conn:
            owned = conn.execute(
                "SELECT id FROM parlant_sessions WHERE id=%s AND customer_id=%s",
                (session_id, self.customer_id),
            ).fetchone()
            if not owned:
                raise BusinessError(
                    "SESSION_NOT_FOUND_OR_FORBIDDEN", "会话不存在或不属于当前演示客户。"
                )
            row = conn.execute(
                "SELECT operation_id FROM session_operations WHERE session_id=%s AND customer_id=%s"
                + (
                    " AND operation_id=%s"
                    if operation_id
                    else " AND operation_id=(SELECT operation_id FROM session_current_operations WHERE session_id=%s)"
                )
                + " ORDER BY last_event_offset DESC,operation_id LIMIT 1",
                (session_id, self.customer_id, identifier(operation_id))
                if operation_id
                else (session_id, self.customer_id, session_id),
            ).fetchone()
            if not row:
                raise BusinessError(
                    "NO_SESSION_OPERATION", "此会话没有可核对的原退货操作，不能自动创建新操作。"
                )
            return {"operation_id": row["operation_id"]}
