"""One immutable startup, ten independent sessions through the actual Parlant engine."""
import os
from common import *
os.environ['PARLANT_HOME']=str(PROJECT/PATHS['runtime'])
os.environ['HF_HOME']=str(PROJECT/'runtime-data/huggingface')
for k in ['HF_HUB_OFFLINE','TRANSFORMERS_OFFLINE','HF_HUB_DISABLE_TELEMETRY']:os.environ[k]='1'
os.environ['PARLANT_DATA_COLLECTION']='false'
os.environ.setdefault('OMP_NUM_THREADS','2');os.environ.setdefault('MKL_NUM_THREADS','2')
import argparse,asyncio,dataclasses,faulthandler,shutil,subprocess,time,uuid
from dotenv import dotenv_values
import parlant.sdk as p
from parlant.adapters.nlp.deepseek_service import DeepSeekService
from parlant.adapters.nlp.hugging_face import HuggingFaceEmbedder
from parlant.core.meter import Meter
from parlant.core.application import Application
from parlant.core.sessions import SessionStore,EventSource,EventKind
from parlant.core.customers import CustomerStore
from parlant.core.agents import AgentId
from parlant.core.guidelines import GuidelineStore
from parlant.core.background_tasks import BackgroundTaskService
from parlant.core.engines.alpha.optimization_policy import OptimizationPolicy,BasicOptimizationPolicy
from parlant.core.engines.alpha.perceived_performance_policy import BasicPerceivedPerformancePolicy
import observe as obs
CONFIG=json.loads((BASE/'config.json').read_text())
AGENT='mtrag-cloud-r0'

class LocalEmbedder(HuggingFaceEmbedder):
    def __init__(self,logger,tracer,meter):super().__init__(logger,tracer,meter,model_name=CONFIG['embedding'].split(' ')[0])
    @property
    def dimensions(self):return 384
    @property
    def max_tokens(self):return 512
class CloudDeepSeek(DeepSeekService):
    async def get_embedder(self,hints=None):return LocalEmbedder(self._logger,self._tracer,self._meter)
def nlp(c):
    c[LocalEmbedder]=LocalEmbedder(c[p.Logger],c[p.Tracer],c[Meter])
    return CloudDeepSeek(c[p.Logger],c[p.Tracer],c[Meter])
class Quiet(BasicPerceivedPerformancePolicy):
    async def is_preamble_required(self,context=None):return False
    async def is_message_splitting_required(self,context,message):return False

async def retrieve(ctx,bm):
    query=ctx.interaction.last_customer_message.content
    hits=bm.search(query,5);obs.EVIDENCE[str(ctx.session.id)]=hits
    obs.record('retriever_returned',session_id=str(ctx.session.id),query=query,passages=hits,payload_sha256=digest(hits))
    return p.RetrieverResult(data={'passages':hits})

async def cleanup(server,sid):
    bg=server.container[BackgroundTaskService];tag=f'process-session({sid})'
    async with bg._lock:task=bg._tasks.get(tag)
    running=task is not None and not task.done()
    if running:await bg.cancel(tag=tag,reason='R0 target terminated')
    if task:
        try:await asyncio.wait_for(asyncio.shield(task),10)
        except asyncio.CancelledError:pass
    await server.container[Application].sessions.delete(sid)
    store=server.container[SessionStore]
    assert not await store._event_collection.find({'session_id': {'$eq': sid}})
    from parlant.core.common import ItemNotFoundError
    try:await store.read_session(sid)
    except ItemNotFoundError:pass
    else:raise AssertionError('Session still exists after cleanup')
    obs.record('session_cleanup',session_id=str(sid),cancelled_running_task=running,events_removed=True)

