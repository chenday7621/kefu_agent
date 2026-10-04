"""Offline checks for terminal cleanup and attempt/session attribution."""

import asyncio
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent/"configs/CONTROL"))
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from bridge import AdapterError, ParlantHost, ToolBridge
from observe import Observer
from run import Meter


class FakeBackgroundTasks:
    def __init__(self):
        self._lock = asyncio.Lock()
        self._tasks = {}

    async def cancel(self, *, tag, reason):
        task = self._tasks[tag]
        task.cancel(reason)


async def check_cleanup(root: Path) -> dict:
    observer = Observer()
    meter = Meter(observer)
    token_one = observer.begin_attempt("old", "attempt-old", root / "old.jsonl")
    observer.register_session("session-old")
    bridge = ToolBridge(180, observer)
    queue_old = bridge.register("session-old")

    # A pending call at max_steps must be discarded, and the Parlant coroutine
    # must stop before the next official environment is started.
    pending = asyncio.create_task(bridge.invoke(SimpleNamespace(session_id="session-old"), "calculate", {"expression": "1+1"}))
    await asyncio.sleep(0)
    ticket = queue_old.get_nowait()
    background = FakeBackgroundTasks()
    late_action = []

    async def old_processing():
        try:
            await asyncio.sleep(3600)
            late_action.append("forbidden")
        except asyncio.CancelledError:
            raise

    background._tasks["process-session(session-old)"] = asyncio.create_task(old_processing())
    host = ParlantHost([], "", bridge)
    host.background_tasks = background
    bridge.close_session("session-old")
    status = await host._cancel_session_processing("session-old", 1)
    assert status["state"] == "cancelled_after_terminal"
    try:
        await pending
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("Pending tool call continued after terminal cleanup")
    assert not late_action
    assert queue_old.empty()
    try:
        await bridge.invoke(SimpleNamespace(session_id="session-old"), "calculate", {"expression": "2+2"})
    except AdapterError:
        pass
    else:
        raise AssertionError("Closed session accepted a late tool call")
    observer.set_phase("old", "attempt-old", "closed")
    observer.end_attempt(token_one)

    token_two = observer.begin_attempt("new", "attempt-new", root / "new.jsonl")
    observer.register_session("session-new")
    bridge.register("session-new")
    meter.add("customer_parlant", success=True, input_tokens=11, output_tokens=2)
    old_token = observer.bind_session("session-old")
    observer.record("late_prior_session_event")
    meter.add("customer_parlant", success=True, input_tokens=13, output_tokens=3)
    observer.unbind_session(old_token)
    assert meter.task("new", "attempt-new")["customer_parlant"]["input_tokens"] == 11
    assert meter.task("old", "attempt-old")["customer_parlant"]["input_tokens"] == 13
    assert any(row["kind"] == "late_prior_session_event" for row in observer.task_records("old", "attempt-old"))
    assert not any(row["kind"] == "late_prior_session_event" for row in observer.task_records("new", "attempt-new"))
    assert any(json.loads(line)["kind"] == "late_prior_session_event" for line in (root / "old.jsonl").read_text().splitlines())
    bridge.close_session("session-new")
    observer.end_attempt(token_two)
    return {"pending_tool_cancelled": True, "post_terminal_tool_blocked": True,
            "background_task_stopped": True, "late_event_and_tokens_original_attempt": True}


if __name__ == "__main__":
    with TemporaryDirectory() as folder:
        print(json.dumps(asyncio.run(check_cleanup(Path(folder))), indent=2))
