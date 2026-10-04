"""Bounded pre-emission checks for facts covered by the V0.2 extension.

Only product availability counts and completed-action claims are checked. This
does not establish general natural-language factual correctness.
"""

from __future__ import annotations

import re
from typing import Any

from v02_guard import ExecutionGuard, fingerprint


COUNT_PHRASE = re.compile(
    r"\b(?P<ratio_available>\d+)\s+(?P<ratio_link>of(?:\s+(?:the|its))?|out\s+of(?:\s+the)?)\s+"
    r"(?P<ratio_total>\d+)\s+(?P<ratio_suffix>(?:returned\s+)?(?:variants?|options?)\s+"
    r"(?:are\s+)?(?:currently\s+)?available)\b"
    r"|\b(?P<available_prefix>\d+)\s+(?P<prefix_suffix>available\s+(?:product\s+)?(?:variants?|options?))\b"
    r"|\b(?P<available_suffix>\d+)\s+(?P<suffix_tail>(?:variants?|options?)\s+"
    r"(?:are\s+)?(?:currently\s+)?available)\b"
    r"|\b(?P<available_pronoun>\d+)\s+(?P<pronoun_tail>of\s+(?:them|those)\s+"
    r"(?:are\s+)?(?:currently\s+)?available)\b",
    re.I,
)
CLAIMS = {
    "exchange_delivered_order_items": re.compile(r"\b(?:exchang(?:e|es)|exchange request)\b.{0,80}\b(?:completed|submitted|done|successful)\b|\b(?:completed|submitted|done|successful)\b.{0,80}\bexchange\b", re.I),
    "return_delivered_order_items": re.compile(r"\b(?:return|refund)\b.{0,80}\b(?:completed|submitted|done|successful)\b|\b(?:completed|submitted|done|successful)\b.{0,80}\breturn\b", re.I),
    "modify_pending_order_items": re.compile(r"\b(?:item|shirt|product)\b.{0,80}\b(?:changed|modified|updated|completed)\b|\b(?:changed|modified|updated|completed)\b.{0,80}\b(?:item|shirt|product)\b", re.I),
    "modify_pending_order_payment": re.compile(r"\b(?:payment method|card)\b.{0,80}\b(?:changed|modified|updated)\b|\b(?:changed|modified|updated)\b.{0,80}\b(?:payment method|card)\b", re.I),
    "modify_pending_order_address": re.compile(r"\b(?:shipping|order|delivery) address\b.{0,80}\b(?:changed|modified|updated)\b", re.I),
    "modify_user_address": re.compile(r"\b(?:default|profile|account) address\b.{0,80}\b(?:changed|modified|updated)\b", re.I),
    "cancel_pending_order": re.compile(r"\b(?:cancellation|cancel(?:led|ed)?)\b.{0,60}\b(?:completed|submitted|successful)\b|\border\b.{0,60}\b(?:cancelled|canceled)\b", re.I),
    "transfer_to_human_agents": re.compile(r"\b(?:you are being transferred|transferred to a human|transfer (?:is )?(?:complete|successful))\b", re.I),
}
NEGATED = re.compile(r"\b(?:not|haven't|hasn't|wasn't|failed|unable|cannot|can't|pending|would|could)\b", re.I)
GENERIC_MOD_CLAIM = re.compile(r"\b(?:order|account|profile)\b.{0,80}\b(?:updated|modified|changed)\b", re.I)
FUTURE_OR_POLICY = re.compile(r"\b(?:i can (?:change|modify|cancel|return|exchange)|can no longer|cannot be|should i|please confirm|once (?:you|the)|after (?:you|the))\b", re.I)


def supplemental_guideline(fact: dict | None, *, action: str | None = None, success: bool = False) -> list[dict]:
    rows = []
    if fact and fact.get("complete"):
        rows.append({"action": f"V0.2 derived fact from official get_product_details call {fact['source_official_call_id']}: "
                                f"for product {fact['product_name']} (product_id {fact['product_id']}), "
                                f"{fact['available_true']} of {fact['variants_total']} returned variants have available=true. "
                                "Use this scoped computed count when answering the customer's availability question."})
    if action:
        status = "succeeded" if success else "did not succeed"
        rows.append({"action": f"V0.2 execution evidence: {action} {status}. Claim completion only if it succeeded for this object."})
    return rows


