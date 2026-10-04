"""Connect Parlant's engine to tau2's official half-duplex orchestrator.

Each Parlant tool awaits a ticket. The tau2 orchestrator executes that ticket
through its own Environment exactly once, then returns the ToolMessage here.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
import inspect
import json
from pathlib import Path
import queue
import threading
import time
from typing import Any
from uuid import uuid4

import httpx
import parlant.sdk as p
from parlant.adapters.nlp.deepseek_service import DeepSeekService
from parlant.adapters.nlp.hugging_face import HuggingFaceEmbedder
from parlant.core.meter import Meter
from parlant.core.engines.alpha.perceived_performance_policy import BasicPerceivedPerformancePolicy
from parlant.client import ParlantClient
from tau2.agent.base_agent import HalfDuplexAgent, ValidAgentInputMessage
from tau2.data_model.message import AssistantMessage, Message, ToolCall, ToolMessage, UserMessage
from tau2.environment.tool import Tool
from observe import Observer, argument_digest, result_summary
from policy_config import ASSOCIATIONS, coverage


AGENT_ID = "tau3-retail-parlant"
PORT = 18800
BASE_URL = f"http://127.0.0.1:{PORT}"


class AdapterError(RuntimeError):
    pass


class LocalRetailEmbedder(HuggingFaceEmbedder):
    def __init__(self, logger: p.Logger, tracer: p.Tracer, meter: Meter) -> None:
        super().__init__(
            logger=logger,
            tracer=tracer,
            meter=meter,
            model_name="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        )

    @property
    def dimensions(self) -> int:
        return 384

    @property
    def max_tokens(self) -> int:
        return 512


class RetailDeepSeekService(DeepSeekService):
    async def get_embedder(self, hints=None) -> p.Embedder:
        return LocalRetailEmbedder(self._logger, self._tracer, self._meter)


def retail_deepseek(container: p.Container) -> p.NLPService:
    if error := DeepSeekService.verify_environment():
        raise AdapterError(error)
    embedder = LocalRetailEmbedder(container[p.Logger], container[p.Tracer], container[Meter])
    container[LocalRetailEmbedder] = embedder
    return RetailDeepSeekService(container[p.Logger], container[p.Tracer], container[Meter])


class QuietPolicy(BasicPerceivedPerformancePolicy):
    async def is_preamble_required(self, context=None) -> bool:
        return False

    async def is_message_splitting_required(self, context, message: str) -> bool:
        return False


@dataclass
class Ticket:
    call: ToolCall
    future: asyncio.Future[ToolMessage]
    loop: asyncio.AbstractEventLoop
    ticket_id: str
    session_id: str
    parlant_call_id: str
    batch: int | str
    iteration: int | str


class ToolBridge:
    def __init__(self, wait_seconds: int, observer: Observer | None = None) -> None:
        self.wait_seconds = wait_seconds
        self.observer = observer
        self._lock = threading.Lock()
        self._queues: dict[str, queue.Queue[Ticket]] = {}

    def register(self, session_id: str) -> queue.Queue[Ticket]:
        with self._lock:
            if session_id in self._queues:
                raise AdapterError(f"Duplicate session: {session_id}")
            result: queue.Queue[Ticket] = queue.Queue()
            self._queues[session_id] = result
            return result

    def unregister(self, session_id: str) -> None:
        with self._lock:
            self._queues.pop(session_id, None)

    async def invoke(self, context: p.ToolContext, name: str, arguments: dict[str, Any]) -> p.ToolResult:
        session_id = str(context.session_id)
        with self._lock:
            outgoing = self._queues.get(session_id)
        if outgoing is None:
            raise AdapterError("Tool called without a registered benchmark session")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[ToolMessage] = loop.create_future()
        call = ToolCall(id=uuid4().hex, name=name, arguments=arguments, requestor="assistant")
        matched = self.observer.claim_parlant_call(session_id, name, arguments) if self.observer else {}
        ticket = Ticket(call=call, future=future, loop=loop, ticket_id=uuid4().hex,
                        session_id=session_id, parlant_call_id=matched.get("parlant_call_id", "unavailable"),
                        batch=matched.get("batch", "unavailable"), iteration=matched.get("iteration", "unavailable"))
        if self.observer:
            self.observer.record("bridge_ticket_created", session=session_id,
                                 ticket_id=ticket.ticket_id, parlant_call_id=ticket.parlant_call_id,
                                 official_call_id=call.id, batch=ticket.batch, iteration=ticket.iteration,
                                 tool_name=name, arguments=arguments,
                                 argument_sha256=argument_digest(name, arguments))
        outgoing.put(ticket)
        official_result = await asyncio.wait_for(future, timeout=self.wait_seconds)
        try:
            data = json.loads(official_result.content)
        except (ValueError, TypeError):
            data = official_result.content
        if self.observer:
            self.observer.record("bridge_result_returned", session=session_id,
                                 ticket_id=ticket.ticket_id, parlant_call_id=ticket.parlant_call_id,
                                 official_call_id=call.id, tool_name=name,
                                 result=data, result_summary=result_summary(name, data))
        return p.ToolResult(data=data)

    def fulfill(self, ticket: Ticket, result: ToolMessage) -> None:
        if result.id != ticket.call.id:
            raise AdapterError("Official tool result ID does not match pending Parlant call")
        if ticket.future.done():
            raise AdapterError("Official tool result was delivered twice")
        if self.observer:
            self.observer.record("official_result_fulfilled", session=ticket.session_id,
                                 ticket_id=ticket.ticket_id, parlant_call_id=ticket.parlant_call_id,
                                 official_call_id=result.id, tool_name=ticket.call.name,
                                 content=result.content, error=result.error)
        ticket.loop.call_soon_threadsafe(ticket.future.set_result, result)


def make_parlant_tool(official: Tool, bridge: ToolBridge) -> p.ToolEntry:
    name = official.name

    async def invoke(context: p.ToolContext, **kwargs: Any) -> p.ToolResult:
        return await bridge.invoke(context, name, kwargs)

    invoke.__name__ = name
    invoke.__doc__ = official.__doc__ or official.short_desc
    source_signature = inspect.signature(official)
    parameters = [
        inspect.Parameter("context", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=p.ToolContext),
        *source_signature.parameters.values(),
    ]
    invoke.__signature__ = inspect.Signature(parameters, return_annotation=p.ToolResult)  # type: ignore[attr-defined]
    return p.tool(name=name)(invoke)


def policy_sections(policy: str) -> list[dict[str, Any]]:
    lines = policy.splitlines()
    starts = [i for i, line in enumerate(lines) if line.startswith("## ")]
    sections = []
    for begin, end in zip([0, *starts], [*starts, len(lines)]):
        if begin == end:
            continue
        body = "\n".join(lines[begin:end]).strip()
        heading = "Overview" if begin == 0 else lines[begin].removeprefix("## ")
        sections.append({"heading": heading, "start_line": begin + 1, "end_line": end, "text": body})
    return sections


CONDITIONS = {
    "Cancel pending order": "The customer wants to cancel a pending order",
    "Modify pending order": "The customer wants to modify a pending order, including address, payment, or items",
    "Return delivered order": "The customer wants to return items from a delivered order",
    "Exchange delivered order": "The customer wants to exchange items from a delivered order",
}

SECTION_TOOLS = {
    "Cancel pending order": ["cancel_pending_order"],
    "Modify pending order": [
        "modify_pending_order_address", "modify_pending_order_items", "modify_pending_order_payment",
    ],
    "Return delivered order": ["return_delivered_order_items"],
    "Exchange delivered order": ["exchange_delivered_order_items"],
}


@dataclass
class AgentState:
    session_id: str
    outgoing: queue.Queue[Ticket]
    last_offset: int = -1
    pending: Ticket | None = None
    seen_results: set[str] = field(default_factory=set)
    messages: list[str] = field(default_factory=list)
    turn_started: float | None = None
    turn_start_utc: str | None = None


class ParlantRetailAgent(HalfDuplexAgent[AgentState]):
    def __init__(self, tools: list[Tool], domain_policy: str, bridge: ToolBridge, observer: Observer | None = None):
        super().__init__(tools=tools, domain_policy=domain_policy)
        self.bridge = bridge
        self.observer = observer
        self.client = ParlantClient(base_url=BASE_URL)

    def get_init_state(self, message_history: list[Message] | None = None) -> AgentState:
        # Tau's default initial assistant greeting is a protocol seed, not a
        # model-generated message. Selected tasks have no prior user history.
        if any(isinstance(m, UserMessage) for m in (message_history or [])):
            raise AdapterError("Pre-existing user history requires explicit Parlant replay")
        session = self.client.sessions.create(agent_id=AGENT_ID, allow_greeting=False)
        outgoing = self.bridge.register(session.id)
        if self.observer:
            self.observer.set_session(session.id)
        return AgentState(session_id=session.id, outgoing=outgoing)

    def generate_next_message(
        self, message: ValidAgentInputMessage, state: AgentState
    ) -> tuple[AssistantMessage, AgentState]:
        if isinstance(message, UserMessage):
            if message.is_tool_call():
                raise AdapterError("User tool calls are unsupported by the Retail text split")
            if self.observer:
                self.observer.next_turn()
                self.observer.record("user_message_received", session=state.session_id,
                                     message=message.model_dump(mode="json"))
            state.turn_started = time.monotonic()
            state.turn_start_utc = datetime.now(timezone.utc).isoformat()
            self.client.sessions.create_event(
                session_id=state.session_id,
                kind="message",
                source="customer",
                message=message.content or "",
            )
        elif isinstance(message, ToolMessage):
            if state.pending is None:
                raise AdapterError("Unexpected official tool result")
            if message.id in state.seen_results:
                raise AdapterError("Duplicate official tool result")
            state.seen_results.add(message.id)
            if self.observer:
                self.observer.record("official_tool_message_received", session=state.session_id,
                                     message=message.model_dump(mode="json"))
            self.bridge.fulfill(state.pending, message)
            state.pending = None
        else:
            raise AdapterError(f"Unsupported orchestrator input: {type(message).__name__}")
        return self._next_output(state), state

    def _next_output(self, state: AgentState) -> AssistantMessage:
        deadline = time.monotonic() + self.bridge.wait_seconds
        while time.monotonic() < deadline:
            try:
                ticket = state.outgoing.get_nowait()
            except queue.Empty:
                ticket = None
            if ticket is not None:
                if state.pending is not None:
                    raise AdapterError("More than one pending tool call")
                state.pending = ticket
                output = AssistantMessage(role="assistant", content=None, tool_calls=[ticket.call])
                if self.observer:
                    self.observer.record("assistant_tool_call_returned", session=state.session_id,
                                         message=output.model_dump(mode="json"))
                return output

            events = self.client.sessions.list_events(
                session_id=state.session_id,
                min_offset=state.last_offset + 1,
                wait_for_data=0,
            )
            ready = False
            for event in events:
                state.last_offset = max(state.last_offset, event.offset)
                if self.observer:
                    self.observer.record("parlant_event", session=state.session_id,
                                         event=event.model_dump(mode="json"))
                if event.kind == "message" and event.source == "ai_agent":
                    content = (event.data or {}).get("message", "")
                    if content:
                        state.messages.append(content)
                elif event.kind == "status" and (event.data or {}).get("status") == "ready":
                    ready = True
                elif event.kind == "status" and (event.data or {}).get("status") == "error":
                    raise AdapterError(f"Parlant session error: {(event.data or {}).get('data')}")
            if ready:
                if state.messages:
                    content = "\n".join(state.messages)
                    state.messages.clear()
                    if self.observer and state.turn_started is not None:
                        self.observer.record("customer_turn_complete", session=state.session_id,
                                             start_utc=state.turn_start_utc,
                                             end_utc=datetime.now(timezone.utc).isoformat(),
                                             full_response_seconds=time.monotonic()-state.turn_started,
                                             response_characters=len(content))
                        state.turn_started = None
                    output = AssistantMessage(role="assistant", content=content)
                    if self.observer:
                        self.observer.record("assistant_message_returned", session=state.session_id,
                                             message=output.model_dump(mode="json"))
                    return output
                raise AdapterError("Parlant completed a turn without a message or tool call")
            time.sleep(0.25)
        raise TimeoutError("Timed out waiting for Parlant's engine")

    def raw_events(self, state: AgentState) -> list[dict[str, Any]]:
        return [e.model_dump(mode="json") for e in self.client.sessions.list_events(state.session_id)]


class ParlantHost:
    def __init__(self, official_tools: list[Tool], policy: str, bridge: ToolBridge):
        self.official_tools = official_tools
        self.policy = policy
        self.bridge = bridge
        self.thread: threading.Thread | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.task: asyncio.Task | None = None
        self.error: BaseException | None = None
        self.policy_map: list[dict[str, Any]] = []
        self.coverage_report: dict[str, Any] = {}

    async def _serve(self) -> None:
        wrappers = {t.name: make_parlant_tool(t, self.bridge) for t in self.official_tools}
        self.coverage_report = coverage(list(wrappers), SECTION_TOOLS)
        if self.coverage_report["missing"]:
            raise AdapterError(f"Official Retail tools without guideline association: {self.coverage_report['missing']}")
        sections = policy_sections(self.policy)
        async with p.Server(
            host="127.0.0.1", port=PORT, tool_service_port=18818,
            nlp_service=retail_deepseek,
        ) as server:
            agent = await server.create_agent(
                id=AGENT_ID,
                name="Retail customer support",
                description="Serve retail customers in English using the official Retail policy and tools.",
                perceived_performance_policy=QuietPolicy(),
            )
            for section in sections:
                heading = section["heading"]
                if heading in ("Overview", "Domain basic", "Generic action rules"):
                    guideline = await agent.create_guideline(
                        action=section["text"], matcher=p.MATCH_ALWAYS
                    )
                else:
                    guideline = await agent.create_guideline(
                        condition=CONDITIONS[heading], action=section["text"],
                        tools=[wrappers[name] for name in SECTION_TOOLS[heading]],
                    )
                self.policy_map.append({
                    "guideline_id": str(guideline.id),
                    "source": "data/tau2/domains/retail/policy.md",
                    "heading": heading,
                    "start_line": section["start_line"],
                    "end_line": section["end_line"],
                    "source_kind": "policy_requirement",
                    "associated_tools": SECTION_TOOLS.get(heading, []),
                })
            for item in ASSOCIATIONS:
                guideline = await agent.create_guideline(
                    condition=item["condition"], action=item["action"],
                    tools=[wrappers[name] for name in item["tools"]],
                )
                self.policy_map.append({
                    "guideline_id": str(guideline.id), "key": item["key"],
                    "source": item["source"], "source_kind": "official_policy_tool_association",
                    "associated_tools": list(item["tools"]),
                })

    def _run_thread(self) -> None:
        loop = asyncio.new_event_loop()
        self.loop = loop
        asyncio.set_event_loop(loop)
        self.task = loop.create_task(self._serve())
        try:
            loop.run_until_complete(self.task)
        except asyncio.CancelledError:
            pass
        except BaseException as exc:
            self.error = exc
        finally:
            loop.close()

    def start(self, timeout_seconds: int = 600) -> None:
        self.thread = threading.Thread(target=self._run_thread, name="parlant-retail", daemon=True)
        self.thread.start()
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if self.error:
                raise AdapterError(f"Parlant startup failed: {self.error}") from self.error
            try:
                response = httpx.get(f"{BASE_URL}/healthz", timeout=2)
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(1)
        raise TimeoutError("Parlant Retail server did not become healthy")

    def stop(self) -> None:
        if self.loop and self.task and not self.task.done():
            self.loop.call_soon_threadsafe(self.task.cancel)
        if self.thread:
            self.thread.join(timeout=20)
