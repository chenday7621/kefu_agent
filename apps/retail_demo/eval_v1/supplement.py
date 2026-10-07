"""Read-only post-eval uncertainty/tariff/coverage notes, never change frozen scores."""
import json
from pathlib import Path
from datetime import datetime
from collections import Counter
from .common import config,save


def main():
 r=Path(config()['result_dir']);calls=[json.loads(p.read_text()) for p in (r/'calls').glob('*.json')];records=[]
 for row in calls:
  model=row.get('returned_model');u=row.get('usage');estimate=None;band=None
  # Current official Flash price captured before execution. Alias billing inferred
  # from returned model, not advertised as a verified invoice/alias mapping.
  if model=='deepseek-flash' and u and all(u.get(k) is not None for k in ('prompt_cache_hit_tokens','prompt_cache_miss_tokens','completion_tokens')):
   dt=datetime.fromisoformat(row['started_utc']);peak_possible=dt.weekday()<5 and (1<=dt.hour<4 or 6<=dt.hour<10)
   factor=2 if peak_possible else 1;band='peak conservative (Chinese holiday schedule not inferred)' if peak_possible else 'off_peak outside peak UTC windows'
   estimate=(u['prompt_cache_hit_tokens']*.003+u['prompt_cache_miss_tokens']*.15+u['completion_tokens']*.6)/1e6*factor
  records.append({'invocation_id':row['invocation_id'],'attempt_id':row.get('attempt_id'),'category':row.get('category'),'status':row['status'],'requested_model':row['requested_model'],'returned_model':model,'reference_flash_estimate_usd':estimate,'time_band':band})
 known=sum(x['reference_flash_estimate_usd'] or 0 for x in records);unknown=sum(x['reference_flash_estimate_usd'] is None for x in records)
 from .protocol import SCENARIOS
 tasks=[];uncertain=[];recoveries=[];locating=[]
 for p in sorted((r/'tasks').glob('*/attempt.json')):
  t=json.loads(p.read_text());scorepath=p.parent/'score.json';s=json.loads(scorepath.read_text()) if scorepath.exists() else None
  if not s:continue
  tasks.append((t,s))
  if t.get('recovery_count'):recoveries.append({'attempt_id':t['attempt_id'],'count':t['recovery_count'],'original_attempt':str(p.parent/'original_attempt_before_adapter_recovery.json'),'completed_first_turn_replayed':False})
  if s['wrong_displayed_amounts']:
   for flag in s['wrong_displayed_amounts']:
    db=json.loads((p.parent/'final_database.json').read_text());event=next(x['doc'] for x in db['parlant_events'] if x['id']==flag['event_id'])
    # Multiple entities invalidate simplistic same-message amount attribution.
    body=event['data'].get('message','');prepared=sum(o['id'] in body for o in db['return_operations']);submitted=sum(q['id'] in body for q in db['return_requests'])
    if prepared and submitted:
     uncertain.append({'attempt_id':t['attempt_id'],'flag':flag,'frozen_score_status_preserved':s['status'],'interpretation':'needs_review: reply mentions a submitted request and a separately prepared operation; flat amount attribution can be ambiguous','human_label':'unreviewed','original_text':body,'do_not_count_as_verified_factual_error':True})
  if t['scenario']['id'] in ('R2','R3'):
   locating.append({'attempt_id':t['attempt_id'],'provided_preset_ID_correction':any(x['query'].startswith('请查询我的订单确定商品。我明确只退订单') for x in t['turns']),'scope':'completion with predeclared customer clarification, not an autonomous no-ID locating score'})
 counts={'planned':40,'scored':len(tasks),'resolved_auto_pass_or_fail':sum(s['status'] in ('pass','fail') for _,s in tasks),'needs_review':sum(s['status']=='needs_review' for _,s in tasks),'LLM_triggered_tasks':sum(any(x.get('attempt_id')==t['attempt_id'] for x in calls) for t,_ in tasks),'fixed_route_only_tasks':sum(not any(x.get('attempt_id')==t['attempt_id'] for x in calls) for t,_ in tasks)}
 passes=sum(s['status']=='pass' for _,s in tasks)
 tariff={'records':records,'known_reference_flash_subtotal_usd':known,'unknown_usage_or_model_calls':unknown,'total_reference_flash_estimate_usd':known if not unknown else None,'mean_known_subtotal_per_planned_task':known/40,'mean_known_subtotal_per_executed_task':known/len(tasks) if tasks else None,'known_subtotal_per_frozen_success':known/passes if passes else None,'alias_actual_bill':'unavailable','basis':'official saved Flash rates, inferred from actual returned model; requested alias mapping/bill not independently verified','budget_estimator_unchanged':'frozen conservative maximum peak Pro estimator and USD5 cap retained','by_category':{g:{'calls':sum(x['category']==g for x in records),'known_flash_reference_usd':sum(x['reference_flash_estimate_usd'] or 0 for x in records if x['category']==g),'unknown':sum(x['reference_flash_estimate_usd'] is None for x in records if x['category']==g)} for g in ('startup','formal','fault','closing')}}
 save(r/'price_inference.json',tariff);save(r/'review_notes.json',{'coverage':counts,'amount_flags_requiring_review':uncertain,'infrastructure_observation_recovery':recoveries,'locating_task_scope':locating,'automatic_scores_never_replaced_or_regraded':True,'historical_preflight_failure_artifacts':['preflight_aborted_before_sdk_home_fix/report.json','preflight_sdk_home_fixed_embedding_env_late/report.json']})
 print(json.dumps({'coverage':counts,'known_flash_reference_usd':known,'unknown_calls':unknown,'ambiguous_amount_flags':len(uncertain)},ensure_ascii=False,indent=2))

if __name__=='__main__':main()
