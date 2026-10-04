"""No model calls: verify frozen interface, complete policy, and bridge isolation."""

import asyncio
import hashlib
import inspect
import json
from pathlib import Path

from tau2.runner import build_environment
from tau2.data_model.message import ToolMessage

from bridge import ToolBridge, make_parlant_tool, policy_sections, SECTION_TOOLS
from policy_config import coverage


def main() -> None:
    environment = build_environment("retail")
    tools = environment.get_tools()
    names = [tool.name for tool in tools]
    assert len(names) == len(set(names)) == 16
    report = coverage(names, SECTION_TOOLS)
    assert not report["missing"]
    bridge = ToolBridge(180)
    for tool in tools:
        wrapper = make_parlant_tool(tool, bridge)
        assert wrapper is not None
        assert inspect.signature(tool) is not None
    policy = environment.get_policy()
    source = (Path(__file__).resolve().parents[1] / "tau2-bench/data/tau2/domains/retail/policy.md").read_text()
    assert policy == source
    sections = policy_sections(policy)
    assert "\n".join(section["text"] for section in sections).split() == policy.split()
    assert len(sections) == 7

    async def isolation() -> None:
        q1 = bridge.register("session-one")
        q2 = bridge.register("session-two")
        assert q1 is not q2
        assert bridge._queues["session-one"] is q1
        assert bridge._queues["session-two"] is q2
        first = asyncio.create_task(bridge.invoke(type("Context", (), {"session_id": "session-one"})(), "calculate", {"expression": "1+1"}))
        second = asyncio.create_task(bridge.invoke(type("Context", (), {"session_id": "session-two"})(), "calculate", {"expression": "2+2"}))
        await asyncio.sleep(0)
        ticket_one = q1.get_nowait()
        ticket_two = q2.get_nowait()
        assert ticket_one.call.arguments == {"expression": "1+1"}
        assert ticket_two.call.arguments == {"expression": "2+2"}
        bridge.fulfill(ticket_one, ToolMessage(id=ticket_one.call.id, role="tool", content='{"value": 2}'))
        bridge.fulfill(ticket_two, ToolMessage(id=ticket_two.call.id, role="tool", content='{"value": 4}'))
        assert (await first).data == {"value": 2}
        assert (await second).data == {"value": 4}
        bridge.unregister("session-one")
        assert "session-one" not in bridge._queues
        assert bridge._queues["session-two"] is q2
        bridge.unregister("session-two")
        assert not bridge._queues

    asyncio.run(isolation())
    result = {
        "status": "passed", "official_tool_count": len(names),
        "official_tool_names": sorted(names), "tool_coverage": report,
        "policy_sha256": hashlib.sha256(policy.encode()).hexdigest(),
        "policy_sections": [section["heading"] for section in sections],
        "bridge_sessions_isolated": True,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
