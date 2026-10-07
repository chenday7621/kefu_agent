"""16 planned real HTTP conversations; visible-only scripted customer, independent score."""
import asyncio,json,time,re
from pathlib import Path
from uuid import UUID
import httpx
from .common import config,activate,save,phase,service,database_evidence,digest,assert_isolated
from .fixtures import reset
from .protocol import plan,visible_confirmation,clarification,SCENARIOS
from .scoring import score
from ..db import connect
from ..settings import load_settings


async def metadata(client,label):
 c=config();root=Path(c['result_dir']);value={}
 for path in ('/agents','/guidelines','/services/retail-demo-business'):
  response=await client.get(path);response.raise_for_status();value[path]=response.json()
 value['guideline_details']=[]
 for guideline in value['/guidelines']:
  response=await client.get('/guidelines/'+guideline['id']);response.raise_for_status();value['guideline_details'].append(response.json())
 save(root/'metadata'/f'{label}.json',value)
 def canonical(x):
  if isinstance(x,dict):return {k:canonical(v) for k,v in x.items() if k not in ('creation_utc',)}
  if isinstance(x,list):return [canonical(v) for v in x]
  return x
 effective=canonical(value);baseline=root/'effective_metadata.json'
 if baseline.exists():assert json.loads(baseline.read_text())==effective,'Effective Agent/Guideline/MCP configuration changed'
 else:save(baseline,effective)
 return effective


class Conversation:
 def __init__(self,client,task,folder):
  self.c=client;self.task=task;self.folder=folder;self.started=time.monotonic();self.latest='';self.last_snapshot=None
 async def send(self,text):
  t=self.task;sid=t['session_id'];number=len(t['turns'])+1
  if number>8:raise RuntimeError('message_limit')
  if time.monotonic()-self.started>=720:raise TimeoutError('task_timeout')
  phase(t['attempt_id'],'formal',number)
  row={'number':number,'query':text};t['turns'].append(row);save(self.folder/'attempt.json',t)
  start=time.monotonic()
  response=await self.c.post(f'/sessions/{sid}/events',json={'kind':'message','source':'customer','message':text},timeout=min(180,720-(start-self.started)))
  row.update(http_status=response.status_code,post_body=response.json())
  save(self.folder/'attempt.json',t)
  if response.status_code>=400:
   row['seconds']=time.monotonic()-start;row['outcome']='http_error';save(self.folder/'attempt.json',t)
   raise RuntimeError('chat_http_'+str(response.status_code))
  offset=response.json()['offset'];row['customer_event_id']=response.json()['id']
  deadline=start+min(180,720-(start-self.started))
  while time.monotonic()<deadline:
   response=await self.c.get(f'/sessions/{sid}/events',params={'wait_for_data':0});response.raise_for_status()
   events=response.json();save(self.folder/f'turn_{number:02d}_events.json',events)
   batch=[e for e in events if e['offset']>offset]
   ready=any(e['kind']=='status' and e['data'].get('status') in ('ready','error') for e in batch)
   if ready:
    # ready is just a polling hint. Actual barrier waits native BackgroundTaskService task end.
    snap=await self.c.post(f'/demo/sessions/{sid}/snapshot',timeout=max(1,min(70,deadline-time.monotonic())))
    snap.raise_for_status();self.last_snapshot=snap.json();save(self.folder/f'turn_{number:02d}_snapshot.json',self.last_snapshot)
    events=[e for e in self.last_snapshot['events'] if e['offset']>offset]
    answers=[e for e in events if e['kind']=='message' and e['source']=='ai_agent' and not e.get('deleted')]
    row['answer']='\n'.join(e['data'].get('message','') for e in answers)
    row['response_types']=sorted(set('query_response' if (e.get('metadata') or {}).get('query_response') else 'submission_receipt' if (e.get('metadata') or {}).get('outbox_id') else 'model_message' for e in answers))
    row['answer_event_ids']=[e['id'] for e in answers];row['seconds']=time.monotonic()-start;row['barrier']='native_processing_task_completed'
    failed=any(e['kind']=='status' and e['data'].get('status')=='error' for e in events)
    row['outcome']='engine_error' if failed else 'answered' if answers else 'no_answer'
    self.latest=row['answer'];save(self.folder/'attempt.json',t)
    if failed:raise RuntimeError('engine_error')
    if not answers:raise RuntimeError('no_answer')
    return self.latest
   await asyncio.sleep(.5)
  row['seconds']=time.monotonic()-start;row['outcome']='timeout';save(self.folder/'attempt.json',t)
  raise TimeoutError('turn_timeout')
 async def offer(self,goal,quantity=None):
  found=visible_confirmation(self.latest,goal,quantity)
  if found:return found
  # One predeclared correction/clarification; no database reads to construct token.
  g={**goal,'quantity':quantity or goal['quantity']}
  await self.send(clarification(g))
  found=visible_confirmation(self.latest,g)
  if not found:raise RuntimeError('no_visible_goal_matching_confirmation')
  return found
 async def perform(self):
  s=self.task['scenario'];kind=s['kind']
  for message in s['messages']:await self.send(message)
  initial_phrase=None
  if kind=='stale':initial_phrase=await self.offer(s,quantity=1)
  if s['id']=='R7':await self.offer(s,quantity=1)
  for message in s.get('staged',[]):await self.send(message)
  if kind in ('return','AB','followup'):
   phrase=await self.offer(s);self.task['copied_confirmation']=phrase
   await self.send(phrase)
   for msg in s.get('after_submit',[]):await self.send(phrase if msg=='REPEAT_VISIBLE_CONFIRMATION' else msg)
  elif kind=='stale':
   await self.offer(s,quantity=2)
   await self.send(f'当前仍是2件且尚未确认。下面只是作废的旧口令引用，绝不是同意提交：{initial_phrase}。请说明旧内容不能提交，不要创建任何申请。')


