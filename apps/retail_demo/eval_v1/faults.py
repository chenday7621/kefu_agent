"""18 new zero-model actual PG/HTTP-MCP/native-Store fault samples, 6 x 3."""
import asyncio,json,time,os,subprocess,sys
from pathlib import Path
from contextlib import ExitStack
from uuid import uuid4
import httpx
from psycopg import sql
from parlant.core.sessions import SessionId,EventKind,EventSource
from parlant.core.services.tools.mcp_service import MCPToolClient
from parlant.core.loggers import StdoutLogger,LogLevel
from parlant.core.tracer import LocalTracer
from parlant.core.tools import ToolContext
from ..settings import load_settings
from ..db import connect
from ..business import RetailService,plain
from ..session_tools import SessionBoundMCP
from ..postgres_stores import open_stores
from ..outbox import Outbox
from ..snapshot_assertions import compare_snapshots
from .common import config,activate,save,service,phase,database_evidence,assert_isolated
from .fixtures import reset
from .runner import metadata

KINDS=('pending_restart','lost_mcp_response','temporary_receipt_failure','post_delivery_crash','interleaved_recovery','failed_manual_retry')

class Servers:
 def __init__(self,folder):self.folder=folder;self.contexts={};self.procs={}
 def start(self,name):
  key='mcp' if name=='mcp' else 'app';module='apps.retail_demo.mcp_server' if key=='mcp' else 'apps.retail_demo.eval_v1.launcher'
  extra={'DEMO_TEST_DROP_COMMITTED_MCP_RESPONSE':str(self.folder/'drop_mcp'),'DEMO_TEST_OUTBOX_AFTER_COMMIT':str(self.folder/'after_commit')}
  ctx=service(module,self.folder,() if key=='mcp' else ('--disable-llm',),extra)
  self.procs[key]=ctx.__enter__();self.contexts[key]=ctx;return self.procs[key]
 def stop(self,key):
  if key in self.contexts:self.contexts.pop(key).__exit__(None,None,None)
 def close(self):self.stop('app');self.stop('mcp')


def control(enabled):
 with connect(load_settings().database_url) as c:c.execute('UPDATE outbox_control SET enabled=%s',(enabled,))


def outbox(op):
 with connect(load_settings().database_url) as c:return plain(c.execute('SELECT * FROM return_outbox WHERE operation_id=%s',(op,)).fetchone())


def counts(op,item):
 with connect(load_settings().database_url) as c:
  return dict(c.execute("SELECT (SELECT count(*) FROM return_requests WHERE operation_id=%s) AS requests,(SELECT reserved_return_quantity FROM order_items WHERE id=%s) AS reserved,(SELECT count(*) FROM return_outbox WHERE operation_id=%s) AS notifications,(SELECT count(*) FROM parlant_events WHERE doc#>>'{metadata,operation_id}'=%s AND doc#>>'{metadata,outbox_id}' IS NOT NULL) AS canonical_receipts",(op,item,op,op)).fetchone())


async def wait(fn,timeout=110):
 start=time.monotonic()
 while time.monotonic()-start<timeout:
  value=fn()
  if value:return value
  await asyncio.sleep(.15)
 raise TimeoutError('fault-state wait exceeded limit')


async def call(name,args,sid,folder):
 s=load_settings();tracer=LocalTracer();ctx=ToolContext('retail-persistent-demo',sid,'demo-alice');row={'name':name,'arguments':args,'session_id':sid};start=time.monotonic()
 try:
  async with MCPToolClient(s.mcp_url,None,StdoutLogger(tracer,LogLevel.ERROR),tracer) as native:
   adapter=SessionBoundMCP(native,RetailService(s.database_url,s.customer_id,require_session_binding=True))
   result=(await adapter.call_tool(name,ctx,args)).data;row['result']=result;return result
 except BaseException as exc:row['error_type']=type(exc).__name__;raise
 finally:row['seconds']=time.monotonic()-start;save(folder/'mcp_calls'/(uuid4().hex+'.json'),row)


async def snapshot(web,sid,path):
 r=await web.post(f'/demo/sessions/{sid}/snapshot',timeout=70);r.raise_for_status();save(path,r.json());return r.json()