async def sample(server,x,bm,attempt):
    token=obs.SCOPE.set((x['task_id'],attempt));path=obs.scope_dir();write(path/'runtime_input.json',x);start=time.monotonic();sid=None
    outcome={'task_id':x['task_id'],'conversation_id':x['conversation_id'],'attempt_id':attempt,'status':'infrastructure_failure','answer':'unavailable'}
    app=server.container[Application];store=server.container[SessionStore]
    try:
        async with asyncio.timeout(CONFIG['per_sample_seconds']):
            sid=(await app.sessions.create(CustomerStore.GUEST_ID,AgentId(AGENT),allow_greeting=False)).id
            outcome['session_id']=str(sid)
            before=len([r for r in obs.ROWS if r['kind']=='model_call_started' and r['attempt_id']==attempt])
            for m in x['input'][:-1]:
                source=EventSource.CUSTOMER if m['speaker']=='user' else EventSource.AI_AGENT
                await app.sessions.create_event(sid,EventKind.MESSAGE,{'message':m['text'],'participant':{'id':'guest' if m['speaker']=='user' else AGENT,'display_name':'User' if m['speaker']=='user' else 'IBM Cloud assistant'},'flagged':False,'tags':[]},None,source,trigger_processing=False)
            loaded=await store.list_events(sid)
            assert [e.data['message'] for e in loaded]==[m['text'] for m in x['input'][:-1]]
            assert before==len([r for r in obs.ROWS if r['kind']=='model_call_started' and r['attempt_id']==attempt])
            assert not any(t.startswith('process-session('+str(sid)+')') for t in server.container[BackgroundTaskService]._tasks)
            obs.record('history_loaded',session_id=str(sid),messages=len(loaded),model_calls_during_load=0,processing_task_absent=True)
            target=await app.sessions.create_event(sid,EventKind.MESSAGE,{'message':x['input'][-1]['text'],'participant':{'id':'guest','display_name':'User'},'flagged':False,'tags':[]},None,EventSource.CUSTOMER,trigger_processing=True)
            offset=target.offset;answers=[];observed=[]
            while True:
                events=await store.list_events(sid,min_offset=offset+1)
                ready=False
                for e in events:
                    offset=max(offset,e.offset);row=dataclasses.asdict(e);observed.append(row);obs.record('parlant_event',session_id=str(sid),event=row)
                    if e.kind==EventKind.MESSAGE and e.source==EventSource.AI_AGENT:answers.append(e.data['message'])
                    if e.kind==EventKind.STATUS:
                        if e.data['status']=='error':raise RuntimeError('Parlant engine error')
                        ready|=e.data['status']=='ready'
                if ready:
                    if not answers:raise RuntimeError('Parlant ready without answer')
                    outcome.update(status='completed',answer='\n'.join(answers),generated_message_count=len(answers),target_trigger_count=1)
                    break
                if obs.FATAL:raise RuntimeError('Fatal provider error')
                await asyncio.sleep(0.1)
            write(path/'parlant_events.json',observed)
            coverage=[r for r in obs.ROWS if r['attempt_id']==attempt and r['kind']=='final_prompt_coverage']
            outcome['evidence_coverage_verified']=bool(coverage) and all(r['all_evidence_covered'] for r in coverage)
            outcome['actual_evidence']=obs.EVIDENCE.get(str(sid),[])
            if not outcome['evidence_coverage_verified']:raise RuntimeError('Evidence did not reach final generation context')
    except Exception as e:
        outcome.update(status='infrastructure_failure',error_type=type(e).__name__,error=str(e))
        obs.record('sample_failure',error_type=type(e).__name__,error=str(e))
    finally:
        if sid:
            try:await cleanup(server,sid)
            except Exception as e:outcome.update(cleanup_error=type(e).__name__);obs.FATAL.append({'reason':'session cleanup failed'})
        outcome['seconds']=time.monotonic()-start;write(path/'outcome.json',outcome);obs.SCOPE.reset(token)
    return outcome

async def restore_effective(server):
    # SDK stores are transient. Rehydrate the exact already evaluated snapshot,
    # including original IDs/timestamps/metadata; schedule no evaluation.
    import datetime
    from parlant.core.agents import AgentStore,CompositionMode,MessageOutputMode
    from parlant.core.guidelines import GuidelineId,Criticality
    from parlant.core.engines.alpha.perceived_performance_policy import PerceivedPerformancePolicyProvider
    frozen=json.loads((RESULT/'startup/effective_configuration.json').read_text())
    for x in frozen['agent']:
        await server.container[AgentStore].create_agent(id=AgentId(x['id']),name=x['name'],description=x['description'],creation_utc=datetime.datetime.fromisoformat(x['creation_utc']),max_engine_iterations=x['max_engine_iterations'],composition_mode=CompositionMode[x['composition_mode'].split('.')[-1]],message_output_mode=MessageOutputMode[x['message_output_mode'].split('.')[-1]],tags=x['tags'])
        server.container[PerceivedPerformancePolicyProvider].set_policy(AgentId(x['id']),Quiet())
    for x in frozen['guidelines']:
        assert x['labels']=='set()' and x['composition_mode'] is None
        await server.container[GuidelineStore].create_guideline(id=GuidelineId(x['id']),creation_utc=datetime.datetime.fromisoformat(x['creation_utc']),condition=x['content']['condition'],action=x['content']['action'],description=x['content']['description'],criticality=Criticality[x['criticality'].split('.')[-1]],metadata=x['metadata'],enabled=x['enabled'],tags=x['tags'],track=x['track'],priority=x['priority'])
    assert not server._guideline_evaluations
    return await server.get_agent(id=AGENT)

