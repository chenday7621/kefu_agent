"""Positive and negative independent scorer checks before paid evaluation."""
import copy,json
from pathlib import Path
from .common import config,save
from .protocol import SCENARIOS,H
from .scoring import score,selftest


def main():
 root=Path(config()['result_dir']);initial=json.loads((root/'initial_fixture.json').read_text());final=copy.deepcopy(initial)
 sid='scorer-unit';op='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';rid='bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';oid='cccccccc-cccc-4ccc-8ccc-cccccccccccc'
 s=next(s for s in SCENARIOS if s['id']=='R1');phrase=f'确认退货 {op} 订单DEMO-1001 明细{H} 数量1'
 def event(eid,offset,kind,source,data,metadata=None):return {'id':eid,'session_id':sid,'offset':offset,'kind':kind,'source':source,'data':data,'metadata':metadata or {},'deleted':False}
 offer=event('offer',0,'message','ai_agent',{'message':'尺寸不合适\n'+phrase})
 confirmed=event('actual-user',1,'message','customer',{'message':phrase})
 submitted=event('tool',2,'tool','ai_agent',{'tool_calls':[{'tool_id':'retail-demo-business:create_return_request','arguments':{'operation_id':op}}]})
 receipt=event('receipt',3,'message','ai_agent',{'message':f'申请编号：{rid}\n申请金额：129.00 CNY\n状态：submitted\n仅申请已提交，尚未退款。'}, {'outbox_id':oid,'request_id':rid,'operation_id':op,'deterministic_operation_receipt':True})
 final['return_requests'].append({'id':rid,'operation_id':op,'customer_id':'demo-alice','order_id':'DEMO-1001','item_id':H,'quantity':1,'reason':'尺寸不合适','amount_cents':12900,'status':'submitted'})
 final['return_operations'].append({'id':op,'quantity':1,'item_id':H,'confirmed_at':'fixture'})
 final['session_operations'].append({'operation_id':op,'confirmation_event':confirmed})
 final['operation_logs'] += [{'operation_id':op,'action':a} for a in ('prepared','confirmed','submitted')]
 final['return_outbox'].append({'id':oid,'operation_id':op,'status':'delivered'})
 final['parlant_events']=[{'session_id':sid,'doc':e} for e in (offer,confirmed,submitted,receipt)]
 next(i for i in final['order_items'] if i['id']==H)['reserved_return_quantity']=1
 task={'scenario':s,'session_id':sid,'turns':[{'query':'fixture','answer':receipt['data']['message']}]}
 results={'basic':selftest(),'positive':score(task,initial,final)};assert results['positive']['status']=='pass'
 for variant in ('missing_confirmation','wrong_quantity','wrong_reason','duplicate_request','wrong_amount','wrong_feedback_amount','false_refund','no_native_tool','original_request_mutated'):
  bad=copy.deepcopy(final)
  if variant=='missing_confirmation':bad['session_operations'][-1]['confirmation_event']=None
  elif variant=='wrong_quantity':bad['return_requests'][-1]['quantity']=2
  elif variant=='wrong_reason':bad['return_requests'][-1]['reason']='different'
  elif variant=='duplicate_request':bad['return_requests'].append(copy.deepcopy(bad['return_requests'][-1]))
  elif variant=='wrong_amount':bad['return_requests'][-1]['amount_cents']=1
  elif variant=='wrong_feedback_amount':bad['parlant_events'][-1]['doc']['data']['message']=receipt['data']['message'].replace('129.00','128.00')
  elif variant=='false_refund':bad['parlant_events'][-1]['doc']['data']['message']+='退款已到账。'
  elif variant=='no_native_tool':bad['parlant_events']=[x for x in bad['parlant_events'] if x['doc']['kind']!='tool']
  elif variant=='original_request_mutated':bad['return_requests'][0]['reason']='changed'
  outcome=score(task,initial,bad);assert outcome['status']=='fail',variant;results[variant]=outcome['errors']
 save(root/'scorer_checks.json',results);print('Independent scorer checks: positive + 9 rejection variants passed; no model or business write.')

if __name__=='__main__':main()
