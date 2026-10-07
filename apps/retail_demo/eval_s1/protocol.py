"""Exactly the V1 legal tasks/script, preserving relative seed42/43 order."""
from ..eval_v1.protocol import SCENARIOS as ALL, plan as original_plan, visible_confirmation, clarification, REQUEST_ID, OP_ID, H, M
SCENARIOS=[s for s in ALL if s["group"]=="legal_return"]
def plan():
    return [p for p in original_plan() if p["scenario"]["group"]=="legal_return"]