async def prepare(web,folder,confirmed=True):
 r=await web.post('/sessions?allow_greeting=false',json={'agent_id':'retail-persistent-demo','customer_id':'demo-alice','title':'APP-EVAL zero-model fault fixture'});r.raise_for_status();sid=r.json()['id']
 args={'order_id':'DEMO-1001','item_id':'DEMO-1001-HEADSET','quantity':1,'reason':'故障验证'}
 result=await call('check_return_eligibility',args,sid,folder);assert result['ok']
 preview=result['data'];op=preview['operation_id']
 async with open_stores(load_settings().database_url) as stores:
  await stores[0].create_event(SessionId(sid),EventSource.SYSTEM,EventKind.TOOL,'fixture-prepare',{'tool_calls':[{'tool_id':'retail-demo-business:check_return_eligibility','arguments':args,'result':{'data':result,'metadata':{},'control':{}}}]})
  await stores[0].create_event(SessionId(sid),EventSource.AI_AGENT,EventKind.MESSAGE,'fixture-offer',{'message':preview['confirmation_phrase'],'participant':{'id':'retail-persistent-demo','display_name':'Marked offline fixture'}},metadata={'fixture_offer':True})
 if confirmed:
  r=await web.post(f'/demo/sessions/{sid}/confirm',json={'message':preview['confirmation_phrase']});r.raise_for_status()
 return {'session_id':sid,'operation_id':op,'args':args,'preview':preview}


def inject(oid,temporary):
 assert_isolated();name='eval_fault_'+uuid4().hex[:12];sequence=name+'_seq'
 with connect(load_settings().database_url) as c:
  if temporary:c.execute(sql.SQL('CREATE SEQUENCE {}').format(sql.Identifier(sequence)))
  gate=sql.SQL(' AND nextval({})<=1').format(sql.Literal(sequence)) if temporary else sql.SQL('')
  c.execute(sql.SQL("CREATE FUNCTION {}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.doc#>>'{{metadata,outbox_id}}'={} {} THEN RAISE EXCEPTION 'APP-EVAL receipt INSERT fault' USING ERRCODE='23514'; END IF; RETURN NEW; END $$").format(sql.Identifier(name),sql.Literal(oid),gate))
  c.execute(sql.SQL('CREATE TRIGGER {} BEFORE INSERT ON parlant_events FOR EACH ROW EXECUTE FUNCTION {}()').format(sql.Identifier(name),sql.Identifier(name)))
 return name,sequence if temporary else None


def remove_fault(spec):
 if not spec:return
 name,sequence=spec
 with connect(load_settings().database_url) as c:
  c.execute(sql.SQL('DROP TRIGGER IF EXISTS {} ON parlant_events').format(sql.Identifier(name)))
  c.execute(sql.SQL('DROP FUNCTION IF EXISTS {}()').format(sql.Identifier(name)))
  if sequence:c.execute(sql.SQL('DROP SEQUENCE IF EXISTS {}').format(sql.Identifier(sequence)))


