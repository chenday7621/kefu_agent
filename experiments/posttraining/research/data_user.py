"""Independent synthetic user: derives selection only from actual tool facts."""

from copy import deepcopy


def resolved_user_fields(known, events):
    fields = deepcopy(known)
    orders, detail_rows = [], []
    for event in events:
        if event["kind"] != "tool_result" or event["result"].get("ok") is not True:
            continue
        data = event["result"]["data"]
        if "orders" in data:
            orders = data["orders"]
        if "order" in data and "items" in data:
            detail_rows.append(data)
    if "order_id" not in fields and fields.get("order_selector") == "latest_delivered":
        choices = [o for o in orders if o["status"] == "delivered"]
        if choices:
            latest = max(o["delivered_at"] for o in choices)
            unique = [o for o in choices if o["delivered_at"] == latest]
            if len(unique) == 1:
                fields["order_id"] = unique[0]["id"]
    details = [d for d in detail_rows if d["order"]["id"] == fields.get("order_id")]
    if details:
        choices = [i for i in details[-1]["items"] if i["id"] == fields.get("item_id")
                   or ("item_id" not in fields and i["product_name"] == fields.get("product_name"))]
        if len(choices) == 1:
            fields["item_id"] = choices[0]["id"]
            if fields.get("quantity_selector") == "all_available":
                fields["quantity"] = choices[0]["available_return_quantity"]
    return fields


class DataUser:
    def __init__(self, task):
        self.known = deepcopy(task["visible_goal_fields"])
        self.scripts = deepcopy(task["user_followups"])
        self.consumed = 0
        self.confirmed = set()

    def react(self, events, actual_reply_text):
        if not self.scripts:
            return None
        fresh = [e for e in events if e["sequence"] > self.consumed]
        script = self.scripts[0]
        if script["trigger"] == "clarification_requested":
            reached = any(e["kind"] == "clarification_requested" and e["field"] == script["field"]
                          for e in fresh)
        elif script["trigger"] == "order_read_visible":
            # A returned observation alone is not a customer-visible answer.
            order = self.known["order_id"]
            reached = any(e["kind"] == "tool_result" and e["result"].get("ok") is True
                          and e["result"]["data"].get("order", {}).get("id") == order
                          for e in fresh) and order in actual_reply_text
        elif script["trigger"] == "latest_snapshot_displayed":
            displays = [e for e in events if e["kind"] == "snapshot_displayed"]
            target = resolved_user_fields(self.known, events)
            reached = bool(displays) and displays[-1] in fresh and all(
                k in target and displays[-1]["snapshot"].get(k) == target[k]
                for k in ("order_id", "item_id", "quantity", "reason"))
        else:
            raise ValueError("Unfrozen training user trigger")
        if not reached:
            return None
        if "sets" in script:
            self.known.update(script["sets"])
            response = {"message": script["message"], "script_sets": script["sets"], "confirm": False}
        else:
            display = displays[-1]
            phrase = display.get("visible_confirmation_phrase")
            oid = display["operation_id"]
            if not phrase or oid in self.confirmed or self.known.get("submission_allowed") is False:
                return None
            self.confirmed.add(oid)
            response = {"message": phrase, "script_sets": None, "confirm": True}
        self.scripts.pop(0)
        self.consumed = max(e["sequence"] for e in events)
        return response