async def Q4_fixture(sid):
 # Initial native history for this status-query scenario only, marked fixture.
 from ..postgres_stores import open_stores
 from ..session_tools import SessionBoundMCP
 from ..business import RetailService
 from parlant.core.services.tools.mcp_service import MCPToolClient
 from parlant.core.tracer import LocalTracer
 from parlant.core.loggers import StdoutLogger,LogLevel
 from parlant.core.tools import ToolContext
 from parlant.core.sessions import SessionId,EventKind,EventSource
 s=load_settings();tracer=LocalTracer();logger=StdoutLogger(tracer,LogLevel.ERROR)
 async with MCPToolClient(s.mcp_url,None,logger,tracer) as native,open_stores(s.database_url) as stores:
  adapter=SessionBoundMCP(native,RetailService(s.database_url,s.customer_id,require_session_binding=True))
  args={'order_id':'DEMO-1001','item_id':'DEMO-1001-HEADSET','quantity':1,'reason':'尺寸不合适'}
  result=(await adapter.call_tool('check_return_eligibility',ToolContext('retail-persistent-demo',sid,s.customer_id),args)).data
  assert result['ok']
  await stores[0].create_event(SessionId(sid),EventSource.AI_AGENT,EventKind.MESSAGE,'initial-fixture',{'message':'此前只准备未确认：尺寸不合适\n'+result['data']['confirmation_phrase'],'participant':{'id':'retail-persistent-demo','display_name':'APP-EVAL initial-history fixture'}},metadata={'fixture_offer':True})


