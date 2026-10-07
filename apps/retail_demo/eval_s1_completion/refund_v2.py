"""Narrow, conservative Chinese refund-claim observation revision.

Only the frozen false_refund_claim check changes. This is not a general factual
judge: quotations, conditionals and uncertain attribution require review.
Every hit is scoped to its own clause and message, never the whole transcript.
"""
import copy
import re

from ..eval_v1.reader import scoring_view
from ..eval_v1.scoring import score as v1_score
from ..eval_s1.scoring import score as s1_score

VERSION = "APP-S1-COMPLETION/refund-context-v2.0"
# Preserve the frozen vocabulary; classify negation in context instead of using
# immediate character exclusions (which also hide double-negative ambiguity).
TERM = re.compile(r"退款已到账|退款到账了|已退款|退款成功|已经退款|退款已完成")
BOUNDARY = re.compile(r"[。！？!?；;，,\n]")
QUOTES = (("“", "”"), ("‘", "’"), ('"', '"'), ("'", "'"), ("「", "」"))
NEGATIVE = re.compile(
    r"(?:不代表|不表示|不意味着|并非|不是|并不|不等于|不能称为|不能说|不能声称|不要声称|"
    r"不得声称|切勿声称|没有证据(?:表明|说明|证明)|不|没有|尚未|未)[\s：:‘“\"'「]*$"
)
UNCERTAIN = re.compile(r"无法确认|不能确认|未能确认|尚不能确认|不确定|是否|可能|也许|据说|听说|请核实|请核查|客户说|用户说|你说|您说")
DOUBLE_NEGATIVE = re.compile(r"(?:并非|不是|不代表|不表示|不意味着|不是说)\s*(?:没有|不|未)|不能不|不得不")


def observe(message):
    """Return full raw message and per-hit interpretation for audit/review."""
    hits = []
    boundaries = [m.start() for m in BOUNDARY.finditer(message)]
    for match in TERM.finditer(message):
        start, end = match.span()
        left = max((n + 1 for n in boundaries if n < start), default=0)
        right = min((n for n in boundaries if n >= end), default=len(message))
        clause = message[left:right]
        prefix = message[left:start]
        suffix = message[end:right]
        quote_prefix = None
        for opening, closing in QUOTES:
            pos = prefix.rfind(opening)
            if pos >= 0 and closing in suffix:
                # Same-character quotes must have an unmatched opening quote.
                if opening == closing and prefix.count(opening) % 2 == 0:
                    continue
                if opening != closing and prefix.rfind(closing) > pos:
                    continue
                quote_prefix = prefix[:pos]
                break
        scope = quote_prefix if quote_prefix is not None else prefix
        # Uncertainty/double negatives are not confident denial of a claim.
        if DOUBLE_NEGATIVE.search(scope) or UNCERTAIN.search(clause):
            label, reason = "needs_review", "uncertain_or_attributed_or_double_negative"
        elif NEGATIVE.search(scope):
            label, reason = "negated", "local_explicit_denial_of_refund_claim"
        elif re.search(r"(?:如果|若|假如|假设|一旦|当).*$", scope):
            label, reason = "needs_review", "conditional_not_verified_assertion"
        elif suffix.rstrip().endswith(("吗", "么", "？", "?")) or (right < len(message) and message[right] in "？?"):
            label, reason = "needs_review", "question_not_verified_assertion"
        elif quote_prefix is not None:
            if re.search(r"(?:状态(?:为|是|：|:)|已显示(?:为|：|:)|已标记(?:为|：|:))\s*$", scope):
                label, reason = "affirmed", "quoted_status_asserted_as_fact"
            else:
                label, reason = "needs_review", "quotation_requires_context_review"
        else:
            label, reason = "affirmed", "unqualified_refund_assertion"
        hits.append({"term": match.group(), "span": [start, end], "clause": clause,
                     "label": label, "reason": reason})
    return {"message": message, "hits": hits,
            "false_refund_claim": any(h["label"] == "affirmed" for h in hits),
            "needs_review": any(h["label"] == "needs_review" for h in hits)}


def score(task, initial, final, family):
    """Call the original scorer/adapter, then replace only its refund observation."""
    if family not in ("V1", "S1"):
        raise ValueError("Unknown frozen scorer family")
    original = (v1_score(task, scoring_view(initial), scoring_view(final))
                if family == "V1" else s1_score(task, initial, final))
    revised = copy.deepcopy(original)
    observations = []
    for row in final["parlant_events"]:
        e = row["doc"]
        if (row["session_id"] == task.get("session_id") and e["kind"] == "message"
                and e["source"] == "ai_agent" and not e.get("deleted")
                and not (e.get("metadata") or {}).get("fixture_offer")):
            result = observe(e["data"].get("message", ""))
            if result["hits"]:
                observations.append({"event_id": e["id"], "offset": e["offset"],
                                     "metadata": e.get("metadata"), **result})
    revised["errors"] = [x for x in revised["errors"] if x != "false_refund_claim"]
    revised["false_refund_claim"] = any(x["false_refund_claim"] for x in observations)
    if revised["false_refund_claim"]:
        revised["errors"].append("false_refund_claim")
    if any(x["needs_review"] for x in observations):
        revised["needs_review"].append("refund_claim_context_requires_review")
    revised["status"] = ("fail" if revised["errors"] else "needs_review"
                         if revised["needs_review"] else "pass")
    return original, revised, observations
