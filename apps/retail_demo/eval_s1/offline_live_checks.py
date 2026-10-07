"""Directly related live-fact and legacy authorization checks, zero model."""
import asyncio
from pathlib import Path
import httpx
from .common import config,activate,save,phase,database_evidence
from .offline_helpers import Servers,control,call,counts
from .offline import case,submit
from .fixtures import reset
from ..business import RetailService
from ..db import connect
from ..settings import load_settings

async def run():
 activate();root=Path(config()['result_dir']);f=root/'offline_live_checks';f.mkdir();phase('offline_live_checks','offline');servers=Servers(f);checks=[]
 try:
  reset();save(f/'initial_database.json',database_evidence());control(False);servers.start('mcp');servers.start('app');s=load_settings();biz=RetailService(s.database_url,s.customer_id,True)
  async with httpx.AsyncClient(base_url='http://127.0.0.1:8920',timeout=70) as web:
   c=await case(web,f,confirmed=False)
   old=biz.create_return_request(c['op'],**c['args']);assert not old['ok']
   stamped=biz.confirm_operation(c['op'],c['preview']['confirmation_phrase'],'forged-model-helper');assert stamped['ok']
   result=await submit(c,f);legacy=biz.create_return_request(c['op'],**c['args']);assert result['error']['code']==legacy['error']['code']=='CONFIRMATION_EVENT_REQUIRED'
   checks.append({'name':'legacy_stamp_is_not_actual_confirmation_for_either_write_path','old_unconfirmed':old,'helper_stamp':stamped,'new':result,'legacy':legacy})
   for field,value,error in [('unit_price_cents',12800,'ORDER_FACTS_CHANGED'),('quantity',0,'QUANTITY_EXCEEDS_AVAILABLE')]:
    # Quantity schema won't allow0, use actual valid total quantity1 after quote2.
    q=await case(web,f,item='DEMO-1001-MUG',quantity=2,confirmed=True)
    with connect(s.database_url) as db:
     db.execute('UPDATE order_items SET '+field+'=%s WHERE id=%s',(value if field=='unit_price_cents' else 1,q['args']['item_id']))
    result=await submit(q,f);assert result['error']['code']==error,result
    checks.append({'name':'first_submit_rechecks_live_'+field,'result':result,'counts':counts(q['op'],q['args']['item_id'])})
    with connect(s.database_url) as db:db.execute('UPDATE order_items SET quantity=2,unit_price_cents=3900 WHERE id=%s',(q['args']['item_id'],))
   q=await case(web,f,item='DEMO-1001-MUG',confirmed=True)
   with connect(s.database_url) as db:db.execute("UPDATE orders SET status='pending' WHERE id='DEMO-1001'")
   result=await submit(q,f);assert result['error']['code']=='ORDER_NOT_DELIVERED'
   checks.append({'name':'first_submit_rechecks_live_order_status','result':result})
   save(f/'final_database.json',database_evidence())
   assert not [r for r in database_evidence()['return_requests'] if r['id']!='11111111-1111-4111-8111-111111111111']
 finally:
  servers.close();control(True);save(f/'report.json',{'passed':len(checks)==4,'checks':checks,'model_calls':0})
 print('Additional authorization/live-fact checks passed:',len(checks),flush=True)
if __name__=='__main__':asyncio.run(run())
