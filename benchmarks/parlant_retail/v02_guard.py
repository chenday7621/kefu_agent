"""V0.2 Retail execution boundary. Only official tool results can establish facts.

This is an application policy gate, not a replacement for Parlant's decisions or
for the official tools' eligibility checks. Audit records contain hashes/IDs,
never authentication text or payment details.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
import threading
from typing import Any

from observe import argument_digest, normalized


# All 16 public tools. Kept in one registry for registration, execution and audit.
# `auth` means that the public policy requires official identity lookup first.
TOOL_POLICY = {
    "find_user_id_by_email": ("authentication", False),
    "find_user_id_by_name_zip": ("authentication", False),
    "get_user_details": ("account_read", True),
    "get_order_details": ("order_read", True),
    "get_product_details": ("catalog_read", False),
    "get_item_details": ("catalog_read", False),
    "list_all_product_types": ("catalog_read", False),
    "calculate": ("calculation", False),
    "cancel_pending_order": ("order_write", True),
    "exchange_delivered_order_items": ("order_write", True),
    "modify_pending_order_address": ("order_write", True),
    "modify_pending_order_items": ("order_write", True),
    "modify_pending_order_payment": ("order_write", True),
    "modify_user_address": ("profile_write", True),
    "return_delivered_order_items": ("order_write", True),
    "transfer_to_human_agents": ("transfer", False),
}
WRITE_TOOLS = frozenset(k for k, (kind, _) in TOOL_POLICY.items() if kind.endswith("write") or kind == "transfer")
DB_WRITE_TOOLS = WRITE_TOOLS - {"transfer_to_human_agents"}

ACTION_TERMS = {
    "cancel_pending_order": (r"\bcancel\b",),
    "exchange_delivered_order_items": (r"\bexchang(?:e|ing)\b", r"\bswap\b"),
    "modify_pending_order_items": (r"\bchang(?:e|ing)\b.{0,100}\b(?:item|product|shirt|option)", r"\bmodif(?:y|ication)\b.{0,100}\bitem"),
    "modify_pending_order_payment": (r"\bchang(?:e|ing|ed)\b\s+(?:the\s+)?(?:order(?:'s)?\s+)?payment method", r"\bswitch\b\s+(?:the\s+)?(?:order(?:'s)?\s+)?payment method"),
    "modify_pending_order_address": (r"\b(?:chang(?:e|ing)|updat(?:e|ing))\b.{0,90}\b(?:shipping|order|delivery) address",),
    "modify_user_address": (r"\b(?:chang(?:e|ing)|updat(?:e|ing))\b.{0,90}\b(?:default|profile|account) address",),
    "return_delivered_order_items": (r"\breturn\b", r"\brefund\b.{0,70}\bitem"),
    "transfer_to_human_agents": (r"\btransfer\b", r"\bhuman agent\b"),
}
NEGATIVE_OR_CONDITIONAL = re.compile(r"\b(?:no|not|don't|do not|only if|if|unless|provided that|but|maybe|perhaps|not yet|wait|hold off|first|before|instead|rather)\b", re.I)
AFFIRMATIVE = re.compile(r"^(?:yes|yep|correct|confirmed|please proceed|go ahead|proceed|i confirm|sure|that's correct)\b", re.I)
ORDER_ID = re.compile(r"#W\d+", re.I)


def fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def has_placeholder(value: Any) -> bool:
    if isinstance(value, str):
        return "<<__missing__>>" in value
    if isinstance(value, dict):
        return any(has_placeholder(v) for v in value.values())
    if isinstance(value, list):
        return not value or any(has_placeholder(v) for v in value)
    return value is None


def derive_product_fact(data: Any, official_call_id: str) -> dict[str, Any]:
    """Count only a complete, returned product's variants; never use the DB."""
    fact = {"source_official_call_id": official_call_id, "product_id": None,
            "product_name": None, "variants_total": None, "available_true": None,
            "complete": False, "reason": None}
    if not isinstance(data, dict) or not isinstance(data.get("product_id"), str):
        fact["reason"] = "missing_product_id"
        return fact
    fact["product_id"], fact["product_name"] = data["product_id"], data.get("name")
    variants = data.get("variants")
    if not isinstance(variants, dict):
        fact["reason"] = "missing_or_invalid_variants"
        return fact
    fact["variants_total"] = len(variants)
    if any(not isinstance(v, dict) or type(v.get("available")) is not bool for v in variants.values()):
        fact["reason"] = "incomplete_available_fields"
        return fact
    fact["available_true"] = sum(v["available"] for v in variants.values())
    fact["complete"] = True
    return fact