async def one(kind,repeat):
 root=Path(config()['result_dir']);aid=f'{kind}_{repeat}';folder=root/'faults'/aid
 if (folder/'report.json').exists():return
 reset();phase(aid,'fault');save(folder/'initial_database.json',database_evidence())
 servers=Servers(folder);spec=None;start=time.monotonic();report={'id':aid,'kind':kind,'repeat':repeat,'passed':False,'injection_triggered':False,'model_calls':0,'automatic_recovery':None,'manual_recovery':None,'fixture_not_model_dialogue':True}
 calls_before=set((root/'calls').glob('*.json')) if (root/'calls').exists() else set()
 try:
  control(False);servers.start('mcp');servers.start('app')
  async with httpx.AsyncClient(base_url='http://127.0.0.1:8910',timeout=25) as web:
   await metadata(web,aid+'_initial')
   case=await prepare(web,folder,confirmed=kind!='pending_restart');report.update({k:case[k] for k in ('session_id','operation_id')});sid=case['session_id'];op=case['operation_id']
   args={'operation_id':op,**case['args']}
   if kind=='pending_restart':
    refused=await call('create_return_request',args,sid,folder);assert not refused['ok'];report['unconfirmed_error']=refused
    before=await snapshot(web,sid,folder/'before_restart_snapshot.json')
    oldpid=servers.procs['app'].pid;servers.stop('app');servers.start('app');await metadata(web,aid+'_restart')
    after=await snapshot(web,sid,folder/'after_restart_snapshot.json');report['snapshot_check']=compare_snapshots(before,after)
    report['injection_triggered']=servers.procs['app'].pid!=oldpid
    result=(await web.get(f'/demo/sessions/{sid}/operation-result')).json();assert result['data']['state']=='awaiting_confirmation'
    refused=await call('create_return_request',args,sid,folder);assert not refused['ok'] and counts(op,case['args']['item_id'])['requests']==0
    confirmed=await web.post(f'/demo/sessions/{sid}/confirm',json={'message':case['preview']['confirmation_phrase']});confirmed.raise_for_status()
    made=await call('create_return_request',args,sid,folder);assert made['ok'];control(True)
    await wait(lambda:outbox(op)['status']=='delivered');report['requires_real_confirmation_after_restart']=True
   elif kind=='lost_mcp_response':
    (folder/'drop_mcp').write_text(op)
    try:await call('create_return_request',args,sid,folder);raise AssertionError('Expected response loss not triggered')
    except AssertionError:raise
    except Exception as exc:report['response_loss_exception']=type(exc).__name__
    code=await asyncio.to_thread(servers.procs['mcp'].wait,10);assert code==77
    report['injection_triggered']=code==77 and not (folder/'drop_mcp').exists();report['crash_exit_code']=code
    assert outbox(op)['status']=='pending' and counts(op,case['args']['item_id'])['requests']==1
    before=await snapshot(web,sid,folder/'before_restart_snapshot.json');save(folder/'committed_before_restart_database.json',database_evidence())
    servers.stop('app');servers.stop('mcp');control(True);recovery_start=time.monotonic();servers.start('mcp');servers.start('app');await metadata(web,aid+'_restart')
    await wait(lambda:outbox(op)['status']=='delivered');report['recovery_seconds']=time.monotonic()-recovery_start
    after=await snapshot(web,sid,folder/'after_restart_snapshot.json');report['snapshot_check']=compare_snapshots(before,after);report['automatic_recovery']=True
   else:
    made=await call('create_return_request',args,sid,folder);assert made['ok'];oid=outbox(op)['id'];report['request_id']=made['data']['request']['id'];report['outbox_id']=oid
    before=await snapshot(web,sid,folder/'before_fault_snapshot.json')
    if kind in ('temporary_receipt_failure','failed_manual_retry'):
     spec=inject(oid,kind=='temporary_receipt_failure');report['injected_trigger']=spec[0];control(True);recovery_start=time.monotonic()
     if kind=='temporary_receipt_failure':
      await wait(lambda:outbox(op)['total_attempts']>=1);first=outbox(op);save(folder/'after_insert_failure.json',first)
      with connect(load_settings().database_url) as c:
       assert c.execute(sql.SQL('SELECT last_value FROM {}').format(sql.Identifier(spec[1]))).fetchone()['last_value']>=1
      await wait(lambda:outbox(op)['status']=='delivered',30);row=outbox(op)
      assert row['total_attempts']==2 and row['last_error'];report['injection_triggered']=True;report['automatic_recovery']=True
      report['recovery_seconds']=time.monotonic()-recovery_start
     else:
      await wait(lambda:outbox(op)['status']=='failed',110);failed=outbox(op);save(folder/'failed_notification.json',failed)
      assert failed['attempts']==5 and failed['total_attempts']==5 and failed['last_error'];report['injection_triggered']=True
      await asyncio.sleep(1.2);assert outbox(op)['total_attempts']==5
      report['automatic_recovery']=False;report['automatic_stop_seconds']=time.monotonic()-recovery_start
      failed_snap=await snapshot(web,sid,folder/'failed_before_restart_snapshot.json');servers.stop('app');servers.start('app');await metadata(web,aid+'_restart_failed')
      restarted=await snapshot(web,sid,folder/'failed_after_restart_snapshot.json');compare_snapshots(failed_snap,restarted)
      assert outbox(op)['status']=='failed';remove_fault(spec);spec=None
      manual_start=time.monotonic();command=[sys.executable,'-m','apps.retail_demo.outbox','retry','--id',oid]
      completed=await asyncio.to_thread(subprocess.run,command,check=True,capture_output=True,text=True);(folder/'manual_retry_output.json').write_text(completed.stdout)
      await wait(lambda:outbox(op)['status']=='delivered');assert outbox(op)['total_attempts']==6 and outbox(op)['manual_retries']==1
      report['manual_recovery']=True;report['manual_recovery_seconds']=time.monotonic()-manual_start
     after=await snapshot(web,sid,folder/'after_fault_snapshot.json');report['snapshot_check']=compare_snapshots(before,after)
    elif kind=='post_delivery_crash':
     (folder/'after_commit').write_text(oid);control(True)
     code=await asyncio.to_thread(servers.procs['app'].wait,20);assert code==78
     report['injection_triggered']=code==78 and not (folder/'after_commit').exists();report['crash_exit_code']=code
     save(folder/'after_committed_crash_database.json',database_evidence());assert outbox(op)['status']=='delivered'
     recovery_start=time.monotonic();servers.stop('app');servers.start('app');await metadata(web,aid+'_restart')
     after=await snapshot(web,sid,folder/'after_restart_snapshot.json');report['snapshot_check']=compare_snapshots(before,after);report['recovery_seconds']=time.monotonic()-recovery_start;report['automatic_recovery']=True
    elif kind=='interleaved_recovery':
     control(True);recovery_start=time.monotonic()
     async with open_stores(load_settings().database_url) as stores:
      box=Outbox(stores[0])
      async def ask():
       r=await web.post(f'/sessions/{sid}/events',json={'kind':'message','source':'customer','message':'刚才退货成功了吗'});r.raise_for_status();return r.json()
      values=await asyncio.gather(box.deliver(oid),box.deliver(oid),ask(),call('create_return_request',args,sid,folder),call('create_return_request',args,sid,folder))
      assert all(v['ok'] and v['data']['idempotent_replay'] for v in values[-2:]);report['interleaving_actions']=['running_worker','direct_delivery_1','direct_delivery_2','normal_HTTP_recovery_query','MCP_idempotent_replay_1','MCP_idempotent_replay_2']
     await wait(lambda:outbox(op)['status']=='delivered');report['injection_triggered']=True;report['automatic_recovery']=True;report['recovery_seconds']=time.monotonic()-recovery_start
     final=await snapshot(web,sid,folder/'before_restart_snapshot.json')
     assert final['events'][:len(before['events'])]==before['events']
     save(folder/'identified_interleaving_tail.json',[{'id':e['id'],'kind':e['kind'],'source':e['source'],'metadata':e['metadata']} for e in final['events'][len(before['events']):]])
     servers.stop('app');servers.start('app');await metadata(web,aid+'_restart');after=await snapshot(web,sid,folder/'after_restart_snapshot.json');report['snapshot_check']=compare_snapshots(final,after)
   report['counts']=counts(op,case['args']['item_id']);assert report['counts']=={'requests':1,'reserved':1,'notifications':1,'canonical_receipts':1}
   report['final_outbox']=outbox(op)
   # Repeated read/deliver/replay never adds application, quantity or canonical receipt.
   async with open_stores(load_settings().database_url) as stores:await Outbox(stores[0]).deliver(outbox(op)['id'])
   assert counts(op,case['args']['item_id'])==report['counts']
   report['passed']=bool(report['injection_triggered'])
 except Exception as exc:
  report['error_type']=type(exc).__name__;report['error']=str(exc) if isinstance(exc,(AssertionError,TimeoutError)) else 'inspect isolated log'
 finally:
  remove_fault(spec);servers.close()
  for marker in ('drop_mcp','after_commit'):(folder/marker).unlink(missing_ok=True)
  save(folder/'final_database.json',database_evidence())
  calls_after=set((root/'calls').glob('*.json')) if (root/'calls').exists() else set();report['model_calls']=len(calls_after-calls_before);assert report['model_calls']==0
  report['seconds']=time.monotonic()-start;save(folder/'report.json',report)
  print(json.dumps({'fault':aid,'passed':report['passed'],'triggered':report['injection_triggered'],'seconds':round(report['seconds'],2),'error':report.get('error')},ensure_ascii=False),flush=True)


async def run():
 activate();assert_isolated()
 for kind in KINDS:
  for repeat in (1,2,3):await one(kind,repeat)

if __name__=='__main__':asyncio.run(run())
