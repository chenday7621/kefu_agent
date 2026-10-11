"""CPU contracts for the real native boundary exposed by Phase 1-D."""

from copy import deepcopy
from datetime import datetime, timezone
import unittest

from parlant.core.tools import Tool, ToolOverlap, ToolParameterOptions

from .episode_evidence import canonical_arguments
from .episode_state import normalize, resolve


class EvidenceChecks(unittest.TestCase):
    def setUp(self):
        self.tool = Tool(name="check_return_eligibility", creation_utc=datetime.now(timezone.utc),
                         metadata={}, description="test", consequential=True, overlap=ToolOverlap.NONE,
                         parameters={k: ({"type": "string"}, ToolParameterOptions())
                                     for k in ("order_id", "item_id", "quantity", "reason", "replaces_operation_id")},
                         required=["order_id", "item_id", "quantity", "reason"])
        self.proposal = {"order_id": "order", "item_id": "item", "quantity": "2", "reason": "退货原因"}

    def test_native_optional_default_is_semantically_the_same_proposal(self):
        gate = dict(self.proposal, replaces_operation_id=None)
        self.assertEqual(canonical_arguments(self.tool, self.proposal), canonical_arguments(self.tool, gate))

    def test_original_app_quantity_validator_is_used(self):
        self.assertEqual(canonical_arguments(self.tool, self.proposal)["quantity"], 2)

    def test_invalid_scalar_is_not_repaired_to_a_valid_quantity(self):
        for value in ("2.0", "-1", "two", True, 2.5):
            with self.subTest(value=value):
                projected = canonical_arguments(self.tool, dict(self.proposal, quantity=value))
                self.assertEqual(projected["quantity"], value)

    def test_required_none_is_preserved_as_invalid_evidence(self):
        projected = canonical_arguments(self.tool, dict(self.proposal, reason=None))
        self.assertIn("reason", projected)
        self.assertIsNone(projected["reason"])

    def test_explicit_replacement_cannot_disappear(self):
        projected = canonical_arguments(self.tool, dict(self.proposal, replaces_operation_id="previous"))
        self.assertEqual(projected["replaces_operation_id"], "previous")

    def test_unknown_trusted_none_is_not_silently_removed(self):
        projected = canonical_arguments(self.tool, dict(self.proposal, customer_id=None))
        self.assertIn("customer_id", projected)

    def test_raw_proposal_and_native_gate_arguments_are_not_mutated(self):
        raw = deepcopy(self.proposal)
        canonical_arguments(self.tool, self.proposal)
        self.assertEqual(self.proposal, raw)

    def test_fixture_clock_roundtrip_preserves_historical_and_future_instants(self):
        anchor = datetime(2026, 10, 10, tzinfo=timezone.utc)
        for offset in (-2400, -60, 0, 1200, 1800):
            expected = {"seconds_from_episode_start": offset}
            self.assertEqual(normalize(resolve(expected, anchor), anchor), expected)


if __name__ == "__main__":
    unittest.main()
