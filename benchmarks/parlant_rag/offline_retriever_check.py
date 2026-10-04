"""Exercise v3.3.2 native Retriever hooks without invoking any model."""
import asyncio,dataclasses,json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from common import *

async def main():
    import parlant.sdk as p
    from parlant.core.engines.alpha.hooks import EngineHooks
    from parlant.core.loggers import Logger,CompositeLogger
    from parlant.core.tracer import Tracer
    from parlant.core.sessions import SessionId
    from parlant.core.agents import AgentId
    from parlant.core.customers import CustomerId
    hooks=EngineHooks();logger=CompositeLogger([]);tracer=NS(trace_id='offline-native-retriever')
    c={EngineHooks:hooks,Logger:logger,Tracer:tracer}
    server=object.__new__(p.Server);server._container=c
    server.get_agent=AsyncMock(return_value=NS(id=AgentId('offline')))
    server.get_customer=AsyncMock(return_value=NS(id=CustomerId('guest')))
    bm=BM25.load();task=readl(DATA/'runtime_dev.jsonl')[0];query=task['input'][-1]['text'];hits=bm.search(query,5)
    seen=[]
    async def retriever(ctx):
        seen.append(ctx.interaction.last_customer_message.content)
        return p.RetrieverResult(data={'passages':bm.search(ctx.interaction.last_customer_message.content,5)})
    server._retrievers={AgentId('offline'):{'cloud_bm25_r0':retriever}}
    await server._setup_retrievers()
    async def emit(trace,data):return NS(data=data)
    ctx=NS(agent=NS(id=AgentId('offline')),customer=NS(id=CustomerId('guest')),session=NS(id=SessionId('offline-session'),metadata={},labels=set(),mode='auto',title=None),state=NS(context_variables=[],tool_events=[]),interaction=NS(last_customer_message=NS(content=query)),tracer=tracer,response_event_emitter=NS(emit_tool_event=emit))
    await hooks.on_preparing[0](ctx,None,None)
    await hooks.on_generating_messages[0](ctx,None,None)
    assert seen==[query] and len(ctx.state.tool_events)==1
    call=ctx.state.tool_events[0].data['tool_calls'][0]
    assert call['tool_id'].endswith(':cloud_bm25_r0')
    assert call['result']['data']['passages']==hits
    assert call['result']['control']['lifespan']=='response'
    # Second generating hook cannot re-emit the same retrieval.
    await hooks.on_generating_messages[0](ctx,None,None);assert len(ctx.state.tool_events)==1
    check={'native_v332_hook_transfer':True,'exact_five_passage_id_and_text':True,'current_question_only':True,'response_lifespan':True,'emit_once':True,'paid_calls':0,'final_generation_prompt':'verified during actual smoke separately'}
    write(REVIEW/'OFFLINE_RETRIEVER_CHECK.json',check);print(check)
if __name__=='__main__':asyncio.run(main())
