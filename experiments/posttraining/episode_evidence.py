"""Canonical scorer evidence emitted at explicit native logger boundaries."""

from copy import deepcopy
from datetime import datetime
import json

from parlant.core.services.tools.mcp_service import prepare_tool_arguments

from .dev_protocol import sha
from .episode_state import EpisodeDatabase
from .trajectory import CANDIDATE, TrajectoryLogger, redact


def canonical_arguments(tool, arguments):
    """Project native semantics without altering raw proposals or execution.

    The native gate fills optional arguments with None. MCP's original array
    converter does not convert scalar integers: APP Quantity.BeforeValidator
    performs that normalization. Both boundaries must be reflected consistently.
    Invalid values remain invalid for the frozen scorer; they are never repaired.
    """
    from apps.retail_demo.mcp_server import native_integer
    try:
        result = dict(prepare_tool_arguments(arguments, tool.parameters))
    except Exception:
        result = dict(arguments)
    result = {k: v for k, v in result.items()
              if v is not None or k in tool.required or k not in tool.parameters}
    if tool.name == "check_return_eligibility" and "quantity" in result:
        result["quantity"] = native_integer(result["quantity"])
    return result


class CanonicalJournal:
    def __init__(self, directory):
        self.path = directory / "CANONICAL_EVENTS.jsonl"
        self.sequence = len(self.read())

    def read(self):
        return [json.loads(s) for s in self.path.read_text().splitlines()] if self.path.exists() else []

    def append(self, kind, event_id=None, **values):
        from uuid import uuid4
        self.sequence += 1
        entry = {"sequence": self.sequence, "event_id": event_id or "audit_" + uuid4().hex,
                 "kind": kind, **values}
        with self.path.open("a") as out:
            out.write(json.dumps(redact(entry), ensure_ascii=False) + "\n")
            out.flush()
        return entry["event_id"]


class CanonicalTrajectory(TrajectoryLogger):
    def __init__(self, directory, config, task):
        super().__init__(directory / "trajectories")
        self.journal = CanonicalJournal(directory)
        self.task = task
        self.projector = EpisodeDatabase.__new__(EpisodeDatabase)
        self.projector.url = config["database_url"]
        self.projector.anchor = datetime.fromisoformat(config["clock_anchor"])
        self.projector.task = task
        self.converted = {}
        self.policy_events = set()

    def update(self, pid, **fields):
        super().update(pid, **fields)
        if fields.get("parse_status") == "passed" and pid not in self.policy_events:
            tool = CANDIDATE.get()[1]
            self.converted[pid] = lambda args: canonical_arguments(tool, args)
            self.journal.append("policy_proposal", policy_step_id=pid, split="DEV", family=self.task["family"],
                                proposed_calls=[dict(p, arguments=self.converted[pid](p["arguments"]))
                                                for p in self.steps[pid]["proposed_calls"]])
            self.policy_events.add(pid)
        elif fields.get("parse_status") in ("failed", "not_attempted") and pid not in self.policy_events:
            self.journal.append("policy_failure", policy_step_id=pid, split="DEV", family=self.task["family"],
                                failure_type=fields.get("failure_type"))
            self.policy_events.add(pid)
        if fields.get("actual_gate_result"):
            gate = fields["actual_gate_result"]
            self.journal.append("gate_result", policy_step_id=pid, status=gate["status"],
                                accepted_calls=[dict(c, arguments=self.converted[pid](c["arguments"]))
                                                for c in gate.get("accepted_calls", [])])

    def result(self, result):
        super().result(result)
        cid = str(result.tool_call.id)
        pid = self.call_parents[cid]
        name = result.tool_call.tool_id.tool_name
        raw = deepcopy(result.result)
        payload = deepcopy(raw["data"])
        # All native failures, including transport errors, remain failures.
        if "ok" not in payload:
            payload = {"ok": False, "error": payload, "data": None}
        if payload["ok"] and name in ("check_return_eligibility", "get_operation_result"):
            data = payload["data"]
            oid = str(data["operation_id"])
            op = self.projector.operation(oid)
            facts = data.get("original_parameters") or data.get("preview") or {}
            for key in ("order_id", "item_id", "quantity", "reason"):
                if key in facts and facts[key] != op[key]:
                    raise ValueError("Native operation result differs from authoritative DB")
            data["operation"] = op
        self.journal.append("tool_result", policy_step_id=pid, tool_call_id=cid,
                            tool_result_id=str(result.id), result=payload,
                            raw_payload_hash=sha(redact(raw)), raw_payload=redact(raw))


def audit_trajectory(directory, task_id):
    import jsonschema
    from pathlib import Path
    schema = json.loads(Path("experiments/posttraining/logger_schema.json").read_text())
    steps, calls, result_ids = {}, {}, set()
    paths = list((directory / "trajectories").glob("policy-*.jsonl"))
    for path in paths:
        for line in path.read_text().splitlines():
            row = json.loads(line)
            jsonschema.validate(row, schema)
            pid = row["policy_step_id"]
            if row["episode_id"] != task_id:
                raise ValueError("Cross-episode trajectory")
            if row["record_sequence"] != steps.get(pid, {"record_sequence": 0})["record_sequence"] + 1:
                raise ValueError("Missing or duplicate journal sequence")
            steps[pid] = row
    for pid, row in steps.items():
        for parent in row["parent_policy_step_ids"]:
            if parent not in steps or pid not in steps[parent]["next_policy_step_ids"]:
                raise ValueError("Broken explicit next-policy link")
        for call in (row["actual_gate_result"] or {}).get("accepted_calls", []):
            cid = call["tool_call_id"]
            if cid in calls:
                raise ValueError("Native call crosses candidates")
            calls[cid] = pid
        executed = {c["tool_call_id"] for c in row["actual_executed_calls"]}
        returned = {r["tool_call_id"] for r in row["tool_results"]}
        if executed != returned:
            raise ValueError("Executed call lacks exact result")
        if any(calls.get(cid) != pid for cid in executed):
            raise ValueError("Executed call lacks gate binding")
        for result in row["tool_results"]:
            rid = result["tool_result_id"]
            if rid in result_ids:
                raise ValueError("Duplicate result ID")
            result_ids.add(rid)
        observed = {r["tool_result_id"] for r in row["relevant_tool_observations"]}
        required = {r["tool_result_id"] for parent in row["parent_policy_step_ids"] for r in steps[parent]["tool_results"]}
        if not required.issubset(observed):
            raise ValueError("Native result omitted from next policy input")
    return {"passed": True, "policy_decisions": len(steps), "gate_calls": len(calls),
            "tool_results": len(result_ids), "journal_paths": [str(p) for p in paths]}