async def freeze_effective(server):
    guidelines=await server.container[GuidelineStore].list_guidelines()
    policy=server.container[OptimizationPolicy]
    assert type(policy) is BasicOptimizationPolicy
    state={'agent':[dataclasses.asdict(x) for x in await server.container[__import__('parlant.core.agents',fromlist=['AgentStore']).AgentStore].list_agents()],'guidelines':[dataclasses.asdict(x) for x in guidelines],'optimization_policy':type(policy).__module__+'.'+type(policy).__name__,'default_batch_sizes':{str(n):policy.get_guideline_matching_batch_size(n) for n in [1,3,10,11,20,21,30,31]},'config_sha256':sha(BASE/'config.json'),'initialization_count':1}
    write(RESULT/'startup/effective_configuration.json',state)
    snap=RESULT/'startup/effective_store';snap.mkdir(parents=True,exist_ok=True)
    for f in sorted((PROJECT/PATHS['runtime']).rglob('*.json')):
        out=snap/f.relative_to(PROJECT/PATHS['runtime']);out.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(f,out)
    write(RESULT/'startup/effective_hashes.json',{str(f.relative_to(RESULT/'startup')):sha(f) for f in sorted((RESULT/'startup').rglob('*')) if f.is_file() and f.name not in ('observations.jsonl','effective_hashes.json')})
    return digest(json.loads(json.dumps(state,default=str)))

