"""One bounded real Parlant/DeepSeek conversation, separate from offline checks.

The scripted customer copies a confirmation actually shown by the assistant.
No simulated-user model, canned assistant answer or benchmark data is used.
"""

import asyncio
from datetime import datetime, timezone
import json
import time
import httpx
from ..business import confirmation_phrase
from ..db import connect
from ..settings import ROOT, load_settings
from .support import fixture_order, process
from ..snapshot_assertions import compare_snapshots


async def verify():
    s = load_settings()
    folder = (
        ROOT
        / "runtime-data/retail-demo/verification"
        / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    )
    folder.mkdir(parents=True, exist_ok=True)
    order_id, item_id = fixture_order(s, 2)
    report = {
        "passed": False,
        "synthetic_order_id": order_id,
        "item_id": item_id,
        "turns": [],
        "turn_timeout_seconds": 180,
        "retries": 0,
    }
    url = f"http://127.0.0.1:{s.parlant_port}"
    api_log = s.parlant_home / "api_calls.jsonl"
    before = len(api_log.read_text().splitlines()) if api_log.exists() else 0

    def save():
        (folder / "dialogue_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        )

    async def events(c, sid):
        r = await c.get(f"/sessions/{sid}/events?wait_for_data=0")
        r.raise_for_status()
        result = r.json()
        (folder / "dialogue_events.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        )
        return result

    async def turn(c, sid, question):
        start = time.monotonic()
        r = await c.post(
            f"/sessions/{sid}/events",
            json={"kind": "message", "source": "customer", "message": question},
        )
        r.raise_for_status()
        offset = r.json()["offset"]
        report["turns"].append({"query": question, "customer_event_id": r.json()["id"]})
        save()
        while time.monotonic() - start < 180:
            batch = [e for e in await events(c, sid) if e["offset"] > offset]
            answers = [e for e in batch if e["kind"] == "message" and e["source"] == "ai_agent"]
            failures = [
                e for e in batch if e["kind"] == "status" and e["data"].get("status") == "error"
            ]
            if failures:
                raise RuntimeError(
                    "Parlant engine emitted an error; see dialogue events and application log"
                )
            ready = any(
                e["kind"] == "status"
                and e["data"].get("status") == "ready"
                and answers
                and e["trace_id"] == answers[-1]["trace_id"]
                for e in batch
            )
            if answers and ready:
                text = "\n".join(e["data"].get("message", "") for e in answers)
                report["turns"][-1].update(answer=text, seconds=time.monotonic() - start)
                save()
                return text
            await asyncio.sleep(1)
        raise TimeoutError("Target answer exceeded 180 seconds; no retry")

    try:
        with process("apps.retail_demo.mcp_server", s.mcp_port, folder):
            with process("apps.retail_demo.parlant_app", s.parlant_port, folder):
                async with httpx.AsyncClient(base_url=url, timeout=20) as c:
                    r = await c.post(
                        "/sessions?allow_greeting=false",
                        json={
                            "agent_id": "retail-persistent-demo",
                            "customer_id": s.customer_id,
                            "title": "真实三轮订单与退货冒烟",
                        },
                    )
                    r.raise_for_status()
                    sid = r.json()["id"]
                    report["session_id"] = sid
                    save()
                    await turn(c, sid, f"请查询我的订单 {order_id}，告诉我商品明细、数量和状态。")
                    answer = await turn(
                        c,
                        sid,
                        f"我要退订单 {order_id} 的明细 {item_id} 中1件，原因是尺寸不合适。请先检查资格并展示申请内容，等我明确确认后再提交。",
                    )
                    with connect(s.database_url) as conn:
                        ops = conn.execute(
                            "SELECT * FROM return_operations WHERE order_id=%s ORDER BY created_at DESC",
                            (order_id,),
                        ).fetchall()
                        assert ops, "No eligibility preparation was actually written"
                        op = ops[0]
                        phrase = confirmation_phrase(op)
                        assert phrase in answer, (
                            "Assistant did not display the backend confirmation phrase; do not invent one in the customer script"
                        )
                        assert (
                            conn.execute(
                                "SELECT count(*) AS n FROM return_requests WHERE order_id=%s",
                                (order_id,),
                            ).fetchone()["n"]
                            == 0
                        ), "Application created before human confirmation"
                        assert op["quantity"] == 1 and op["reason"] == "尺寸不合适"
                    report["no_submission_before_confirmation"] = True
                    report["operation_id"] = str(op["id"])
                    save()
                    final = await turn(c, sid, phrase)
                    with connect(s.database_url) as conn:
                        rows = conn.execute(
                            "SELECT * FROM return_requests WHERE order_id=%s", (order_id,)
                        ).fetchall()
                        assert len(rows) == 1, (
                            "Confirmed conversation did not create exactly one application"
                        )
                        application = rows[0]
                        assert (
                            application["operation_id"] == op["id"]
                            and application["amount_cents"] == 12900
                            and application["status"] == "submitted"
                        )
                        logs = conn.execute(
                            "SELECT action,details FROM operation_logs WHERE operation_id=%s ORDER BY id",
                            (op["id"],),
                        ).fetchall()
                        assert [x["action"] for x in logs] == ["prepared", "confirmed", "submitted"]
                        assert logs[1]["details"]["source"] == f"parlant-human-message:{sid}"
                        assert str(application["id"]) in final, (
                            "Final assistant answer omitted the real request ID"
                        )
                    snapshot_response = await c.post(f"/demo/sessions/{sid}/snapshot", timeout=70)
                    snapshot_response.raise_for_status()
                    snapshot = snapshot_response.json()
                    (folder / "before_restart_snapshot.json").write_text(
                        json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n"
                    )
                    # DTO transcript is auxiliary. Full native snapshot is captured
                    # after the actual processing task finishes, including state.
                    transcript = await events(c, sid)
                    tools = [
                        t
                        for e in transcript
                        if e["kind"] == "tool"
                        for t in e["data"].get("tool_calls", [])
                    ]
                    tool_ids = [str(t.get("tool_id")) for t in tools]
                    assert any("get_order_details" in t for t in tool_ids)
                    assert any("check_return_eligibility" in t for t in tool_ids)
                    assert any("create_return_request" in t for t in tool_ids), (
                        "A DB write alone is not proof of a Parlant MCP invocation"
                    )
                    report.update(
                        request_id=str(application["id"]),
                        amount_cents=application["amount_cents"],
                        status=application["status"],
                        operation_logs=logs,
                        actual_parlant_tool_ids=tool_ids,
                    )
                    (folder / "before_restart_events.json").write_text(
                        json.dumps(transcript, ensure_ascii=False, indent=2) + "\n"
                    )
            # Restart without generation; restore the actual customer/AI transcript.
            with process(
                "apps.retail_demo.parlant_app", s.parlant_port, folder, ("--disable-llm",)
            ):
                async with httpx.AsyncClient(base_url=url, timeout=20) as c:
                    restored = await events(c, sid)
                    (folder / "after_restart_events.json").write_text(
                        json.dumps(restored, ensure_ascii=False, indent=2) + "\n"
                    )
                    response = await c.post(f"/demo/sessions/{sid}/snapshot", timeout=70)
                    response.raise_for_status()
                    after_snapshot = response.json()
                    (folder / "after_restart_snapshot.json").write_text(
                        json.dumps(after_snapshot, ensure_ascii=False, indent=2) + "\n"
                    )
                    report["snapshot_comparison"] = compare_snapshots(snapshot, after_snapshot)
                    report["real_dialogue_transcript_restored_after_restart"] = True
        report["passed"] = True
    except Exception as exc:
        report.update(error_type=type(exc).__name__, error=str(exc))
        raise
    finally:
        calls = (
            [json.loads(line) for line in api_log.read_text().splitlines()[before:]]
            if api_log.exists()
            else []
        )
        (folder / "api_calls.json").write_text(
            json.dumps(calls, ensure_ascii=False, indent=2) + "\n"
        )
        report["api_client_invocations"] = len(calls)
        report["api_client_invocations_note"] = (
            "SDK create invocations; transport-internal retries are not separately counted"
        )
        report["input_tokens"] = sum(
            x["input_tokens"] for x in calls if x.get("input_tokens") is not None
        )
        report["output_tokens"] = sum(
            x["output_tokens"] for x in calls if x.get("output_tokens") is not None
        )
        report["cache_hit_tokens"] = (
            sum(x["cache_hit_tokens"] for x in calls if x.get("cache_hit_tokens") is not None)
            if calls and all(x.get("cache_hit_tokens") is not None for x in calls)
            else None
        )
        report["cache_miss_tokens"] = (
            sum(x["cache_miss_tokens"] for x in calls if x.get("cache_miss_tokens") is not None)
            if calls and all(x.get("cache_miss_tokens") is not None for x in calls)
            else None
        )
        save()
        print(json.dumps(report, ensure_ascii=False, indent=2))
        print("Evidence directory:", folder.relative_to(ROOT))


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(verify(), timeout=720))