@dataclass
class Proposal:
    action: str
    text_sha256: str
    user_turn: int
    order_ids: set[str]
    item_ids: set[str]
    payment_ids: set[str]
    amounts: set[str]
    text: str = field(repr=False)


@dataclass
class Confirmation:
    proposal_sha256: str
    user_message_sha256: str
    user_turn: int
    text: str = field(repr=False)


@dataclass
class Operation:
    status: str
    official_call_id: str | None = None
    result: Any = None
    confirmation_sha256: str | None = None
    confirmation_turn: int | None = None


@dataclass
class Session:
    user_turn: int = 0
    user_messages: list[str] = field(default_factory=list)
    authenticated_user_id: str | None = None
    auth_source_id: str | None = None
    authorized_orders: set[str] = field(default_factory=set)
    order_source_id: str | None = None
    orders: dict[str, dict] = field(default_factory=dict)
    order_detail_sources: dict[str, str] = field(default_factory=dict)
    payment_methods: dict[str, dict] = field(default_factory=dict)
    profile_address: dict[str, Any] = field(default_factory=dict)
    products: dict[str, dict] = field(default_factory=dict)
    product_facts: dict[str, dict] = field(default_factory=dict)
    proposals: list[Proposal] = field(default_factory=list)
    confirmations: list[Confirmation] = field(default_factory=list)
    operations: dict[str, Operation] = field(default_factory=dict)
    unknown_resources: set[str] = field(default_factory=set)
    successes: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)