async def main_async(recover=False,continue_remaining=False):
    bm=BM25.load();tasks={x['task_id']:x for x in readl(DATA/'runtime_dev.jsonl')};selection=json.loads((DATA/'selection.json').read_text());holder={};ready=asyncio.Event()
    async def serve():
        async with p.Server(host='127.0.0.1',port=18900,tool_service_port=18918,nlp_service=nlp,session_store='transient',log_level=p.LogLevel.INFO) as server:
            holder['server']=server
            if recover:
                agent=await restore_effective(server)
            else:
                agent=await server.create_agent(id=AGENT,name='IBM Cloud documentation assistant',description='Help users with IBM Cloud documentation in English using retrieved passages.',perceived_performance_policy=Quiet())
                for rule in CONFIG['rules']:await agent.create_guideline(**rule)
            async def retriever(ctx):return await retrieve(ctx,bm)
            await agent.attach_retriever(retriever,id='cloud_bm25_r0')
            ready.set()
    service=asyncio.create_task(serve());outcomes=json.loads((RESULT/'smoke_status.json').read_text())['outcomes'] if continue_remaining else []
    try:
        while not ready.is_set():
            if service.done():await service
            await asyncio.sleep(0.1)
        server=holder['server']
        startup_deadline=time.monotonic()+600
        while not server.ready.is_set():
            if service.done():await service
            if obs.FATAL:raise RuntimeError('Provider stop rule during startup')
            if time.monotonic()>startup_deadline:raise TimeoutError('SDK startup exceeded 600s')
            await asyncio.sleep(0.2)
        if recover:
            from parlant.core.agents import AgentStore
            old=json.loads((RESULT/'startup/effective_configuration.json').read_text())
            actual_agent=json.loads(json.dumps([dataclasses.asdict(x) for x in await server.container[AgentStore].list_agents()],default=str))
            actual_rules=json.loads(json.dumps([dataclasses.asdict(x) for x in await server.container[GuidelineStore].list_guidelines()],default=str))
            assert actual_agent==old['agent'] and actual_rules==old['guidelines']
            assert type(server.container[OptimizationPolicy]) is BasicOptimizationPolicy
            effective=digest(old)
            write(RESULT/('startup/continuation_validation.json' if continue_remaining else 'startup/recovery_validation.json'),{'same_agent_ids_timestamps_and_all_fields':True,'same_guideline_ids_timestamps_metadata_and_all_fields':True,'scheduled_evaluations':0,'effective_digest':effective,'configuration_initializations':1,'service_starts':3 if continue_remaining else 2,'startup_recovery_count':1})
        else:effective=await freeze_effective(server)
        write(RESULT/'startup/status.json',{'ready':True,'effective_digest':effective})
        print('STARTUP FROZEN',effective,flush=True)
        already_completed={x['task_id'] for x in outcomes if x['status']=='completed'}
        for i,selected in enumerate(selection['targets']):
            if selected['task_id'] in already_completed:continue
            if obs.FATAL:break
            x=tasks[selected['task_id']]
            for recovery in range(2):
                attempt=f'{i+1:02d}_attempt{recovery+1}'
                result=await sample(server,x,bm,attempt);outcomes.append(result)
                write(RESULT/'smoke_checkpoint.json',outcomes)
                print('SAMPLE',i+1,attempt,result['status'],round(result['seconds'],2),flush=True)
                if result['status']=='completed' or obs.FATAL:break
                if recovery==0:print('INFRASTRUCTURE RECOVERY ONCE',flush=True)
            if result['status']!='completed':break
        if recover:
            final_agent=json.loads(json.dumps([dataclasses.asdict(x) for x in await server.container[AgentStore].list_agents()],default=str))
            final_rules=json.loads(json.dumps([dataclasses.asdict(x) for x in await server.container[GuidelineStore].list_guidelines()],default=str))
            assert final_agent==old['agent'] and final_rules==old['guidelines']
            write(RESULT/'final_effective_validation.json',{'agent_unchanged':True,'all_guideline_metadata_unchanged':True,'effective_digest':effective})
        write(RESULT/'smoke_status.json',{'planned':10,'outcomes':outcomes,'stop_reasons':obs.FATAL,'service_stopped':False,'initializations':1})
    finally:
        service.cancel()
        try:await asyncio.wait_for(service,30)
        except asyncio.CancelledError:pass
        status_path=RESULT/'smoke_status.json'
        state=json.loads(status_path.read_text()) if status_path.exists() else {'planned':10,'outcomes':outcomes,'stop_reasons':obs.FATAL}
        state['service_stopped']=service.done();write(status_path,state)

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--execute',action='store_true');parser.add_argument('--recover-startup',action='store_true');parser.add_argument('--continue-remaining',action='store_true');args=parser.parse_args()
    if not args.execute:raise SystemExit('Paid execution requires --execute; inspect frozen protocol first')
    if (RESULT/'smoke_status.json').exists():
        if not (args.recover_startup or args.continue_remaining):raise SystemExit('Existing smoke run is immutable; no rerun')
        previous=json.loads((RESULT/'smoke_status.json').read_text())
        if args.continue_remaining:
            assert previous['service_stopped'] and len(previous['outcomes'])==2
            assert [x['status'] for x in previous['outcomes']]==['completed','audit_recovery_extra_answer']
            assert not (RESULT/'CONTINUATION_STARTED.json').exists()
        else:
            assert not previous['outcomes'] and previous['service_stopped']
            assert not (RESULT/'startup/recovery_validation.json').exists()
        assert (RESULT/'startup/effective_configuration.json').exists()
    frozen=json.loads((REVIEW/'RUN_FROZEN.json').read_text())
    addendum=json.loads((REVIEW/('CONTINUATION_FROZEN.json' if args.continue_remaining else 'STARTUP_RECOVERY_FROZEN.json')).read_text()) if args.recover_startup or args.continue_remaining else None
    for name,value in frozen['files'].items():
        expected=addendum['changed_files'].get(name,value) if addendum else value
        assert sha(PROJECT/name)==expected,name
    if addendum:
        for name,value in addendum.get('additional_code_sha256',{}).items():assert sha(PROJECT/name)==value,name
        assert sha(RESULT/'startup/effective_configuration.json')==addendum['effective_configuration_sha256']
        assert sha(PROJECT/PATHS['runtime']/'evaluation_cache.json')==addendum['evaluation_cache_sha256']
    secret=dotenv_values(PROJECT/'.env').get('DEEPSEEK_API_KEY');assert secret,'Missing key in root .env';os.environ['DEEPSEEK_API_KEY']=secret
    if args.continue_remaining:write(RESULT/'CONTINUATION_STARTED.json',{'canary_skipped':True,'remaining':9,'configuration_initializations':1})
    faulthandler.enable();obs.install()
    try:asyncio.run(main_async(args.recover_startup or args.continue_remaining,args.continue_remaining))
    finally:os.environ.pop('DEEPSEEK_API_KEY',None)
if __name__=='__main__':main()
