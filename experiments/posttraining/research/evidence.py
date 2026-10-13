"""Keep parser failure gate diagnostics without fabricating a parsed proposal."""

from ..episode_evidence import CanonicalTrajectory
from ..trajectory import TrajectoryLogger
from ..policy import TrustedValueLeak
from pathlib import Path


class ResearchTrajectory(CanonicalTrajectory):
    def __init__(self, directory, config, task):
        super().__init__(directory, config, task)
        from dotenv import dotenv_values

        protected = [config.get(k) for k in ("customer_id", "session_token", "policy_capability")]
        protected.extend(task.get("initial_db_fixture", {}).get("trusted_context", {}).get(k)
                         for k in ("customer_id", "session_token"))
        env_path = config.get("repo_env")
        if env_path and Path(env_path).is_file():
            protected.extend(v for k, v in dotenv_values(env_path).items()
                             if any(s in k.upper() for s in ("TOKEN", "KEY", "SECRET", "PASSWORD")))
        self._forbidden_values = tuple(set(v for v in protected if isinstance(v, str) and len(v) >= 4))
        self._boundary_rejections = {}

    def assert_no_forbidden_values(self, text):
        if any(v in text for v in self._forbidden_values):
            raise TrustedValueLeak("Actual protected server value at the policy boundary")

    def mask_forbidden_values(self, text):
        for value in self._forbidden_values:
            text = text.replace(value, "[REDACTED_ACTUAL_TRUSTED_VALUE]")
        return text

    def record_boundary_gate(self, pid, result, rejections):
        self._boundary_rejections[pid] = rejections
        try:
            self.gate(pid, *result)
        finally:
            self._boundary_rejections.pop(pid)

    def update(self, pid, **fields):
        gate = fields.get("actual_gate_result") or {}
        if gate and pid in self._boundary_rejections:
            gate = dict(gate, boundary_rejections=self._boundary_rejections[pid])
            if not gate.get("accepted_calls"):
                gate["status"] = "rejected"
            fields = dict(fields, actual_gate_result=gate, failure_type="INVALID_PROPOSAL_ARGUMENTS")
        if gate.get("status") == "parse_failed_no_dispatch":
            if self.steps[pid]["parse_status"] != "failed" or gate.get("accepted_calls"):
                raise ValueError("Invalid failed-parse diagnostic gate")
            # Frozen scorer represents this decision as policy_failure and
            # requires a parsed proposal for a canonical gate_result. The exact
            # raw logger still retains the actual native empty-evaluation gate.
            TrajectoryLogger.update(self, pid, **fields)
            return
        # A native gate is journaled once; later boundary detail enriches only
        # its exact snapshot rather than creating a second causal gate event.
        if gate and self.steps[pid]["actual_gate_result"] is not None:
            previous = self.steps[pid]["actual_gate_result"]
            if previous.get("accepted_calls", []) != gate.get("accepted_calls", []):
                raise ValueError("Gate enrichment changed its accepted ToolCall bindings")
            TrajectoryLogger.update(self, pid, **fields)
            return
        super().update(pid, **fields)
