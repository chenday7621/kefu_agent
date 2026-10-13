"""A scripted USER actor, never a tool planner or gold-backed confirmer."""

from copy import deepcopy


class ScriptedUser:
    def __init__(self, opening, visible_goal, followups, family):
        # No final goal/expected DB/scorer/probe fields enter this object.
        self.opening, self.visible = opening, deepcopy(visible_goal)
        self.scripts, self.family = deepcopy(followups), family
        self.used_confirmations = set()
        self.last_consumed_sequence = 0

    def snapshot_matches_user_goal(self, snapshot):
        fields = ("order_id", "item_id", "quantity", "reason")
        return all(key in self.visible and snapshot.get(key) == self.visible[key] for key in fields)

    def react(self, events):
        fresh = [e for e in events if e["sequence"] > self.last_consumed_sequence]
        displays = [e for e in events if e["kind"] == "snapshot_displayed"]
        if self.scripts:
            script = self.scripts[0]
            trigger = script["trigger"]
            if trigger == "clarification_requested":
                reached = any(e["kind"] == trigger and e["field"] == script["field"] for e in fresh)
            elif trigger in ("snapshot_displayed", "latest_snapshot_displayed"):
                reached = any(e["kind"] == "snapshot_displayed" for e in fresh)
                if trigger == "latest_snapshot_displayed":
                    reached = (
                        reached
                        and bool(displays)
                        and self.snapshot_matches_user_goal(displays[-1]["snapshot"])
                    )
            elif trigger == "next_user_turn":
                reached = bool(fresh)
            else:
                raise ValueError("Unknown frozen script trigger")
            if not reached:
                return None
            if "sets" in script:
                self.visible.update(script["sets"])
                response = {
                    "message": script["message"],
                    "script_sets": script["sets"],
                    "confirm": False,
                }
            else:
                response = self.confirmation(displays)
                if response is None:
                    return None
            self.scripts.pop(0)
            self.last_consumed_sequence = max(e["sequence"] for e in events)
            return response
        return None

    def confirmation(self, displays):
        if self.visible.get("submission_allowed") is False or not displays:
            return None
        # Only the latest shown draft is eligible; no fallback to an older match.
        display = displays[-1]
        op = display["snapshot"]
        if not self.snapshot_matches_user_goal(op) or op["id"] in self.used_confirmations:
            return None
        # Taken from the actual agent's displayed phrase, never gold DB lookup.
        phrase = display.get("visible_confirmation_phrase")
        if not phrase:
            return None
        self.used_confirmations.add(op["id"])
        return {"message": phrase, "script_sets": None, "confirm": True, "operation_id": op["id"]}
