"""Strict restart checks with individually proven Outbox-only tails."""


def compare_snapshots(before, after):
    assert after["session"] == before["session"], "Full native session state changed"
    assert after["current_operation"] == before["current_operation"], "Current operation changed"
    prefix = before["events"]
    assert after["events"][: len(prefix)] == prefix, "Original event content/order changed"
    tail = after["events"][len(prefix) :]
    pending = {r["id"]: r for r in before["outbox"] if r["status"] == "pending"}
    delivered = {r["id"]: r for r in after["outbox"] if r["status"] == "delivered"}
    identified = []
    for i, event in enumerate(tail):
        meta = event["metadata"]
        oid = meta.get("outbox_id")
        assert oid in pending and oid in delivered, "Undeclared tail event"
        expected = pending[oid]
        assert oid not in identified, "Duplicate notification tail"
        assert event["id"] == delivered[oid]["receipt_event_id"]
        assert event["kind"] == "message" and event["source"] == "ai_agent" and not event["deleted"]
        assert meta.get("deterministic_operation_receipt") is True
        assert (
            meta.get("request_id") == expected["request_id"]
            and meta.get("operation_id") == expected["operation_id"]
        )
        assert event["offset"] == before["next_offset"] + i
        assert (
            expected["request_id"] in event["data"]["message"]
            and "尚未退款" in event["data"]["message"]
        )
        identified.append(oid)
    assert after["next_offset"] == before["next_offset"] + len(tail)
    return {
        "original_event_count": len(prefix),
        "identified_outbox_tail_ids": identified,
        "full_session_state_equal": True,
    }
