"""Independent admission of exact, visible-evidence rule supervision."""

import hashlib
import json

from parlant.core.engines.alpha.tool_calling.single_tool_batch import SingleToolBatchSchema
from ..policy import reject_trusted_keys


def verify_sample(sample, lineage, split_registry):
    if sample["source_type"] != "synthetic_rule_expert" or sample["split"] not in (
        "train",
        "sft_valid",
    ):
        raise ValueError("DEV/oracle/provider output is not policy supervision")
    if split_registry.get(sample["family_id"]) != sample["split"]:
        raise ValueError("Unregistered or overlapping family split")
    if sample["episode_id"].startswith("PTDEV-") or lineage.get("counts_as_baseline"):
        raise ValueError("DEV episode contamination")
    target = sample["target_json"]
    reject_trusted_keys(target)
    parsed = SingleToolBatchSchema.model_validate(target)
    if parsed.name != sample["candidate_tool"]:
        raise ValueError("Candidate/target mismatch")
    if sample["prompt_token_ids"] != lineage["prompt_token_ids"]:
        raise ValueError("Runtime/training prefix differs")
    if hashlib.sha256(lineage["rendered_prompt"].encode()).hexdigest() != sample["prompt_sha256"]:
        raise ValueError("Prompt hash mismatch")
    if sample["policy_step_id"] != lineage["policy_step_id"]:
        raise ValueError("Policy attribution mismatch")
    if not all(
        sample["verification"].get(k) is True
        for k in ("schema", "visible_evidence", "native_gate", "business_predicates")
    ):
        raise ValueError("Unreviewed or failed independent verification")
    available = {str(f["event_id"]): f for f in lineage["preceding_visible_facts"]}
    calls = [c for c in parsed.tool_calls_for_candidate_tool if c.is_applicable]
    for call in calls:
        for arg in call.argument_evaluations or []:
            refs = sample["argument_evidence"].get(arg.parameter_name, [])
            if not refs or any(ref not in available for ref in refs):
                raise ValueError("Argument unsupported by preceding visible evidence")
            paths = sample["argument_paths"].get(arg.parameter_name, {})
            supported = False
            for ref in refs:
                value = available[ref]["data"]
                path = paths.get(ref)
                if not isinstance(path, list):
                    raise ValueError("Exact visible fact path required")
                try:
                    for component in path:
                        value = value[component]
                except (KeyError, IndexError, TypeError):
                    continue
                if isinstance(value, (str, int, float)) and not isinstance(value, bool):
                    supported |= str(value) == arg.value_as_string
            if not supported:
                raise ValueError("Exact visible fact does not support the proposed value")
    if lineage.get("backend_rejection") or lineage.get("wrong_action"):
        raise ValueError("Rejected/wrong proposal cannot become a positive action label")
    if calls and len(lineage.get("gate_call_ids", [])) != len(calls):
        raise ValueError("CALL targets lack exact native gate evidence")
    if set(lineage.get("executed_call_ids", [])) != set(lineage.get("result_call_ids", [])):
        raise ValueError("Actual tool results lack execution attribution")
    if calls and set(lineage["gate_call_ids"]) != set(lineage["executed_call_ids"]):
        raise ValueError("Expert CALL not executed; no fabricated successful label")
    normalized = {
        "prompt": lineage["rendered_prompt"],
        "candidate": sample["candidate_tool"],
        "target": target,
    }
    return hashlib.sha256(
        json.dumps(normalized, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
