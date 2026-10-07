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
  key='mcp' if name=='mcp' else 'app';module='apps.retail_demo.mcp_server' if key=='mcp' else 'apps.retail_demo.eval_s1.launcher'
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