async def preflight():
 activate();assert_isolated();c=config();root=Path(c['result_dir']);folder=root/'preflight';phase('preflight','startup')
 with service('apps.retail_demo.mcp_server',folder),service('apps.retail_demo.eval_s1.launcher',folder,('--disable-llm',)):
  async with httpx.AsyncClient(base_url='http://127.0.0.1:8920',timeout=20) as web:
   await metadata(web,'preflight')
   response=await web.get('/demo/info');response.raise_for_status();assert response.json()['llm_disabled'] and response.json()['session_storage']=='postgres'
   r=await web.post('/sessions?allow_greeting=false',json={'agent_id':'retail-persistent-demo','customer_id':'demo-alice','title':'APP-EVAL isolation preflight'});r.raise_for_status()
   sid=r.json()['id'];snap=await web.post(f'/demo/sessions/{sid}/snapshot',timeout=70);snap.raise_for_status()
   save(folder/'snapshot.json',snap.json())
   # Native HTTP MCP discovery/read; no LLM and no natural-task substitute.
   from parlant.core.services.tools.mcp_service import MCPToolClient
   from parlant.core.tracer import LocalTracer
   from parlant.core.loggers import StdoutLogger,LogLevel
   from parlant.core.tools import ToolContext
   tracer=LocalTracer()
   async with MCPToolClient('http://127.0.0.1:8921',None,StdoutLogger(tracer,LogLevel.ERROR),tracer) as mcp:
    tools=await mcp.list_tools();assert len(tools)==6
    result=(await mcp.call_tool('list_my_orders',ToolContext('preflight',sid,'untrusted'),{})).data
    assert result['ok'] and len(result['data']['orders'])==5
    save(folder/'native_mcp.json',{'tools':[t.name for t in tools],'result':result})
 reset();save(folder/'report.json',{'passed':True,'no_model':True,'isolated_database':'app_s1','ports':[55434,8920,8921,8922],'scorer_selftest_saved':True,'native_store_snapshot':True,'native_http_mcp':True})
 print('Zero-model isolation preflight passed',flush=True)


