"""Append-only, explicit-ID trajectory journal. Each line is a full step snapshot.

Materialize by policy_step_id and increasing record_sequence, never by timestamp.
One decision may propose/execute several calls; arrays preserve every edge.
"""

from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from threading import RLock
from uuid import uuid4

TRUSTED = frozenset(
    {
        "session_token",
        "customer_id",
        "agent_id",
        "api_key",
        "authorization",
        "capability_token",
        "access_token",
        "secret",
    }
)
FRAME = ContextVar("posttraining_frame", default=None)
CANDIDATE = ContextVar("posttraining_candidate", default=None)


def redact(value):
    if isinstance(value, dict):
        protected_argument = str(value.get("parameter_name", "")).lower() in TRUSTED
        return {
            k: "[REDACTED]"
            if k.lower() in TRUSTED or (protected_argument and k == "value_as_string")
            else redact(v)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        # Structured JSON embedded in tool observations/prompts must also be scrubbed.
        try:
            parsed = json.loads(value)
            if isinstance(parsed, (dict, list)):
                sanitized = redact(parsed)
                return json.dumps(sanitized, ensure_ascii=False) if sanitized != parsed else value
        except (ValueError, TypeError):
            pass
        value = re.sub(
            r'(?i)(["\']?(?:session_token|customer_id|agent_id|api_key|authorization|capability_token|access_token|secret)["\']?\s*[:=]\s*)("[^"\n]*"|\'[^\'\n]*\'|[^\s,;}]+)',
            r'\1"[REDACTED]"',
            value,
        )
        return re.sub(r"(?i)Bearer\s+\S+", "Bearer [REDACTED]", value)
    return value


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True)
class DecisionFrame:
    episode_id: str
    session_id: str
    turn_id: str
    engine_iteration: int
    history_refs: tuple = ()
    parent_ids: tuple = ()
    observations: tuple = ()


