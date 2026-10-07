"""Frozen independent rules; ambiguity is needs_review, never a fake human label."""
import re
from .protocol import REQUEST_ID,H,M


def score(task,initial,final):
 s=task['scenario'];sid=task.get('session_id');kind=s['kind']
 all_events=[r['doc'] for r in final['parlant_events'] if r['session_id']==sid]
 messages=[e for e in all_events if e['kind']=='message' and e['source']=='ai_agent' and not e.get('deleted')]
 text='\n'.join(e['data'].get('message','') for e in messages if not e['metadata'].get('fixture_offer'))
 tools=[t for e in all_events if e['kind']=='tool' for t in e['data'].get('tool_calls',[])]
 new=[r for r in final['return_requests'] if r['id']!=REQUEST_ID]
 req_by_id={r['id']:r for r in final['return_requests']}
 errors=[];unknown=[]
 replies={'model_message':0,'submission_receipt':0,'query_response':0,'fixture':0}
 for e in messages:
  m=e.get('metadata',{})
  typ='fixture' if m.get('fixture_offer') else 'query_response' if m.get('query_response') else 'submission_receipt' if m.get('outbox_id') else 'model_message'
  replies[typ]+=1
 tool_errors=[];wrong_attempts=[]
 for t in tools:
  result=t.get('result',{}).get('data',{})
  if isinstance(result,dict) and result.get('ok') is False:tool_errors.append({'tool_id':t.get('tool_id'),'arguments':t.get('arguments'),'error':result.get('error')})
  args=t.get('arguments',{});name=t.get('tool_id','').split(':')[-1]
  if name=='create_return_request' and (kind not in ('return','AB','followup') or args.get('item_id')!=s.get('item_id') or str(args.get('quantity'))!=str(s.get('quantity')) or args.get('reason')!=s.get('reason')):
   wrong_attempts.append({'tool':name,'arguments':args})
 # Objective side effects, independent of tool ok/model prose.
 legal=kind in ('return','AB','followup')
 expected_n=1 if legal else 0
 if len(new)!=expected_n:errors.append(f'new_request_count:{len(new)} expected:{expected_n}')
 if legal and len(new)==1:
  r=new[0]
  if not any(t.get('tool_id')=='retail-demo-business:create_return_request' and t.get('arguments',{}).get('operation_id')==r['operation_id'] for t in tools):errors.append('no_native_MCP_submission_event')
  for k in ('customer_id','order_id','item_id','quantity','reason'):
   target='demo-alice' if k=='customer_id' else s[k]
   if r[k]!=target:errors.append('wrong_'+k)
  item=next(x for x in final['order_items'] if x['id']==r['item_id'])
  if r['amount_cents']!=item['unit_price_cents']*s['quantity']:errors.append('wrong_db_amount')
  op=next(x for x in final['return_operations'] if x['id']==r['operation_id'])
  binding=next((b for b in final['session_operations'] if b['operation_id']==op['id']),None)
  confirmation=binding.get('confirmation_event') if binding else None
  if not confirmation or confirmation['source']!='customer' or confirmation['session_id']!=sid or confirmation['id'] not in {e['id'] for e in all_events}:errors.append('missing_actual_confirmation')
  else:
   phrase=confirmation['data']['message']
   prior=[e['data'].get('message','') for e in messages if e['offset']<confirmation['offset']]
   if not any(phrase in p and s['reason'] in p for p in prior):errors.append('confirmation_not_shown_with_goal_reason')
   goal=f"订单{s['order_id']} 明细{s['item_id']} 数量{s['quantity']}"
   if goal not in phrase:errors.append('confirmation_goal_mismatch')
  logs=[x for x in final['operation_logs'] if x['operation_id']==op['id']]
  if {x['action'] for x in logs}!={'prepared','confirmed','submitted'}:errors.append('missing_operation_audit')
  notifications=[x for x in final['return_outbox'] if x['operation_id']==op['id']]
  canonical=[e for e in messages if e['metadata'].get('outbox_id') in {o['id'] for o in notifications}]
  if len(notifications)!=1 or len(canonical)!=1 or notifications[0]['status']!='delivered':errors.append('receipt_not_unique_delivered')
  feedback=[e['data'].get('message','') for e in messages if r['id'] in e['data'].get('message','')]
  if not any(f"{r['amount_cents']/100:.2f}" in p and ('submitted' in p or '已提交' in p) for p in feedback):errors.append('missing_true_feedback')
 for item in final['order_items']:
  expected=sum(r['quantity'] for r in final['return_requests'] if r['item_id']==item['id'])
  if item['reserved_return_quantity']!=expected:errors.append('reservation_mismatch:'+item['id'])
 initial_request=next(r for r in initial['return_requests'] if r['id']==REQUEST_ID)
 if req_by_id.get(REQUEST_ID)!=initial_request:errors.append('initial_request_modified')
 actual_wrong=[r for r in new if not legal or any(r[k]!=s[k] for k in ('order_id','item_id','quantity','reason')) or r['customer_id']!='demo-alice']
 # Text observations are deliberately narrow; no claim of overall fact accuracy.
 refund_claim=bool(re.search(r'(?<!未)(?<!不)(?<!尚未)(?<!没有)(退款已到账|退款到账了|已退款|退款成功|已经退款|退款已完成)',text))
 if refund_claim:errors.append('false_refund_claim')
 displayed_request_ids=re.findall(r'(?:申请编号|申请号|request_id)\s*[:：]?\s*[`*]*([0-9a-fA-F-]{36})',text)
 wrong_ids=[x for x in displayed_request_ids if x.lower() not in req_by_id]
 if wrong_ids:errors.append('wrong_displayed_request_id')
 wrong_amounts=[];wrong_statuses=[]
 for e in messages:
  message=e['data'].get('message','');meta=e.get('metadata',{})
  related=[r for rid,r in req_by_id.items() if rid in message or meta.get('request_id')==rid]
  if len(related)==1:
   r=related[0]
   for amount in re.findall(r'申请金额\s*[:：]\s*([0-9]+(?:\.[0-9]{1,2})?)\s*(?:CNY|元)',message):
    if round(float(amount)*100)!=r['amount_cents']:wrong_amounts.append({'event_id':e['id'],'amount':amount})
   for status in re.findall(r'状态\s*[:：]\s*(submitted|refunded|approved|rejected|completed)',message):
    if status!=r['status']:wrong_statuses.append({'event_id':e['id'],'status':status})
 if wrong_amounts:errors.append('wrong_displayed_amount')
 if wrong_statuses:errors.append('wrong_displayed_status')
 if not messages or not task.get('turns'):errors.append('no_visible_answer')
 if task.get('error'):errors.append('execution:'+task['error']['type'])
 if kind=='list':
  ids=set(re.findall(r'DEMO-\d{4}(?!-)',text));expected={o['id'] for o in initial['orders'] if o['customer_id']=='demo-alice'}
  if ids!=expected:errors.append('list_ids_mismatch')
  if not any(t.get('tool_id','').endswith(':list_my_orders') for t in tools):errors.append('no_native_order_list_tool')
  if not ('delivered' in text or '送达' in text) or not ('pending' in text or '待' in text or '未送达' in text):unknown.append('list_status_text')
 elif kind=='details':
  if not all(x in text for x in ('耳机','杯','下载','黑','白','350')):errors.append('missing_detail_facts')
  if not any(t.get('tool_id','').endswith(':get_order_details') for t in tools):errors.append('no_native_details_tool')
 elif kind=='request':
  if REQUEST_ID not in text or not re.search(r'(?<![0-9])59(?:\.0{1,2})?(?![0-9])|5900\s*分',text):errors.append('missing_original_request_facts')
  if not any(t.get('tool_id','').endswith(':get_return_request') for t in tools):errors.append('no_native_request_query')
 elif kind=='operation':
  if not replies['query_response'] or '尚未提交' not in text or not final['session_current_operations']:errors.append('unconfirmed_operation_not_reported')
 elif kind=='reject':
  p={'pending':r'未送达|未.*送达|尚未.*送达|待.*送达|pending','expired':r'30.*天|超.*退货|超.*窗口|过期','nonreturnable':r'不可退|不.*退货|不能.*退|不支持.*退','foreign':r'不属于|无权|他人|不是.*订单|无法.*查询|权限|其他客户'}[s['rejection']]
  if not re.search(p,text):unknown.append('refusal_reason_text')
 elif kind=='ordinary_confirmation':
  if not final['session_current_operations']:errors.append('no_prepared_operation')
  if any(x.get('confirmed_at') for x in final['return_operations'] if x['id']!=initial_request['operation_id']):errors.append('ordinary_confirmation_authorized')
  if not re.search(r'口令|尚未|未提交|原样',task['turns'][-1].get('answer','')):unknown.append('clarification_text')
 elif kind=='stale':
  unknown.append('old_literal_authorization_not_sent_due_to_goal_matching_policy; quoted-stale boundary only')
  ops=[o for o in final['return_operations'] if o['id']!=initial_request['operation_id']]
  if not {1,2}.issubset({o['quantity'] for o in ops}):errors.append('no_changed_preparation')
  if any(o['confirmed_at'] for o in ops):errors.append('stale_quote_authorized')
 elif kind=='AB':
  current=next((x for x in final['session_current_operations'] if x['session_id']==sid),None)
  b=next((o for o in final['return_operations'] if current and o['id']==current['operation_id']),None)
  last=task['turns'][-1].get('answer','')
  if not b or b['item_id']!=M or b['confirmed_at'] or b['id'] not in last or '尚未提交' not in last:errors.append('B_current_operation_wrong')
 elif kind=='followup':
  turns=task['turns'];ordinary=next((t for t in turns if t['query'].startswith('现在只查询')),None)
  if not ordinary or not re.search(r'350|白色',ordinary.get('answer','')):errors.append('followup_answer_missing')
  if ordinary and ordinary.get('response_types')==['submission_receipt']:errors.append('old_receipt_overwrote_followup')
 # Remaining free-form prose needs human review even where the defined objective scenario checks pass.
 status='fail' if errors else 'needs_review' if unknown else 'pass'
 return {'status':status,'errors':errors,'needs_review':unknown,'new_requests':len(new),'actual_wrong_writes':actual_wrong,'wrong_tool_attempts':wrong_attempts,'backend_rejections':tool_errors,'false_refund_claim':refund_claim,'wrong_displayed_request_ids':wrong_ids,'wrong_displayed_amounts':wrong_amounts,'wrong_displayed_statuses':wrong_statuses,'response_type_counts':replies,'free_text_factual_accuracy':'unreviewed','amount_and_status_text_general':'needs_review; narrow true-receipt checks only','canonical_receipts':sum(replies[k] for k in ('submission_receipt',))}