class ExecutionGuard:
    def __init__(self, official_tools: list[Any] | None = None) -> None:
        self.lock = threading.RLock()
        self.sessions: dict[str, Session] = {}
        self.tool_schemas = {tool.name: tool.params for tool in official_tools or []}
        if official_tools is not None and set(self.tool_schemas) != set(TOOL_POLICY):
            raise ValueError("V0.2 tool registry does not match official Retail tools")

    def register(self, session_id: str) -> None:
        with self.lock:
            if session_id in self.sessions:
                raise ValueError("Duplicate guard session")
            self.sessions[session_id] = Session()

    def unregister(self, session_id: str) -> None:
        with self.lock:
            self.sessions.pop(session_id, None)

    def state(self, session_id: str) -> Session:
        return self.sessions[session_id]

    @staticmethod
    def _clear_identity(s: Session) -> None:
        s.authenticated_user_id = None
        s.auth_source_id = None
        s.authorized_orders.clear()
        s.order_source_id = None
        s.orders.clear()
        s.order_detail_sources.clear()
        s.payment_methods.clear()
        s.profile_address.clear()
        s.confirmations.clear()

    def user_message(self, session_id: str, text: str) -> None:
        with self.lock:
            s = self.state(session_id)
            s.user_turn += 1
            s.user_messages.append(text)
            # Confirmation must follow an actually displayed plan. A new user
            # request/condition invalidates the previous pending confirmation.
            latest = [p for p in s.proposals if p.user_turn == s.user_turn - 1]
            if latest and AFFIRMATIVE.search(text.strip()) and not NEGATIVE_OR_CONDITIONAL.search(text):
                for p in latest:
                    s.confirmations.append(Confirmation(p.text_sha256, fingerprint(text), s.user_turn, text))
            s.events.append({"kind": "user_message", "turn": s.user_turn, "message_sha256": fingerprint(text),
                             "confirmation_count": len(s.confirmations)})

    def assistant_message(self, session_id: str, text: str) -> None:
        with self.lock:
            s = self.state(session_id)
            low = text.lower()
            if re.search(r"\b(?:please confirm|confirm (?:that|you)|would you like me to proceed)\b", low):
                for action, patterns in ACTION_TERMS.items():
                    if any(re.search(p, low) for p in patterns):
                        p = Proposal(action, fingerprint(text), s.user_turn,
                                     set(ORDER_ID.findall(text)), set(re.findall(r"\b\d{10}\b", text)),
                                     set(re.findall(r"\b(?:credit_card|gift_card|paypal)_\w+\b", text)),
                                     set(re.findall(r"\$\s*(\d+(?:\.\d{1,2})?)\b", text)), text)
                        s.proposals.append(p)
                        s.events.append({"kind": "proposal", "action": action, "turn": s.user_turn,
                                         "message_sha256": p.text_sha256, "order_ids": sorted(p.order_ids),
                                         "item_count": len(p.item_ids), "payment_count": len(p.payment_ids),
                                         "amount_count": len(p.amounts)})

    def _valid_arguments(self, name: str, args: dict) -> str | None:
        if has_placeholder(args):
            return "missing_or_placeholder_parameter"
        if any(v is None or (isinstance(v, str) and not v.strip() and k != "address2")
               for k, v in args.items()):
            return "missing_or_placeholder_parameter"
        schema = self.tool_schemas.get(name)
        if schema is not None:
            try:
                schema.model_validate(args, strict=True)
            except Exception:
                return "invalid_parameter_type_or_required_field"
        return None

    def _auth_user_evidence(self, name: str, args: dict, s: Session) -> bool:
        if name == "find_user_id_by_email":
            email = str(args.get("email", "")).lower()
            return bool(email) and any(email in msg.lower() for msg in s.user_messages)
        parts = [str(args.get(k, "")).lower() for k in ("first_name", "last_name", "zip")]
        return all(parts) and any(all(re.search(r"(?<!\w)" + re.escape(part) + r"(?!\w)", msg.lower()) for part in parts)
                                  for msg in s.user_messages)

    def _matching_confirmation(self, name: str, args: dict, s: Session) -> Confirmation | None:
        order_id = args.get("order_id")
        for c in reversed(s.confirmations):
            p = next((p for p in reversed(s.proposals) if p.text_sha256 == c.proposal_sha256 and p.action == name), None)
            if p is None or c.user_turn != s.user_turn:
                continue
            if order_id and p.order_ids and order_id not in p.order_ids:
                continue
            if p.item_ids and not p.item_ids.issubset(set(args.get("item_ids", []) + args.get("new_item_ids", []))):
                continue
            if p.payment_ids and args.get("payment_method_id") and args["payment_method_id"] not in p.payment_ids:
                continue
            # If the confirmation specifies an order/ID different from the plan,
            # the previous plan is stale. A new plan must be displayed.
            confirmation_orders = set(ORDER_ID.findall(c.text))
            if confirmation_orders and order_id and order_id not in confirmation_orders:
                continue
            candidate_ids = set(args.get("item_ids", []) + args.get("new_item_ids", []))
            mentioned_ids = set(re.findall(r"\b\d{10}\b", c.text))
            if mentioned_ids and candidate_ids and not mentioned_ids.issubset(candidate_ids | p.item_ids):
                continue
            confirmed_payments = set(re.findall(r"\b(?:credit_card|gift_card|paypal)_\w+\b", c.text))
            if confirmed_payments and args.get("payment_method_id") and args["payment_method_id"] not in confirmed_payments:
                continue
            confirmed_amounts = set(re.findall(r"\$\s*(\d+(?:\.\d{1,2})?)\b", c.text))
            if confirmed_amounts and not confirmed_amounts.issubset(p.amounts):
                continue
            return c
        return None

    def check(self, session_id: str, name: str, arguments: dict) -> tuple[str, str, dict]:
        """Return (decision, reason, evidence). No alternative tool is selected."""
        with self.lock:
            s = self.state(session_id)
            args = arguments
            if name not in TOOL_POLICY:
                return "blocked", "unknown_official_tool", {}
            problem = self._valid_arguments(name, args)
            if problem:
                return "blocked", problem, {}
            args = normalized(args)
            kind, auth_required = TOOL_POLICY[name]
            if kind == "authentication":
                if not self._auth_user_evidence(name, args, s):
                    return "blocked", "authentication_fields_not_provided_by_current_user", {}
                return "forwarded_to_official", "authentication_lookup", {"user_message_sha256": [fingerprint(m) for m in s.user_messages]}
            if auth_required and not s.authenticated_user_id:
                return "blocked", "authenticate_first_with_email_or_name_and_zip", {}
            evidence = {"auth_official_call_id": s.auth_source_id}
            if name == "get_user_details" and args.get("user_id") != s.authenticated_user_id:
                return "blocked", "requested_user_is_not_authenticated_user", evidence
            if kind in {"order_read", "order_write"}:
                order_id = args.get("order_id")
                if order_id not in s.authorized_orders:
                    return "blocked", "order_not_in_authenticated_users_official_order_list", evidence
                evidence["order_list_official_call_id"] = s.order_source_id
            if kind not in {"order_write", "profile_write", "transfer"}:
                return "forwarded_to_official", "read_allowed", evidence
            resource = str(args.get("order_id") or args.get("user_id") or name)
            if resource in s.unknown_resources:
                return "blocked", "resource_has_unresolved_unknown_write", evidence
            key = argument_digest(name, args)
            op = s.operations.get(key)
            if op and op.status == "succeeded":
                return "cached_reuse", "previous_success_same_operation", evidence | {"source_official_call_id": op.official_call_id}
            if kind == "transfer":
                if not any(re.search(r"\b(?:human|agent|representative|person|team|transfer)\b", m, re.I) for m in s.user_messages):
                    return "blocked", "transfer_not_requested_or_out_of_scope_not_proven", evidence
                return "forwarded_to_official", "transfer_allowed", evidence
            if kind == "profile_write" and args.get("user_id") != s.authenticated_user_id:
                return "blocked", "profile_user_is_not_authenticated_user", evidence
            if kind == "order_write" and args.get("order_id") not in s.orders:
                return "blocked", "order_details_not_yet_queried", evidence
            if kind == "order_write":
                status = s.orders[args["order_id"]].get("status")
                required_status = "delivered" if name in {"exchange_delivered_order_items", "return_delivered_order_items"} else "pending"
                if status != required_status:
                    return "blocked", "latest_official_order_status_not_eligible", evidence | {"observed_status": status}
                evidence["order_detail_official_call_id"] = s.order_detail_sources.get(args["order_id"])
            # A write may use IDs obtained from authorized results, not speculative
            # IDs inferred alongside a still pending read.
            if name in {"exchange_delivered_order_items", "modify_pending_order_items", "return_delivered_order_items"}:
                known_items = {x.get("item_id") for x in s.orders[args["order_id"]].get("items", [])}
                if any(x not in known_items for x in args.get("item_ids", [])):
                    return "blocked", "old_item_not_in_authorized_order_result", evidence
            if name in {"exchange_delivered_order_items", "modify_pending_order_items"}:
                variants = {vid: v for product in s.products.values() for vid, v in product.get("variants", {}).items()}
                if any(vid not in variants or variants[vid].get("available") is not True for vid in args.get("new_item_ids", [])):
                    return "blocked", "new_item_not_in_completed_available_product_result", evidence
                if len(args.get("item_ids", [])) != len(args.get("new_item_ids", [])):
                    return "blocked", "item_list_length_mismatch", evidence
                # The tool result establishes the selected variant ID. Its
                # descriptive option values must also be grounded in the
                # displayed plan or the user's request, excluding internal
                # IDs and generic option keys. This is deliberately strict.
                described = " ".join([*s.user_messages, *(p.text for p in s.proposals)]).lower()
                for vid in args.get("new_item_ids", []):
                    options = variants[vid].get("options", {})
                    if not isinstance(options, dict):
                        return "blocked", "variant_options_missing", evidence
                    for value in options.values():
                        if isinstance(value, str) and value and value.lower() not in described:
                            return "blocked", "variant_option_not_in_user_or_displayed_plan", evidence
            if args.get("payment_method_id") and args["payment_method_id"] not in s.payment_methods:
                return "blocked", "payment_method_not_in_authenticated_user_result", evidence
            if args.get("payment_method_id") and "original payment method" in " ".join(s.user_messages[-2:]).lower():
                order = s.orders.get(args.get("order_id"), {})
                payments = order.get("payment_history", [])
                original = next((p.get("payment_method_id") for p in payments if p.get("transaction_type") == "payment"), None)
                if original and args["payment_method_id"] != original:
                    return "blocked", "payment_method_differs_from_requested_original", evidence
            if name in {"modify_pending_order_address", "modify_user_address"}:
                address = " ".join(s.user_messages).lower()
                current = s.profile_address if name == "modify_user_address" else s.orders[args["order_id"]].get("address", {})
                for key in ("address1", "address2", "city", "state", "country", "zip"):
                    value = str(args.get(key, ""))
                    if value and value.lower() not in address and value != str(current.get(key, "")):
                        return "blocked", "new_address_field_not_from_user", evidence
            if name == "cancel_pending_order" and not any(str(args.get("reason", "")).lower() in m.lower() for m in s.user_messages):
                return "blocked", "cancel_reason_not_from_user", evidence
            c = self._matching_confirmation(name, args, s)
            if c is None:
                return "blocked", "no_current_explicit_confirmation_for_displayed_action", evidence
            if name in {"exchange_delivered_order_items", "modify_pending_order_items"}:
                try:
                    old_items = {x["item_id"]: x for x in s.orders[args["order_id"]]["items"]}
                    variants = {vid: v for product in s.products.values() for vid, v in product["variants"].items()}
                    difference = sum((Decimal(str(variants[new]["price"])) - Decimal(str(old_items[old]["price"]))
                                      for old, new in zip(args["item_ids"], args["new_item_ids"])), Decimal("0"))
                except (KeyError, TypeError, ValueError, InvalidOperation):
                    return "blocked", "price_difference_not_from_completed_tool_results", evidence
                p = next((p for p in reversed(s.proposals) if p.text_sha256 == c.proposal_sha256 and p.action == name), None)
                if p is None or f"{abs(difference):.2f}" not in {f"{Decimal(a):.2f}" for a in p.amounts}:
                    return "blocked", "computed_price_difference_not_displayed_in_confirmed_plan", evidence
                evidence["computed_difference"] = str(difference)
                evidence["product_official_call_ids"] = sorted({f["source_official_call_id"] for f in s.product_facts.values()})
            evidence["confirmation_message_sha256"] = c.user_message_sha256
            evidence["confirmation_turn"] = c.user_turn
            evidence["proposal_message_sha256"] = c.proposal_sha256
            if op:
                if op.status in {"in_flight", "unknown"}:
                    return "blocked", "operation_in_flight_or_result_unknown", evidence
                # Explicit no-write failure may be retried after a new turn and
                # renewed confirmation; the tool remains the final authority.
                if op.status == "failed_no_write" and op.confirmation_turn == c.user_turn:
                    return "blocked", "failed_operation_requires_new_confirmation", evidence
            return "forwarded_to_official", "write_preconditions_satisfied", evidence

    def record_attempt(self, session_id: str, name: str, args: dict, decision: str, reason: str,
                       evidence: dict, *, parlant_call_id: str, batch: Any, iteration: Any,
                       ticket_id: str | None = None, official_call_id: str | None = None) -> None:
        with self.lock:
            s = self.state(session_id)
            s.events.append({"kind": "attempt", "tool_name": name, "decision": decision, "reason": reason,
                             "argument_sha256": argument_digest(name, args), "parlant_call_id": parlant_call_id,
                             "batch": batch, "iteration": iteration, "ticket_id": ticket_id,
                             "official_call_id": official_call_id, "turn": s.user_turn, "evidence": evidence})
            if decision == "forwarded_to_official" and name in WRITE_TOOLS:
                s.operations[argument_digest(name, args)] = Operation("in_flight",
                    confirmation_sha256=evidence.get("confirmation_message_sha256"),
                    confirmation_turn=evidence.get("confirmation_turn"))

    def official_result(self, session_id: str, name: str, args: dict, data: Any, official_call_id: str) -> dict | None:
        with self.lock:
            s = self.state(session_id)
            success = not (isinstance(data, str) and data.startswith("Error:"))
            if name in {"find_user_id_by_email", "find_user_id_by_name_zip"}:
                if success and isinstance(data, str) and data:
                    if s.authenticated_user_id and s.authenticated_user_id != data:
                        # One user per conversation; never switch to a new user.
                        s.events.append({"kind": "identity_switch_rejected", "official_call_id": official_call_id})
                        self._clear_identity(s)
                    else:
                        s.authenticated_user_id, s.auth_source_id = data, official_call_id
                else:
                    self._clear_identity(s)
            elif success and name == "get_user_details" and isinstance(data, dict):
                if data.get("user_id") == s.authenticated_user_id:
                    s.authorized_orders = set(data.get("orders", []))
                    s.order_source_id = official_call_id
                    s.payment_methods = data.get("payment_methods", {}) if isinstance(data.get("payment_methods"), dict) else {}
                    s.profile_address = data.get("address", {}) if isinstance(data.get("address"), dict) else {}
            elif success and name == "get_order_details" and isinstance(data, dict):
                if data.get("order_id") in s.authorized_orders and data.get("user_id") == s.authenticated_user_id:
                    s.orders[data["order_id"]] = data
                    s.order_detail_sources[data["order_id"]] = official_call_id
            elif success and name == "get_product_details" and isinstance(data, dict):
                fact = derive_product_fact(data, official_call_id)
                if fact["product_id"]:
                    s.products[fact["product_id"]] = data
                    s.product_facts[fact["product_id"]] = fact
                    s.events.append({"kind": "derived_product_fact", **fact})
                return fact
            if name in WRITE_TOOLS:
                op = s.operations.get(argument_digest(name, args))
                if op:
                    op.official_call_id, op.result = official_call_id, data
                    if success:
                        op.status = "succeeded"
                        s.successes.append({"tool_name": name, "arguments_sha256": argument_digest(name, args),
                                            "order_id": args.get("order_id"), "user_id": args.get("user_id"),
                                            "official_call_id": official_call_id})
                        if isinstance(data, dict) and data.get("order_id") in s.orders:
                            s.orders[data["order_id"]] = data
                        if name == "modify_user_address" and isinstance(data, dict) and isinstance(data.get("address"), dict):
                            s.profile_address = data["address"]
                    else:
                        # The official runner returns Error: for exceptions. Only
                        # recognize a narrow, inspected set of pre-mutation errors.
                        pre_mutation = ("Variant not found", "Non-pending order", "Non-delivered order",
                                        "Order not found", "User not found", "Payment method not found",
                                        "There should be exactly one payment", "Insufficient gift card balance",
                                        "The number of items", "Invalid reason")
                        op.status = "failed_no_write" if any(x in data for x in pre_mutation) else "unknown"
                        if op.status == "unknown":
                            s.unknown_resources.add(str(args.get("order_id") or args.get("user_id") or name))
                    s.events.append({"kind": "official_outcome", "tool_name": name, "status": op.status,
                                     "official_call_id": official_call_id, "argument_sha256": argument_digest(name, args)})
            return None

    def unknown_result(self, session_id: str, name: str, args: dict) -> None:
        with self.lock:
            op = self.state(session_id).operations.get(argument_digest(name, args))
            if op:
                op.status = "unknown"
                self.state(session_id).unknown_resources.add(str(args.get("order_id") or args.get("user_id") or name))
                self.state(session_id).events.append({"kind": "official_outcome", "tool_name": name,
                                                      "status": "unknown", "argument_sha256": argument_digest(name, args),
                                                      "official_call_id": op.official_call_id})