class TrajectoryLogger:
    def __init__(self, directory=None):
        directory = Path(
            directory
            or os.environ.get(
                "POSTTRAIN_TRAJECTORY_DIR", "/mnt/nvme3/chenyi/posttraining/runtime/trajectories"
            )
        )
        resolved = directory.resolve()
        allowed_roots = (Path("/mnt"), Path("/nas/chenyi/posttraining-qwen"))
        if not any(resolved.is_relative_to(root) for root in allowed_roots):
            raise ValueError("Trajectories require /mnt or the dedicated NAS experiment root")
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / f"policy-{uuid4().hex}.jsonl"
        self.steps, self.call_parents, self.result_parents = {}, {}, {}
        self.lock = RLock()

    def _write(self, step):
        step["record_sequence"] += 1
        step["timestamp"] = datetime.now(timezone.utc).isoformat()
        # Sanitize BEFORE serialization; never persist raw credentials.
        record = redact(step)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def begin(
        self,
        frame,
        tool_id,
        tool_name,
        messages,
        rendered,
        schema,
        model,
        revision,
        generation_origin="interface",
    ):
        with self.lock:
            pid = "policy_" + uuid4().hex
            step = dict(
                schema_version="posttraining.exact.v1",
                record_sequence=0,
                episode_id=frame.episode_id,
                session_id=frame.session_id,
                turn_id=frame.turn_id,
                engine_iteration=frame.engine_iteration,
                policy_step_id=pid,
                parent_policy_step_id=frame.parent_ids[0] if len(frame.parent_ids) == 1 else None,
                parent_policy_step_ids=list(frame.parent_ids),
                schema_name="SingleToolBatchSchema",
                candidate_tool_id=tool_id,
                candidate_tool_name=tool_name,
                prompt_messages=redact(messages),
                rendered_prompt=redact(rendered),
                prompt_hash=digest(rendered),
                prompt_hash_scope="exact_model_visible_rendered_prompt",
                available_tool_schema=schema,
                conversation_history_refs=list(frame.history_refs),
                relevant_tool_observations=list(frame.observations),
                model_name=model,
                model_revision=revision,
                raw_model_output=None,
                parsed_schema_output=None,
                generation_origin=generation_origin,
                parse_status="pending",
                proposed_action=None,
                proposed_tool_name=None,
                proposed_arguments=None,
                proposed_calls=[],
                actual_gate_result=None,
                actual_executed_tool_call_id=None,
                actual_tool_name=None,
                actual_arguments=None,
                actual_executed_calls=[],
                tool_result_id=None,
                tool_result=None,
                tool_results=[],
                next_policy_step_id=None,
                next_policy_step_ids=[],
                terminal_status=None,
                final_reward=None,
                failure_type=None,
                loss_design={
                    "system_user_tool_observations": "input_only",
                    "target": "consumed_policy_fields_only",
                    "token_spans": None,
                },
            )
            for parent in frame.parent_ids:
                old = self.steps[parent]
                if old["episode_id"] != frame.episode_id or old["session_id"] != frame.session_id:
                    raise ValueError("Cross-episode parent")
            self.steps[pid] = step
            self._write(step)
            for parent in frame.parent_ids:
                old = self.steps[parent]
                old["next_policy_step_ids"].append(pid)
                old["next_policy_step_id"] = pid if len(old["next_policy_step_ids"]) == 1 else None
                self._write(old)
            return pid

    def update(self, pid, **fields):
        with self.lock:
            if set(fields) - self.steps[pid].keys():
                raise ValueError("Unknown trajectory field")
            self.steps[pid].update(redact(fields))
            self._write(self.steps[pid])

    def gate(self, pid, calls, evaluations, missing, invalid):
        with self.lock:
            step = self.steps[pid]
            if step["parse_status"] != "passed" or step["actual_gate_result"] is not None:
                raise ValueError("Gate requires one successfully parsed decision")
            for call in calls:
                if str(call.id) in self.call_parents:
                    raise ValueError("Duplicate call ID")
                if call.tool_id.to_string() != step["candidate_tool_id"]:
                    raise ValueError("Cross-candidate call")
            for call in calls:
                self.call_parents[str(call.id)] = pid
            result = {
                "status": "accepted"
                if calls
                else ("no_call" if step["proposed_action"] == "NO_CALL" else "rejected"),
                "evaluations": [str(e[1].name) for e in evaluations],
                "missing": [x.parameter for x in missing],
                "invalid": [x.parameter for x in invalid],
                "accepted_tool_call_ids": [str(c.id) for c in calls],
                "accepted_calls": [
                    {
                        "tool_call_id": str(c.id),
                        "tool_name": c.tool_id.to_string(),
                        "arguments": redact(c.arguments),
                    }
                    for c in calls
                ],
            }
            self.update(
                pid,
                actual_gate_result=result,
                failure_type="GATE_REJECTED" if result["status"] == "rejected" else None,
            )

    def executing(self, call):
        with self.lock:
            pid = self.call_parents[str(call.id)]  # No temporal/name/argument fallback.
            step = self.steps[pid]
            binding = next(
                c
                for c in step["actual_gate_result"]["accepted_calls"]
                if c["tool_call_id"] == str(call.id)
            )
            if binding["tool_name"] != call.tool_id.to_string() or binding["arguments"] != redact(
                call.arguments
            ):
                raise ValueError("Call differs from its original gate binding")
            if any(c["tool_call_id"] == str(call.id) for c in step["actual_executed_calls"]):
                raise ValueError("Call already dispatched")
            entry = {
                "tool_call_id": str(call.id),
                "tool_name": call.tool_id.to_string(),
                "arguments": redact(call.arguments),
                "argument_stage": "original_ToolCaller_dispatch",
            }
            step["actual_executed_calls"].append(entry)
            self.update(
                pid,
                actual_executed_tool_call_id=str(call.id)
                if len(step["actual_executed_calls"]) == 1
                else None,
                actual_tool_name=entry["tool_name"]
                if len(step["actual_executed_calls"]) == 1
                else None,
                actual_arguments=entry["arguments"]
                if len(step["actual_executed_calls"]) == 1
                else None,
            )

    def result(self, result):
        with self.lock:
            cid, rid = str(result.tool_call.id), str(result.id)
            pid = self.call_parents[cid]
            step = self.steps[pid]
            dispatched = next(
                (c for c in step["actual_executed_calls"] if c["tool_call_id"] == cid), None
            )
            if (
                dispatched is None
                or dispatched["tool_name"] != result.tool_call.tool_id.to_string()
                or dispatched["arguments"] != redact(result.tool_call.arguments)
            ):
                raise ValueError("Result differs from its dispatched call binding")
            if (
                not any(c["tool_call_id"] == cid for c in step["actual_executed_calls"])
                or rid in self.result_parents
            ):
                raise ValueError("Result without unique dispatched call")
            if any(r["tool_call_id"] == cid for r in step["tool_results"]):
                raise ValueError("Duplicate result for call")
            self.result_parents[rid] = pid
            step["tool_results"].append(
                {"tool_call_id": cid, "tool_result_id": rid, "result": redact(result.result)}
            )
            self.update(
                pid,
                tool_result_id=rid if len(step["tool_results"]) == 1 else None,
                tool_result=redact(result.result) if len(step["tool_results"]) == 1 else None,
            )

    def finish_episode(self, episode_id, status, reward=None, failure_type=None):
        for pid, step in list(self.steps.items()):
            if step["episode_id"] == episode_id:
                self.update(
                    pid,
                    terminal_status=status,
                    final_reward=reward,
                    failure_type=failure_type or step["failure_type"],
                )