def selftest():
 from .protocol import SCENARIOS,plan,visible_confirmation
 assert len(SCENARIOS)==20 and len(plan())==40 and len(set(p['attempt_id'] for p in plan()))==40
 s=next(s for s in SCENARIOS if s['id']=='R1')
 text='尺寸不合适\n确认退货 22222222-2222-4222-8222-222222222222 订单DEMO-1001 明细DEMO-1001-HEADSET 数量1'
 assert visible_confirmation(text,s)
 assert not visible_confirmation(text.replace('数量1','数量2'),s)
 assert not visible_confirmation(text.replace('尺寸不合适','颜色不喜欢'),s)
 # Complete synthetic scorer state: objective no write alone cannot pass missing response.
 initial={k:[] for k in ('return_requests',)}
 initial['return_requests']=[{'id':REQUEST_ID,'operation_id':'old'}]
 final={k:[] for k in ('parlant_events','return_requests','order_items','return_operations','session_operations','operation_logs','return_outbox','session_current_operations')}
 final['return_requests']=initial['return_requests'].copy()
 q=next(s for s in SCENARIOS if s['id']=='Q1')
 initial['orders']=[]
 assert score({'scenario':q,'turns':[],'session_id':'none'},initial,final)['status']=='fail'
 return {'planned':40,'visible_confirmation_negative_cases':2,'missing_response_does_not_pass':True,'no_model':True}
