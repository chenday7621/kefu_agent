"""Frozen application plus supplementary logging and dual budget guard."""
import asyncio, json, os, time
from pathlib import Path
from uuid import uuid4
from datetime import datetime,timezone
from .common import config, activate, save, assert_isolated


def ledger():
 return Path(config()['result_dir'])/'calls'


def guard_cost(row):
 if row.get('status')=='completed' and row.get('usage'):
  u=row['usage'];p=u.get('prompt_tokens');o=u.get('completion_tokens');hit=u.get('prompt_cache_hit_tokens');miss=u.get('prompt_cache_miss_tokens')
  if p is not None and o is not None:
   return ((hit*.044+miss*1.32) if hit is not None and miss is not None else p*1.32)/1e6+o*3.96/1e6
 return row.get('reserved_upper_usd',0)


def install():
 from ..nlp import DemoDeepSeek
 import parlant.sdk as sdk
 assert sdk.PARLANT_HOME_DIR == Path(config()['result_dir'])/'parlant', 'SDK home is not isolated'
 from ..session_tools import SessionBoundMCP
 original=DemoDeepSeek.get_schematic_generator
 async def get(self,t,hints=None):
  gen=await original(self,t,hints)
  if getattr(gen,'_app_eval_observed',False):return gen
  gen._app_eval_observed=True
  call=gen._client.chat.completions.create
  async def observed(*args,**kwargs):
   directory=ledger();directory.mkdir(exist_ok=True)
   previous=[json.loads(p.read_text()) for p in directory.glob('*.json')]
   reserve=(len(json.dumps(kwargs.get('messages',[]),ensure_ascii=False).encode())*1.32+kwargs.get('max_tokens',8192)*3.96)/1e6
   auth=json.loads((Path(config()['audit_dir'])/'BUDGET_AUTHORIZATION.json').read_text())
   assert auth['additional_cap_usd']==1 and auth['cumulative_cap_usd']==3
   assert json.loads((Path(config()['runtime_dir'])/'phase.json').read_text())['attempt_id'] in ('r2_R1','r2_R6','completion_startup','completion_closing')
   original_dir=Path(config()['original_result_dir'])/'calls'
   original_rows=[json.loads(p.read_text()) for p in original_dir.glob('*.json')]
   assert len(original_rows)==257 and sum(not x.get('usage') for x in original_rows)==5
   prior=sum(guard_cost(x) for x in original_rows)
   incremental=sum(guard_cost(x) for x in previous)
   if incremental+reserve>auth['additional_cap_usd'] or prior+incremental+reserve>auth['cumulative_cap_usd']:
    save(Path(config()['result_dir'])/'STOP.json',{'reason':'supplement_dual_budget_cap','incremental_cap_usd':1,'cumulative_cap_usd':3,'prior_bound_usd':prior,'incremental_bound_usd':incremental,'next_reservation':reserve})
    raise RuntimeError('APP-S1 completion budget cap; no additional calls')
   consecutive=0
   for r in sorted(previous,key=lambda r:r['started_utc'],reverse=True):
    if r['status']=='completed':break
    if r['status']=='failed':consecutive+=1
   if consecutive>=3 or (Path(config()['result_dir'])/'STOP.json').exists():
    raise RuntimeError('APP-EVAL paused after persistent API error or budget stop')
   ctx=json.loads((Path(config()['runtime_dir'])/'phase.json').read_text())
   cid=uuid4().hex;path=directory/(cid+'.json');start=time.monotonic()
   row={**ctx,'invocation_id':cid,'trace_id':self._tracer.trace_id,'schema':t.__name__,'requested_model':kwargs.get('model'),'request':kwargs,'status':'in_flight','started_utc':datetime.now(timezone.utc).isoformat(),'reserved_upper_usd':reserve,'physical_http_requests':'unavailable','transport_retries':'unavailable','usage':None}
   save(path,row)
   try:
    response=await call(*args,**kwargs)
    row.update(status='completed',response_id=response.id,returned_model=response.model,response=response.model_dump(mode='json'),usage=response.usage.model_dump(mode='json') if response.usage else None)
    return response
   except BaseException as exc:
    row.update(status='failed',error_type=type(exc).__name__,http_status=getattr(exc,'status_code',None),usage=None)
    if getattr(exc,'status_code',None)==402:
     save(Path(config()['result_dir'])/'STOP.json',{'reason':'insufficient_balance','call_id':cid})
    elif consecutive+1>=3:
     save(Path(config()['result_dir'])/'STOP.json',{'reason':'three_consecutive_API_errors','call_id':cid})
    raise
   finally:
    row['seconds']=time.monotonic()-start;row['completed_utc']=datetime.now(timezone.utc).isoformat();save(path,row)
  gen._client.chat.completions.create=observed
  return gen
 DemoDeepSeek.get_schematic_generator=get
 native_call=SessionBoundMCP.call_tool
 async def tool(self,name,context,arguments):
  directory=Path(config()['result_dir'])/'mcp_calls';directory.mkdir(exist_ok=True)
  ctx=json.loads((Path(config()['runtime_dir'])/'phase.json').read_text())
  row={**ctx,'id':uuid4().hex,'name':name,'session_id':context.session_id,'arguments':{k:v for k,v in arguments.items() if k!='session_token'},'started_utc':datetime.now(timezone.utc).isoformat()};start=time.monotonic()
  try:
   result=await native_call(self,name,context,arguments);row.update(status='completed',result={'data':result.data,'metadata':result.metadata});return result
  except BaseException as exc:row.update(status='failed',error_type=type(exc).__name__);raise
  finally:row['seconds']=time.monotonic()-start;save(directory/(row['id']+'.json'),row)
 SessionBoundMCP.call_tool=tool


if __name__=='__main__':
 activate();assert_isolated()
 from ..settings import prepare_model_environment,load_settings
 prepare_model_environment(load_settings()) # set HF cache/offline/thread env before SDK/model imports
 install()
 from ..parlant_app import main
 main()
