"""Finite zero-model APP-S1 checks: real PG, HTTP MCP and native Store."""
import asyncio,json,os,time
from pathlib import Path
from uuid import uuid4
import httpx
from .common import config,activate,save,phase,database_evidence,assert_isolated
from .fixtures import reset
from .offline_helpers import Servers,call,control,counts,outbox,wait,snapshot
from ..business import RetailService,BusinessError
from ..settings import load_settings
from ..db import connect,initialize
from ..postgres_stores import open_stores
from ..session_tools import SessionBoundMCP
from ..snapshot_assertions import compare_snapshots
from parlant.core.sessions import SessionId,EventKind,EventSource
from parlant.core.tools import ToolContext
from parlant.core.services.tools.mcp_service import MCPToolClient
from parlant.core.tracer import LocalTracer
from parlant.core.loggers import StdoutLogger,LogLevel

async def case(web,folder,item='DEMO-1001-HEADSET',quantity=1,reason='离线参数逐字核对，尺寸不合适',sid=None,confirmed=True):
 if sid is None:
  r=await web.post('/sessions?allow_greeting=false',json={'agent_id':'retail-persistent-demo','customer_id':'demo-alice','title':'APP-S1 marked offline fixture'});r.raise_for_status();sid=r.json()['id']
 args={'order_id':'DEMO-1001','item_id':item,'quantity':quantity,'reason':reason}
 result=await call('check_return_eligibility',args,sid,folder);assert result['ok'],result
 p=result['data']
 async with open_stores(load_settings().database_url) as (store,*_):
  await store.create_event(SessionId(sid),EventSource.AI_AGENT,EventKind.MESSAGE,'fixture-offer-'+uuid4().hex,{'message':f"{p['reason']} {p['amount_display']}\n{p['confirmation_phrase']}"},metadata={'fixture_offer':True})
 if confirmed:
  r=await web.post(f'/demo/sessions/{sid}/confirm',json={'message':p['confirmation_phrase']});r.raise_for_status()
 return {'sid':sid,'op':p['operation_id'],'args':args,'preview':p}

async def submit(c,folder,**extra):return await call('submit_confirmed_return',{'operation_id':c['op'],**extra},c['sid'],folder)