def check_reply(guard: ExecutionGuard, session_id: str, message: str) -> tuple[str, dict[str, Any]]:
    """Repair covered contradictions before EventPublisher persists the AI message."""
    s = guard.state(session_id)
    original_sha = fingerprint(message)
    result = message
    corrections: list[dict[str, Any]] = []
    facts = [f for f in s.product_facts.values() if f.get("complete")]
    count_chunks = re.split(r"(?<=[.!?])(?=\s|$)", result)
    corrected_chunks = []
    for chunk in count_chunks:
        incomplete = [f for f in s.product_facts.values() if not f.get("complete") and f.get("product_name")
                      and str(f["product_name"]).lower().replace("-", "") in chunk.lower().replace("-", "")]
        if len(incomplete) == 1 and COUNT_PHRASE.search(chunk):
            fact = incomplete[0]
            corrections.append({"field": "available_true_unknown", "product_id": fact["product_id"],
                                "source_official_call_id": fact["source_official_call_id"]})
            corrected_chunks.append(f" The available variant count for {fact['product_name']} is unknown because the returned data is incomplete.")
            continue
        matching = [f for f in facts if f.get("product_name") and
                    str(f["product_name"]).lower().replace("-", "") in chunk.lower().replace("-", "")]
        if not matching and len(facts) == 1 and str(facts[0].get("product_name") or "").lower().replace("-", "") in result.lower().replace("-", ""):
            matching = facts
        if len(matching) == 1:
            fact = matching[0]
            def replace(match: re.Match) -> str:
                actual = fact["available_true"]
                if match.group("ratio_available") is not None:
                    given = int(match.group("ratio_available"))
                    total = int(match.group("ratio_total"))
                    if given != actual:
                        corrections.append({"field": "available_true", "product_id": fact["product_id"],
                                            "source_official_call_id": fact["source_official_call_id"],
                                            "observed": given, "actual": actual})
                    if total != fact["variants_total"]:
                        corrections.append({"field": "variants_total", "product_id": fact["product_id"],
                                            "source_official_call_id": fact["source_official_call_id"],
                                            "observed": total, "actual": fact["variants_total"]})
                    return f"{actual} {match.group('ratio_link')} {fact['variants_total']} {match.group('ratio_suffix')}"
                for count_group, tail_group in (("available_prefix", "prefix_suffix"),
                                                ("available_suffix", "suffix_tail"),
                                                ("available_pronoun", "pronoun_tail")):
                    if match.group(count_group) is not None:
                        given = int(match.group(count_group))
                        if given != actual:
                            corrections.append({"field": "available_true", "product_id": fact["product_id"],
                                                "source_official_call_id": fact["source_official_call_id"],
                                                "observed": given, "actual": actual})
                        return f"{actual} {match.group(tail_group)}"
                return match.group(0)
            chunk = COUNT_PHRASE.sub(replace, chunk)
        corrected_chunks.append(chunk)
    result = "".join(corrected_chunks)
    # A sentence with a success assertion needs an official success for that
    # action and, if an order is named, for that same order. Local block, failed
    # official result, and unknown result never grant this evidence.
    chunks = re.split(r"(?<=[.!?])(?=\s|$)", result)
    repaired = []
    for chunk in chunks:
        stripped = chunk.strip()
        if not stripped:
            repaired.append(chunk)
            continue
        if FUTURE_OR_POLICY.search(stripped):
            repaired.append(chunk)
            continue
        unsupported = []
        if re.fullmatch(r"(?:done|completed|all set)[.!]?", stripped, re.I) and not s.successes:
            unsupported.append("unspecified_action")
        for action, pattern in CLAIMS.items():
            match = pattern.search(stripped)
            if not match:
                continue
            if NEGATED.search(stripped[max(0, match.start()-28):match.end()]):
                continue
            order_ids = re.findall(r"#W\d+", stripped, re.I)
            if not any(row["tool_name"] == action and (not order_ids or row.get("order_id") in order_ids)
                       for row in s.successes):
                unsupported.append(action)
        generic_match = GENERIC_MOD_CLAIM.search(stripped)
        if generic_match and not NEGATED.search(stripped[max(0, generic_match.start()-28):generic_match.end()]):
            order_ids = re.findall(r"#W\d+", stripped, re.I)
            if not any(row["tool_name"].startswith("modify_") and
                       (not order_ids or row.get("order_id") in order_ids) for row in s.successes):
                unsupported.append("unspecified_modification")
        if unsupported:
            corrections.append({"field": "unsupported_completion", "actions": unsupported,
                                "source_official_call_id": None})
            repaired.append(" I have not completed that action. I need to confirm its status before I can say it is done.")
        else:
            repaired.append(chunk)
    result = "".join(repaired)
    known_last_four = {str(v.get("last_four")) for v in s.payment_methods.values()
                       if isinstance(v, dict) and v.get("last_four") is not None}
    def checked_card_tail(match: re.Match) -> str:
        digits = match.group(1)
        if digits not in known_last_four:
            corrections.append({"field": "unsupported_card_tail", "observed_sha256": fingerprint(digits),
                                "source_official_call_id": s.order_source_id})
            return "selected payment method"
        return match.group(0)
    result = re.sub(r"\b(?:card|Mastercard|Visa|credit card)\s+ending\s+in\s+(\d{4})\b",
                    checked_card_tail, result, flags=re.I)
    return result, {"kind": "reply_pre_emission_check", "original_sha256": original_sha,
                    "sent_sha256": fingerprint(result), "corrections": corrections,
                    "product_source_ids": [f["source_official_call_id"] for f in facts],
                    "successful_official_call_ids": [r["official_call_id"] for r in s.successes]}
