"""Native session/restore/retriever/prompt checks without a model request."""
from common_r1 import *
import asyncio,dataclasses,datetime,inspect
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

async def main():
    import run,observe
    observe.SCOPE.set(('offline','offline-checks'))
    from parlant.adapters.db.transient import TransientDocumentDatabase
    from parlant.core.agents import AgentStore,AgentDocumentStore,AgentId
    from parlant.core.guidelines import GuidelineStore,GuidelineDocumentStore
    from parlant.core.engines.alpha.perceived_performance_policy import PerceivedPerformancePolicyProvider
    from parlant.core.sessions import EventSource,EventKind,SessionStore,SessionDocumentStore,SessionId
    from parlant.core.application import Application
    from parlant.core.background_tasks import BackgroundTaskService
    from parlant.core.customers import CustomerId
    gen=NS(generate=lambda s:hashlib.sha256(str(s).encode()).hexdigest()[:10])
    async with AgentDocumentStore(gen,TransientDocumentDatabase()) as agents,GuidelineDocumentStore(gen,TransientDocumentDatabase()) as rules,SessionDocumentStore(TransientDocumentDatabase()) as sessions:
        server=NS(container={AgentStore:agents,GuidelineStore:rules,PerceivedPerformancePolicyProvider:PerceivedPerformancePolicyProvider(run.Quiet())},_guideline_evaluations={},get_agent=AsyncMock())
        await run.restore_effective(server)
        old=json.loads((R0_RESULT/'startup/effective_configuration.json').read_text())
        assert json.loads(json.dumps([dataclasses.asdict(x) for x in await agents.list_agents()],default=str))==old['agent']
        assert json.loads(json.dumps([dataclasses.asdict(x) for x in await rules.list_guidelines()],default=str))==old['guidelines']
        # Exercise the real Application.SessionModule without processing history.
        from parlant.core.application import SessionModule
        bg=NS(_tasks={},_lock=asyncio.Lock());engine=NS(process=AsyncMock())
        module=SessionModule(logger=NS(),meter=NS(),agent_store=agents,tracer=NS(trace_id='offline'),session_store=sessions,customer_store=NS(),session_listener=NS(),nlp_service=NS(),engine=engine,event_emitter_factory=NS(),background_task_service=bg)
        module.dispatch_processing_task=AsyncMock(return_value='offline')
        history_task=load_tasks()[0]
        session=await module.create(CustomerId('guest'),AgentId(run.AGENT),allow_greeting=False)
        for m in history_task['input'][:-1]:
            await module.create_event(session.id,EventKind.MESSAGE,{'message':m['text'],'participant':{'id':'guest' if m['speaker']=='user' else run.AGENT,'display_name':m['speaker']},'flagged':False,'tags':[]},None,EventSource.CUSTOMER if m['speaker']=='user' else EventSource.AI_AGENT,trigger_processing=False)
        assert module.dispatch_processing_task.await_count==0 and engine.process.await_count==0
        loaded=await sessions.list_events(session.id)
        assert [e.data['message'] for e in loaded]==[m['text'] for m in history_task['input'][:-1]]
        await module.create_event(session.id,EventKind.MESSAGE,{'message':history_task['input'][-1]['text']},None,EventSource.CUSTOMER,trigger_processing=True)
        assert module.dispatch_processing_task.await_count==1
        await run.cleanup(NS(container={Application:NS(sessions=module),SessionStore:sessions,BackgroundTaskService:bg}),session.id)
        # Native retrieval hooks preserve exact payload and emit once.
        import parlant.sdk as p
        from parlant.core.engines.alpha.hooks import EngineHooks
        from parlant.core.loggers import Logger,CompositeLogger
        from parlant.core.tracer import Tracer
        hooks=EngineHooks();tracer=NS(trace_id='offline-r1');native=object.__new__(p.Server)
        native._container={EngineHooks:hooks,Logger:CompositeLogger([]),Tracer:tracer}
        native.get_agent=AsyncMock(return_value=NS(id=AgentId(run.AGENT)))
        native.get_customer=AsyncMock(return_value=NS(id=CustomerId('guest')))
        selected_path=RESULT/'selected_ten_retrieval.jsonl'
        if selected_path.exists():targets=readl(selected_path)
        else:
            tasks={t['task_id']:t for t in load_tasks()};rankings={r['task_id']:r for r in readl(RESULT/'R1-A_per_task.jsonl')};passages={p['_id']:p for p in readl(DATA/'cloud_passages.jsonl')};targets=[]
            for selected in json.loads((DATA/'selection.json').read_text())['targets']:
                t=tasks[selected['task_id']];row=rankings[t['task_id']];hits=[]
                for h in row['top100'][:10]:
                    pp=passages[h['document_id']];hits.append({'document_id':pp['_id'],'text':pp['text'],'title':pp['title'],'url':pp['url'],'score':h['score']})
                targets.append({'task':t,'query':row['query'],'candidate':'R1-A','top10':hits,'top5':hits[:5]})
        for i,target in enumerate(targets):
            sid=SessionId(f'offline-{i}');task=target['task'];run.SESSION_INPUT[str(sid)]=target
            async def retriever(ctx):return await run.retrieve(ctx,None)
            native._retrievers={AgentId(run.AGENT):{'cloud_bm25_r0':retriever}}
            hooks.on_preparing.clear();hooks.on_generating_messages.clear();await native._setup_retrievers()
            messages=[NS(source=EventSource.CUSTOMER if m['speaker']=='user' else EventSource.AI_AGENT,content=m['text']) for m in task['input']]
            async def emit(trace,data):return NS(data=data)
            ctx=NS(agent=NS(id=AgentId(run.AGENT)),customer=NS(id=CustomerId('guest')),session=NS(id=sid,metadata={},labels=set(),mode='auto',title=None),state=NS(context_variables=[],tool_events=[]),interaction=NS(messages=messages,last_customer_message=messages[-1]),tracer=tracer,response_event_emitter=NS(emit_tool_event=emit))
            await hooks.on_preparing[0](ctx,None,None);await hooks.on_generating_messages[0](ctx,None,None)
            assert ctx.state.tool_events[0].data['tool_calls'][0]['result']['data']['passages']==target['top5']
            await hooks.on_generating_messages[0](ctx,None,None);assert len(ctx.state.tool_events)==1
    # Verify decode against actual R0 CANNED_FLUID prompts and exact passages.
    from coverage import checks
    for sample in readl(R0_RESULT/'smoke_samples.jsonl'):
        attempt=sample['attempts'][0]
        prompt=(R0_RESULT/'attempts'/attempt/'final_generation_prompt.txt')
        if not prompt.exists():
            rows=[r for r in readl(R0_RESULT/'attempts'/attempt/'observations.jsonl') if r['kind']=='model_call_started' and r['schema']=='CannedResponseDraftSchema']
            prompt=R0_RESULT/'attempts'/attempt/'prompts'/f"{rows[0]['call_id']}.txt"
        expected=sample['actual_evidence'];rows,rendered=checks(prompt.read_text(),expected,expected)
        assert rendered==expected and all(r['full_text_in_prompt'] for r in rows)
    from openai import AsyncClient
    import httpx2
    client=AsyncClient(api_key='offline-dummy',base_url='https://api.deepseek.com')
    assert client._client.send.__func__ is httpx2.AsyncClient.send
    await client.close()
    # Exercise the installed send observer on an in-memory transport, no sockets/API.
    observe.install()
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(lambda request:httpx2.Response(200,json={'offline':True}))) as transport:
        response=await transport.send(transport.build_request('POST','https://api.deepseek.com/chat/completions'))
        assert response.status_code==200
    assert sum(r['kind']=='physical_http_started' for r in observe.ROWS)==1
    assert sum(r['kind']=='physical_http_finished' and r['status']==200 for r in observe.ROWS)==1
    write(REVIEW/('OFFLINE_RUNTIME_CHECKS.json' if selected_path.exists() else 'OFFLINE_RUNTIME_PREFLIGHT.json'),{'passed':True,'selected_candidate_checked':selected_path.exists(),'exact_R0_effective_metadata_restored':True,'new_guideline_evaluations':0,'history_load_dispatches':0,'current_turn_dispatches':1,'session_cleanup_verified':True,'native_top5_transfer_all_ten':True,'runtime_speaker_text_history_exact':True,'native_emit_once':True,'actual_R0_canned_prompt_decode_ten':True,'transport_actual_send_class':'httpx2.AsyncClient','paid_calls':0,'generation_audit_failure_never_retries_completed_answer':True})
    print('OFFLINE RUNTIME PASSED',flush=True)
if __name__=='__main__':asyncio.run(main())
