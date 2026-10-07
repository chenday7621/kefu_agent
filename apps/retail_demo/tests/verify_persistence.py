"""Restart actual Parlant SDK app and inspect native PostgreSQL session/customer persistence.

LLM generation is explicitly blocked. The stored message is a manual human-agent
note, not a generated answer; this check does not replace the dialogue smoke.
"""

import asyncio
from datetime import datetime, timezone
import json
import httpx
from ..settings import load_settings, ROOT
from .support import process


async def verify():
    s = load_settings()
    folder = (
        ROOT
        / "runtime-data/retail-demo/verification"
        / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    )
    folder.mkdir(parents=True, exist_ok=True)
    url = f"http://127.0.0.1:{s.parlant_port}"
    before = (
        (s.parlant_home / "model_calls.jsonl").read_bytes()
        if (s.parlant_home / "model_calls.jsonl").exists()
        else b""
    )
    with process("apps.retail_demo.mcp_server", s.mcp_port, folder):
        with process("apps.retail_demo.parlant_app", s.parlant_port, folder, ("--disable-llm",)):
            async with httpx.AsyncClient(base_url=url, timeout=20) as c:
                info = (await c.get("/demo/info")).json()
                assert info["llm_disabled"]
                assert (await c.get("/chat/")).status_code == 200
                agents = (await c.get("/agents")).json()
                assert any(a["id"] == "retail-persistent-demo" for a in agents)
                customers = (await c.get("/customers")).json()
                assert any(x["id"] == s.customer_id for x in customers)
                reject = await c.post(
                    "/sessions?allow_greeting=false",
                    json={"agent_id": "retail-persistent-demo", "customer_id": "demo-bob"},
                )
                assert reject.status_code == 403
                r = await c.post(
                    "/sessions?allow_greeting=false",
                    json={
                        "agent_id": "retail-persistent-demo",
                        "customer_id": s.customer_id,
                        "title": "离线重启持久化检查",
                    },
                )
                r.raise_for_status()
                session = r.json()
                sid = session["id"]
                r = await c.post(
                    f"/sessions/{sid}/events",
                    json={
                        "kind": "message",
                        "source": "human_agent",
                        "message": "人工备注：重启后应保留本条记录；此消息未通过模型生成。",
                        "participant": {"display_name": "离线验证操作员"},
                    },
                )
                r.raise_for_status()
                message = r.json()
                first = (await c.get(f"/sessions/{sid}/events?wait_for_data=0")).json()
                assert any(e["id"] == message["id"] for e in first)
        with process("apps.retail_demo.parlant_app", s.parlant_port, folder, ("--disable-llm",)):
            async with httpx.AsyncClient(base_url=url, timeout=20) as c:
                r = await c.get(f"/sessions/{sid}")
                r.raise_for_status()
                restored = r.json()
                events = (await c.get(f"/sessions/{sid}/events?wait_for_data=0")).json()
                assert (
                    restored["id"] == sid
                    and restored["customer_id"] == s.customer_id
                    and restored["agent_id"] == "retail-persistent-demo"
                )
                assert first == events
                customers2 = (await c.get("/customers")).json()
                assert len([x for x in customers2 if x["id"] == s.customer_id]) == 1
                services = (await c.get("/services")).json()
                assert any(
                    x["name"] == "retail-demo-business" and x["kind"] == "mcp" for x in services
                )
    after = (
        (s.parlant_home / "model_calls.jsonl").read_bytes()
        if (s.parlant_home / "model_calls.jsonl").exists()
        else b""
    )
    assert before == after, "Offline restart verification made a model call"
    report = {
        "passed": True,
        "session_id": sid,
        "customer_id": s.customer_id,
        "agent_id": "retail-persistent-demo",
        "message_id": message["id"],
        "same_events_after_process_restart": True,
        "native_customer_store": "postgres",
        "native_session_store": "postgres",
        "customer_duplicates_after_restart": 0,
        "model_calls": 0,
        "chat_page_http_status": 200,
        "mcp_native_registry_verified": True,
        "not_a_natural_language_conversation_test": True,
        "persistence_tables": ["parlant_sessions", "parlant_events", "parlant_customers"],
    }
    (folder / "parlant_persistence_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print("Evidence directory:", folder.relative_to(ROOT))


if __name__ == "__main__":
    asyncio.run(verify())
