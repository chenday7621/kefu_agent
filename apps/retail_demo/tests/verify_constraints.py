"""Focused real PostgreSQL transaction/constraint checks; no model or mock."""

import json
from uuid import uuid4
import psycopg
from ..business import RetailService
from ..db import connect
from ..settings import load_settings
from .support import fixture_order


def verify():
    s = load_settings()
    service = RetailService(s.database_url, s.customer_id)
    oid, item = fixture_order(s, 2)
    preview = service.check_return_eligibility(oid, item, 1, "约束验证")["data"]
    opid = preview["operation_id"]
    wrong = service.confirm_operation(opid, "确认", "verification-human")
    assert not wrong["ok"] and wrong["error"]["code"] == "CONFIRMATION_MISMATCH"
    with connect(s.database_url) as c:
        assert (
            c.execute("SELECT confirmed_at FROM return_operations WHERE id=%s", (opid,)).fetchone()[
                "confirmed_at"
            ]
            is None
        )

    def rejected_insert(amount):
        try:
            with connect(s.database_url) as c:
                c.execute(
                    "INSERT INTO return_requests(id,operation_id,customer_id,order_id,item_id,quantity,reason,amount_cents) VALUES (%s,%s,%s,%s,%s,1,%s,%s)",
                    (uuid4(), opid, s.customer_id, oid, item, "约束验证", amount),
                )
        except psycopg.IntegrityError:
            pass
        else:
            raise AssertionError("Direct SQL bypassed return constraints")
        with connect(s.database_url) as c:
            assert (
                c.execute(
                    "SELECT reserved_return_quantity FROM order_items WHERE id=%s", (item,)
                ).fetchone()["reserved_return_quantity"]
                == 0
            )
            assert (
                c.execute(
                    "SELECT count(*) AS n FROM return_requests WHERE order_id=%s", (oid,)
                ).fetchone()["n"]
                == 0
            )

    rejected_insert(12900)  # no human confirmation, even with otherwise correct facts
    assert service.confirm_operation(opid, preview["confirmation_phrase"], "verification-human")[
        "ok"
    ]
    rejected_insert(1)  # backend integer-cents constraint; entire write rolls back
    response = service.create_return_request(opid, oid, item, 1, "约束验证")
    assert response["ok"]
    rid = response["data"]["request"]["id"]
    for statement in [
        "UPDATE return_requests SET amount_cents=1 WHERE id=%s",
        "DELETE FROM return_requests WHERE id=%s",
    ]:
        try:
            with connect(s.database_url) as c:
                c.execute(statement, (rid,))
        except psycopg.IntegrityError:
            pass
        else:
            raise AssertionError("Submitted immutable application changed")
    assert service.get_return_request(rid)["data"]["request"]["amount_cents"] == 12900
    # Expiry of a draft is tested on a separate synthetic line, not existing user data.
    oid2, item2 = fixture_order(s, 1)
    draft = service.check_return_eligibility(oid2, item2, 1, "过期验证")["data"]
    with connect(s.database_url) as c:
        c.execute(
            "UPDATE return_operations SET expires_at=now()-interval '1 second' WHERE id=%s",
            (draft["operation_id"],),
        )
    expired = service.confirm_operation(
        draft["operation_id"], draft["confirmation_phrase"], "verification-human"
    )
    assert not expired["ok"] and expired["error"]["code"] == "OPERATION_EXPIRED"
    print(
        json.dumps(
            {
                "passed": True,
                "checks": [
                    "wrong_confirmation_no_stamp",
                    "direct_unconfirmed_insert_rejected",
                    "incorrect_amount_rejected_without_reserved_quantity_change",
                    "submitted_update_delete_rejected",
                    "expired_draft_rejected",
                ],
                "persisted_request_id": rid,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    verify()