async def run():
 activate();assert_isolated();c=config();root=Path(c['result_dir']);folder=root/'offline';folder.mkdir(exist_ok=True);phase('offline','offline')
 report={'checks':[],'no_model':True,'passed':False,'attempts':1};servers=Servers(folder);s=load_settings();biz=RetailService(s.database_url,s.customer_id,require_session_binding=True)
 def check(name,evidence):report['checks'].append({'name':name,'passed':True,'evidence':evidence});save(folder/'report.json',report)
 try:
  reset();save(folder/'initial_database.json',database_evidence());control(False);servers.start('mcp');servers.start('app')
  async with httpx.AsyncClient(base_url='http://127.0.0.1:8920',timeout=70) as web:
   # Read actual native wire/model discovery; the latter hides the capability.
   tracer=LocalTracer()
   async with MCPToolClient(s.mcp_url,None,StdoutLogger(tracer,LogLevel.ERROR),tracer) as native:
    tools=await native.list_tools();assert 'create_return_request' not in {t.name for t in tools}
    visible=await SessionBoundMCP(native,biz).read_tool('submit_confirmed_return');assert set(visible.parameters)=={'operation_id'}
    save(folder/'tool_schema.json',{'model_schema':{'properties':{k:v[0] for k,v in visible.parameters.items()},'required':list(visible.required)},'wire_schema':(next(t for t in await native._client.list_tools() if t.name=='submit_confirmed_return')).inputSchema,'discovery':[t.name for t in tools]})
    direct=(await native.call_tool('submit_confirmed_return',ToolContext('x','x','x'),{'operation_id':str(uuid4())})).data
    assert direct['error']['code']=='SESSION_CONTEXT_REQUIRED'
    raw_extra=[]
    for field in ('reason','quantity','confirmed','amount_cents','customer_id','order_id','item_id'):
     try:
      await native._client.call_tool('submit_confirmed_return',{'operation_id':str(uuid4()),field:'untrusted'})
      raise AssertionError('raw extra accepted')
     except AssertionError:raise
     except Exception as exc:raw_extra.append({'field':field,'rejected':type(exc).__name__})
   check('schema_single_model_parameter_no_legacy_MCP_and_direct_ID_not_authorization',{'direct':direct,'raw_extra':raw_extra})
   a=await case(web,folder,quantity=2)
   extra=[]
   for key,value in [('reason','尺寸不合'),('quantity',1),('confirmed',True),('amount_cents',1),('customer_id','demo-alice'),('order_id','DEMO-1001'),('item_id','DEMO-1001-HEADSET')]:
    try:await submit(a,folder,**{key:value});raise AssertionError('Extra field accepted')
    except AssertionError:raise
    except Exception as exc:extra.append({'field':key,'rejected':type(exc).__name__})
   assert counts(a['op'],a['args']['item_id'])['requests']==0
   conflict=biz.create_return_request(a['op'],**{**a['args'],'reason':'尺寸不合'})
   assert conflict['error']['code']=='IDEMPOTENCY_CONFLICT'
   check('new_tool_rejects_extra_parameters_legacy_still_strict',{'extra':extra,'legacy':conflict})
   results=await asyncio.gather(*[submit(a,folder) for _ in range(8)])
   assert all(x['ok'] for x in results) and len({x['data']['request']['id'] for x in results})==1
   made=results[0]['data']['request'];assert all(made[k]==a['args'][k] for k in a['args']) and made['amount_cents']==25800
   check('eight_concurrent_snapshot_submissions_exact_reason_quantity_and_single_request',{'request':made,'counts':counts(a['op'],a['args']['item_id'])})
   # Expired replay retains submitted request and creates no second notification.
   with connect(s.database_url) as db:db.execute("UPDATE return_operations SET expires_at=now()-interval '1 second' WHERE id=%s",(a['op'],))
   replay=await submit(a,folder);assert replay['ok'] and replay['data']['request']['id']==made['id']
   control(True);await wait(lambda:outbox(a['op'])['status']=='delivered')
   assert counts(a['op'],a['args']['item_id'])=={'requests':1,'reserved':2,'notifications':1,'canonical_receipts':1}
   check('expired_submitted_replay_same_result_single_outbox_receipt',replay)
   # B different line must retain A identity/current pointer; plain question does not become A receipt.
   b=await case(web,folder,item='DEMO-1001-MUG',sid=a['sid'],confirmed=False)
   r=await web.post(f"/sessions/{a['sid']}/events",json={'kind':'message','source':'customer','message':'刚才退货成功了吗'});r.raise_for_status()
   snap=await snapshot(web,a['sid'],folder/'AB_snapshot.json');last=[e for e in snap['events'] if e['kind']=='message' and e['source']=='ai_agent'][-1]
   assert last['metadata']['query_response'] and b['op'] in last['data']['message'] and '尚未提交' in last['data']['message'] and made['id'] not in last['data']['message']
   assert snap['current_operation']['operation_id']==b['op'] and (await submit(a,folder))['ok']
   check('A_submitted_B_unconfirmed_current_query_and_explicit_A_replay',{'query':last,'current':snap['current_operation']})
   # Fake/cross-session/customer and unconfirmed gates.
   denied=await submit(b,folder);assert not denied['ok']
   rejected=[]
   for label,op,sid in [('fake',str(uuid4()),b['sid']),('cross_session',a['op'],(await case(web,folder,item='DEMO-1001-MUG',confirmed=False))['sid'])]:
    try:await call('submit_confirmed_return',{'operation_id':op},sid,folder);raise AssertionError('unauthorized accepted')
    except BusinessError as exc:rejected.append({'case':label,'code':exc.code})
   async with open_stores(s.database_url) as stores:
    foreign=await stores[0].create_session(customer_id='demo-bob',agent_id='retail-persistent-demo',title='marked Bob fixture')
   try:await call('submit_confirmed_return',{'operation_id':a['op']},str(foreign.id),folder);raise AssertionError('foreign accepted')
   except BusinessError as exc:rejected.append({'case':'foreign_session','code':exc.code})
   other=RetailService(s.database_url,'demo-bob',True).submit_confirmed_return(a['op'],biz.issue_tool_context(a['sid']))
   assert not other['ok']
   check('unconfirmed_fake_cross_session_and_customer_denied_no_writes',{'unconfirmed':denied,'rejected':rejected,'other_customer':other})
   before=await snapshot(web,a['sid'],folder/'before_restart.json');servers.stop('app');servers.start('app')
   after=await snapshot(web,a['sid'],folder/'after_restart.json');check('native_full_snapshot_restarts_strictly',compare_snapshots(before,after))
   # Immutable prepared parameters; rollback leaves original unchanged.
   try:
    with connect(s.database_url) as db:db.execute("UPDATE return_operations SET reason='改写' WHERE id=%s",(b['op'],))
    raise AssertionError('snapshot changed')
   except Exception as exc:
    if isinstance(exc,AssertionError):raise
    immutable={'error_type':type(exc).__name__}
   check('database_rejects_snapshot_mutation',immutable)
   # Exact old authorization, both before and after already confirmed draft supersession.
   for already_confirmed in (False,True):
    old=await case(web,folder,item='DEMO-1001-MUG',confirmed=already_confirmed)
    new=await case(web,folder,item='DEMO-1001-MUG',quantity=2,sid=old['sid'],confirmed=False)
    old_reply=await web.post(f"/demo/sessions/{old['sid']}/confirm",json={'message':old['preview']['confirmation_phrase']})
    assert old_reply.status_code==409 and 'superseded' in old_reply.text
    newer=await submit(new,folder);legacy=biz.create_return_request(old['op'],**old['args']);old_submit=await submit(old,folder)
    assert not newer['ok'] and legacy['error']['code']=='OPERATION_SUPERSEDED' and old_submit['error']['code']=='OPERATION_SUPERSEDED'
    with connect(s.database_url) as db:
     frozen=db.execute('SELECT * FROM return_operations WHERE id=%s',(old['op'],)).fetchone();assert bool(frozen['confirmed_at'])==already_confirmed and str(frozen['superseded_by'])==new['op']
    check('literal_old_complete_token_rejected_'+str(already_confirmed),{'old_operation':old['op'],'new_operation':new['op'],'actual_sent_token':old['preview']['confirmation_phrase'],'http':old_reply.status_code,'body':old_reply.json(),'legacy_submit':legacy,'new_unconfirmed':newer,'old_snapshot':frozen})
   exp=await case(web,folder,item='DEMO-1001-MUG',confirmed=True)
   with connect(s.database_url) as db:db.execute("UPDATE return_operations SET expires_at=now()-interval '1 second' WHERE id=%s",(exp['op'],))
   assert (await submit(exp,folder))['error']['code']=='OPERATION_EXPIRED'
   oldr=await web.post(f"/demo/sessions/{exp['sid']}/confirm",json={'message':exp['preview']['confirmation_phrase']});assert oldr.status_code==409
   check('expired_not_submitted_cannot_confirm_or_submit',oldr.json())
   # Explicit replacement across different lines, while an independent line
   # does not revoke the other existing unsubmitted operation.
   original=await case(web,folder,item='DEMO-1001-MUG',confirmed=True)
   args={'order_id':'DEMO-1004','item_id':'DEMO-1004-HEADSET','quantity':1,'reason':'独立演示更改商品','replaces_operation_id':original['op']}
   changed=await call('check_return_eligibility',args,original['sid'],folder);assert changed['ok']
   denial=await web.post(f"/demo/sessions/{original['sid']}/confirm",json={'message':original['preview']['confirmation_phrase']});assert denial.status_code==409
   assert (await submit(original,folder))['error']['code']=='OPERATION_SUPERSEDED'
   independent=await case(web,folder,item='DEMO-1001-MUG',confirmed=True)
   otherline=await call('check_return_eligibility',{'order_id':'DEMO-1004','item_id':'DEMO-1004-HEADSET','quantity':1,'reason':'独立申请'},independent['sid'],folder);assert otherline['ok']
   assert (await submit(independent,folder))['ok']
   assert biz.session_operation(independent['sid'])['data']['operation_id']==otherline['data']['operation_id']
   check('explicit_cross_line_replacement_and_independent_draft_does_not_revoke_original',{'replaced':original,'changed':changed,'old_token_response':denial.json(),'independent':independent,'current_different_line':otherline})
   save(folder/'first_group_database.json',database_evidence())
  servers.close();reset();control(False);servers.start('mcp');servers.start('app')
  async with httpx.AsyncClient(base_url='http://127.0.0.1:8920',timeout=70) as web:
   # Successful native new-tool trace receipt followed by ordinary message.
   a=await case(web,folder)
   result=await submit(a,folder);assert result['ok']
   async with open_stores(s.database_url) as (store,*_):
    await store.create_event(SessionId(a['sid']),EventSource.AI_AGENT,EventKind.TOOL,'actual-submit-trace',{'tool_calls':[{'tool_id':'retail-demo-business:submit_confirmed_return','arguments':{'operation_id':a['op']},'result':{'data':result,'metadata':{},'control':{}}}]})
    receipt=await store.create_event(SessionId(a['sid']),EventSource.AI_AGENT,EventKind.MESSAGE,'actual-submit-trace',{'message':'will be replaced from DB'})
    assert receipt.metadata.get('outbox_id')
    await store.create_event(SessionId(a['sid']),EventSource.CUSTOMER,EventKind.MESSAGE,'later-turn',{'message':'之后只是查询杯子，不退货'})
    ordinary=await store.create_event(SessionId(a['sid']),EventSource.AI_AGENT,EventKind.MESSAGE,'actual-submit-trace',{'message':'普通问答夹具，白色350ml杯子'})
    assert not ordinary.metadata.get('outbox_id') and '普通问答夹具' in ordinary.data['message']
   control(True);assert counts(a['op'],a['args']['item_id'])=={'requests':1,'reserved':1,'notifications':1,'canonical_receipts':1}
   check('new_tool_trace_fixed_receipt_no_overwrite_later_same_trace_question',{'receipt':receipt.metadata,'ordinary':ordinary.data})
   save(folder/'trace_group_database.json',database_evidence())
  servers.close();reset();control(False);servers.start('mcp');servers.start('app')
  async with httpx.AsyncClient(base_url='http://127.0.0.1:8920',timeout=70) as web:
   a=await case(web,folder);(folder/'drop_mcp').write_text(a['op'])
   try:await submit(a,folder);raise AssertionError('Fault not triggered')
   except AssertionError:raise
   except Exception as exc:loss={'exception':type(exc).__name__}
   code=await asyncio.to_thread(servers.procs['mcp'].wait,10);assert code==77 and not (folder/'drop_mcp').exists()
   save(folder/'loss_committed_database.json',database_evidence());assert counts(a['op'],a['args']['item_id'])['requests']==1
   servers.stop('app');servers.stop('mcp');control(True);servers.start('mcp');servers.start('app')
   await wait(lambda:outbox(a['op'])['status']=='delivered')
   replays=await asyncio.gather(*[submit(a,folder) for _ in range(4)])
   assert all(x['ok'] for x in replays) and len({x['data']['request']['id'] for x in replays})==1
   for i in range(2):
    r=await web.post(f"/sessions/{a['sid']}/events",json={'kind':'message','source':'customer','message':'刚才退货成功了吗'});r.raise_for_status()
   assert counts(a['op'],a['args']['item_id'])=={'requests':1,'reserved':1,'notifications':1,'canonical_receipts':1}
   check('actual_exit77_after_commit_restart_concurrent_replay_and_query_unique',{'exit_code':code,**loss,'counts':counts(a['op'],a['args']['item_id'])})
   await snapshot(web,a['sid'],folder/'lost_recovered_snapshot.json');save(folder/'final_database.json',database_evidence())
  assert not list((root/'calls').glob('*.json')) if (root/'calls').exists() else True
  report['passed']=True
 except BaseException as exc:
  report.update(error_type=type(exc).__name__,error=str(exc));raise
 finally:
  servers.close();control(True);save(folder/'final_database_after_stop.json',database_evidence());save(folder/'report.json',report)
 print('APP-S1 offline passed:',len(report['checks']),flush=True)

if __name__=='__main__':asyncio.run(run())
