"""Observation mapping only: original frozen score/criteria untouched.

Resolve snapshot tool arguments from the actual saved operation solely for the
legacy scorer's tool-goal comparison. Raw native events and args are saved unchanged.
"""
import copy
from ..eval_v1.scoring import score as original_score, selftest
from .reader import scoring_view

def score(task,initial,final):
    initial=scoring_view(initial); final=scoring_view(final)
    view=copy.deepcopy(final)
    ops={o["id"]:o for o in view["return_operations"]}
    for row in view["parlant_events"]:
        for t in row["doc"].get("data",{}).get("tool_calls",[]):
            if t.get("tool_id")=="retail-demo-business:submit_confirmed_return":
                args=t.get("arguments",{})
                op=ops.get(args.get("operation_id"))
                t["tool_id"]="retail-demo-business:create_return_request"
                if op:
                    t["arguments"]={"operation_id":op["id"],**{k:op[k] for k in ("order_id","item_id","quantity","reason")}}
    return original_score(task,initial,view)
