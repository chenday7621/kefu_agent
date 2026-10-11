"""Keep parser failure gate diagnostics without fabricating a parsed proposal."""

from ..episode_evidence import CanonicalTrajectory
from ..trajectory import TrajectoryLogger


class ResearchTrajectory(CanonicalTrajectory):
    def update(self, pid, **fields):
        gate = fields.get("actual_gate_result") or {}
        if gate.get("status") == "parse_failed_no_dispatch":
            if self.steps[pid]["parse_status"] != "failed" or gate.get("accepted_calls"):
                raise ValueError("Invalid failed-parse diagnostic gate")
            # Frozen scorer represents this decision as policy_failure and
            # requires a parsed proposal for a canonical gate_result. The exact
            # raw logger still retains the actual native empty-evaluation gate.
            TrajectoryLogger.update(self, pid, **fields)
            return
        super().update(pid, **fields)