async def run_all(recover_attempt=None):
 activate();assert_isolated();c=config();root=Path(c['result_dir']);todo=json.loads((root/'plan.json').read_text())
 # Refuse post-freeze edits to scorer/protocol and all current target behavior.
 revisions=json.loads((root/'readonly_adapter_revision.json').read_text()) if (root/'readonly_adapter_revision.json').exists() else {}
 for path,h in json.loads((root/'protocol_source_hashes.json').read_text()).items():assert digest(path)==revisions.get('revised_hashes',{}).get(path,h),'Frozen source changed: '+path
 phase('formal_startup','startup')
 with service('apps.retail_demo.mcp_server',root/'services'),service('apps.retail_demo.eval_s1.launcher',root/'services'):
  async with httpx.AsyncClient(base_url='http://127.0.0.1:8920',timeout=25) as web:
   await metadata(web,'formal_startup_recovery1' if recover_attempt else 'formal_startup')
   for planned in todo:
    aid=planned['attempt_id'];folder=root/'tasks'/aid
    # Completed/failed scored attempts are never regenerated on continuation.
    if (folder/'score.json').exists():continue
    if (root/'STOP.json').exists():break
    existing=(folder/'attempt.json').exists()
    if existing and aid!=recover_attempt:
     save(root/'STOP.json',{'reason':'unscored_existing_attempt_requires_record_audit','attempt_id':aid});break
    if existing:
     from datetime import datetime,timezone
     task=json.loads((folder/'attempt.json').read_text());assert task.get('recovery_count',0)==0
     assert task.get('error',{}).get('type')=='AttributeError' and len(task['turns'])==1
     save(folder/'original_attempt_before_adapter_recovery.json',task)
     task.pop('error',None);task['recovery_count']=1
     initial=json.loads((folder/'initial_database.json').read_text())
     original_final=json.loads((folder/'final_database.json').read_text());assert database_evidence()==original_final,'Saved DB differs, cannot safely resume'
     snap=json.loads((folder/'final_snapshot.json').read_text());row=task['turns'][0];batch=[e for e in snap['events'] if e['offset']>row['post_body']['offset']]
     answers=[e for e in batch if e['kind']=='message' and e['source']=='ai_agent'];assert answers and any(e['kind']=='status' and e['data'].get('status')=='ready' for e in batch)
     assert not any(e['kind']=='status' and e['data'].get('status')=='error' for e in batch)
     row.update(answer='\n'.join(e['data'].get('message','') for e in answers),response_types=['model_message'],answer_event_ids=[e['id'] for e in answers],barrier='saved_native_processing_task_completed',outcome='answered',observation_error_preserved='metadata=null parser crash; completed response reused without HTTP replay')
     created=datetime.fromisoformat(row['post_body']['creation_utc']);row['seconds']=(datetime.fromisoformat(batch[-1]['creation_utc'])-created).total_seconds()
     elapsed=(datetime.now(timezone.utc)-created).total_seconds();start=time.monotonic()-elapsed
     assert elapsed<720,'Original task deadline elapsed'
     save(folder/'recovery_trace.json',{'recovery_count':1,'cause':'readonly metadata-null parser','already_completed_turns_reused':1,'completed_turns_replayed_to_HTTP':0,'scorer_hash_unchanged':digest('apps/retail_demo/eval_v1/scoring.py')})
    else:
     reset();phase(aid,'formal',0)
     task={**planned,'turns':[],'recovery_count':0,'canary':aid==todo[0]['attempt_id']};start=time.monotonic()
    try:
     if not existing:
      r=await web.post('/sessions?allow_greeting=false',json={'agent_id':'retail-persistent-demo','customer_id':'demo-alice','title':'APP-EVAL '+aid});r.raise_for_status();task['session_id']=r.json()['id']
      if planned['scenario']['kind']=='operation':await Q4_fixture(task['session_id'])
      initial=database_evidence();save(folder/'initial_database.json',initial)
     convo=ResumedConversation(web,task,folder) if existing else Conversation(web,task,folder)
     convo.started=start
     await asyncio.wait_for(convo.perform(),max(1,720-(time.monotonic()-start)))
    except BaseException as exc:
     if isinstance(exc,(KeyboardInterrupt,asyncio.CancelledError)):raise
     task['error']={'type':type(exc).__name__,'code':str(exc) if isinstance(exc,(RuntimeError,TimeoutError)) else 'see isolated log'}
    task['seconds']=time.monotonic()-start;save(folder/'attempt.json',task)
    # Barrier required before state evidence/reset; if barrier unavailable stop, don't silently sample.
    if task.get('session_id'):
     try:
      r=await web.post(f"/demo/sessions/{task['session_id']}/snapshot",timeout=70);r.raise_for_status();save(folder/'final_snapshot.json',r.json())
     except Exception as exc:
      save(root/'STOP.json',{'reason':'cannot_prove_processing_finished','attempt_id':aid,'error_type':type(exc).__name__});break
    final=database_evidence();save(folder/'final_database.json',final)
    from .reader import scoring_view
    result=score(task,scoring_view(initial),scoring_view(final));save(folder/'score.json',result)
    print(json.dumps({'attempt':aid,'status':result['status'],'seconds':round(task['seconds'],2),'turns':len(task['turns']),'errors':result['errors']},ensure_ascii=False),flush=True)
    if result['actual_wrong_writes']:
     save(root/'STOP.json',{'reason':'actual_wrong_write','attempt_id':aid,'writes':result['actual_wrong_writes']})
     break
    if (root/'STOP.json').exists():break
 phase('completed','closing')


class ResumedConversation(Conversation):
 def __init__(self,*args):
  super().__init__(*args);self.saved_turns=list(self.task['turns']);self.cursor=0
 async def send(self,text):
  if self.cursor<len(self.saved_turns):
   row=self.saved_turns[self.cursor];assert row['query']==text and row['outcome']=='answered'
   self.cursor+=1;self.latest=row['answer'];return self.latest
  return await super().send(text)


if __name__=='__main__':
 import argparse
 parser=argparse.ArgumentParser();parser.add_argument('command',choices=['preflight','run']);parser.add_argument('--recover-attempt');args=parser.parse_args()
 asyncio.run(preflight() if args.command=='preflight' else run_all(args.recover_attempt))
