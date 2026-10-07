"""Summarize all planned attempts, unknowns and cost; no rescoring or model calls."""
import json,math,statistics,subprocess,tarfile,hashlib,os,re
from pathlib import Path
from collections import Counter
from .common import config,save,digest,activate,database_evidence


def stats(values):
 values=sorted(values)
 return {'n':len(values),'median':statistics.median(values) if values else None,'p95_nearest_rank':values[math.ceil(.95*len(values))-1] if values else None}


def main():
 c=config();r=Path(c['result_dir']);audit=Path(c['audit_dir']);plan=json.loads((r/'plan.json').read_text());rows=[]
 for item in plan:
  p=r/'tasks'/item['attempt_id'];attempt=json.loads((p/'attempt.json').read_text()) if (p/'attempt.json').exists() else None
  scored=json.loads((p/'score.json').read_text()) if (p/'score.json').exists() else None
  rows.append({**item,'status':scored['status'] if scored else 'unscored' if attempt else 'not_run','attempt':attempt,'score':scored})
 def group(items):
  counts=Counter(i['status'] for i in items)
  return {'planned_denominator':len(items),'executed':sum(i['attempt'] is not None for i in items),'judgment_coverage':sum(i['score'] is not None for i in items),'passed':counts['pass'],'pass_rate_planned':counts['pass']/len(items) if items else None,'statuses':dict(counts)}
 summary={'all':group(rows),'repeats':{str(n):group([i for i in rows if i['repeat']==n]) for n in (1,2)},'groups':{g:group([i for i in rows if i['scenario']['group']==g]) for g in ('query','legal_return','reject','boundary')}}
 consistency=[]
 for sid in sorted({i['scenario']['id'] for i in rows}):
  pair=[i for i in rows if i['scenario']['id']==sid]
  consistency.append({'scenario':sid,'statuses':[x['status'] for x in pair],'both_scored':all(x['score'] for x in pair),'same_status':pair[0]['status']==pair[1]['status'],'same_request_count':all(x['score'] for x in pair) and pair[0]['score']['new_requests']==pair[1]['score']['new_requests']})
 summary['consistency']={'planned_pairs':20,'both_scored':sum(x['both_scored'] for x in consistency),'same_status_and_request_count':sum(x['both_scored'] and x['same_status'] and x['same_request_count'] for x in consistency),'pairs':consistency}
 calls=[json.loads(p.read_text()) for p in sorted((r/'calls').glob('*.json'))] if (r/'calls').exists() else []
 known=[];unknown=[];total_input=total_output=total_hit=total_miss=0
 for row in calls:
  u=row.get('usage');cost=None
  if u and u.get('prompt_tokens') is not None and u.get('completion_tokens') is not None:
   hit=u.get('prompt_cache_hit_tokens');miss=u.get('prompt_cache_miss_tokens');input_tokens=u['prompt_tokens'];output_tokens=u['completion_tokens']
   cost=((hit*.044+miss*1.32) if hit is not None and miss is not None else input_tokens*1.32)/1e6+output_tokens*3.96/1e6
   total_input+=input_tokens;total_output+=output_tokens
   if hit is not None:total_hit+=hit
   if miss is not None:total_miss+=miss
  row['reference_estimate_usd']=cost
  (known if cost is not None else unknown).append(row)
 cost=sum(x['reference_estimate_usd'] for x in known)
 bycategory={g:{'calls':sum(x.get('category')==g for x in calls),'known_reference_estimate_usd':sum(x.get('reference_estimate_usd') or 0 for x in calls if x.get('category')==g),'unknown_usage_calls':sum(x.get('reference_estimate_usd') is None and x.get('category')==g for x in calls)} for g in ('startup','formal','fault','closing')}
 bystatus={g:{'calls':sum(x['status']==g for x in calls),'known_reference_estimate_usd':sum(x.get('reference_estimate_usd') or 0 for x in calls if x['status']==g),'unknown_usage_calls':sum(x.get('reference_estimate_usd') is None and x['status']==g for x in calls)} for g in ('completed','failed','in_flight')}
 paid={'client_invocations':len(calls),'unique_invocation_ids':len({x['invocation_id'] for x in calls}),'unique_response_ids':len({x['response_id'] for x in calls if x.get('response_id')}),'known_usage_calls':len(known),'unknown_usage_calls':len(unknown),'known_input_tokens':total_input,'known_output_tokens':total_output,'known_cache_hit_tokens':total_hit,'known_cache_miss_tokens':total_miss,'known_reference_estimate_usd':cost,'total_reference_estimate_usd':cost if not unknown else None,'actual_alias_rate':'unavailable','bill':'unavailable','budget_cap_usd':5,'mean_known_subtotal_per_planned_task_usd':cost/40,'known_subtotal_divided_by_successes_usd':cost/summary['all']['passed'] if summary['all']['passed'] else None,'by_category':bycategory,'by_call_status':bystatus,'physical_http_calls_and_retries':'unavailable'}
 per_task_cost={i['attempt_id']:{'calls':len([x for x in calls if x.get('attempt_id')==i['attempt_id']]),'known_reference_estimate_usd':sum(x['reference_estimate_usd'] or 0 for x in calls if x.get('attempt_id')==i['attempt_id']),'unknown_usage_calls':sum(x['reference_estimate_usd'] is None for x in calls if x.get('attempt_id')==i['attempt_id'])} for i in rows}
 failed_turns=[{'attempt_id':i['attempt_id'],**t} for i in rows if i['attempt'] for t in i['attempt']['turns'] if t.get('outcome')!='answered']
 mcps=[json.loads(p.read_text()) for p in (r/'mcp_calls').glob('*.json')] if (r/'mcp_calls').exists() else []
 latency={'whole_task_all':stats([i['attempt']['seconds'] for i in rows if i['attempt'] and 'seconds' in i['attempt']]),'whole_task_passed':stats([i['attempt']['seconds'] for i in rows if i['status']=='pass']),'whole_task_failed_or_review':stats([i['attempt']['seconds'] for i in rows if i['status'] in ('fail','needs_review')]),'turn_answered':stats([t['seconds'] for i in rows if i['attempt'] for t in i['attempt']['turns'] if t.get('outcome')=='answered']),'failed_turns':failed_turns,'MCP_formal':stats([x['seconds'] for x in mcps if x.get('category')=='formal']),'model_invocation':stats([x['seconds'] for x in calls]),'receipt_delivery_seconds':stats([]),'note':'wall durations; model concurrency durations are not summed into task latency'}
 receipts=[]
 from datetime import datetime
 for i in rows:
  f=r/'tasks'/i['attempt_id']/'final_database.json'
  if f.exists():
   for o in json.loads(f.read_text())['return_outbox']:
    if o['completed_at']:receipts.append((datetime.fromisoformat(o['completed_at'])-datetime.fromisoformat(o['created_at'])).total_seconds())
 latency['receipt_delivery_seconds']=stats(receipts)
 faults=[json.loads(p.read_text()) for p in sorted((r/'faults').glob('*/report.json'))] if (r/'faults').exists() else []
 fault_summary={'planned_denominator':18,'executed':len(faults),'injection_valid':sum(x['injection_triggered'] for x in faults),'passed':sum(x['passed'] for x in faults),'by_kind':{k:{'planned':3,'executed':sum(x['kind']==k for x in faults),'valid_injections':sum(x['kind']==k and x['injection_triggered'] for x in faults),'passed':sum(x['kind']==k and x['passed'] for x in faults),'automatic_recovery_applicable':sum(x['kind']==k and x['automatic_recovery'] is not None for x in faults),'automatic_recovered':sum(x['kind']==k and x['automatic_recovery'] is True for x in faults),'manual_recovered':sum(x['kind']==k and x['manual_recovery'] is True for x in faults)} for k in ('pending_restart','lost_mcp_response','temporary_receipt_failure','post_delivery_crash','interleaved_recovery','failed_manual_retry')},'recovery_seconds':stats([x['recovery_seconds'] for x in faults if x.get('recovery_seconds') is not None]),'manual_recovery_seconds':stats([x['manual_recovery_seconds'] for x in faults if x.get('manual_recovery_seconds') is not None]),'model_calls':sum(x['model_calls'] for x in faults)}
 summary['observations']={'wrong_tool_attempts':sum(len(i['score']['wrong_tool_attempts']) for i in rows if i['score']),'backend_rejections':sum(len(i['score']['backend_rejections']) for i in rows if i['score']),'actual_wrong_writes':sum(len(i['score']['actual_wrong_writes']) for i in rows if i['score']),'false_refund_claims':sum(i['score']['false_refund_claim'] for i in rows if i['score']),'wrong_displayed_request_ids':sum(len(i['score']['wrong_displayed_request_ids']) for i in rows if i['score']),'wrong_displayed_amounts':sum(len(i['score']['wrong_displayed_amounts']) for i in rows if i['score']),'wrong_displayed_statuses':sum(len(i['score']['wrong_displayed_statuses']) for i in rows if i['score']),'response_types':dict(sum((Counter(i['score']['response_type_counts']) for i in rows if i['score']),Counter())),'unbounded_free_text_accuracy':'unreviewed'}
 save(r/'metrics.json',{'natural_language':summary,'faults':fault_summary,'usage':paid,'latency':latency});save(r/'task_results.json',rows);save(r/'fault_results.json',faults);save(r/'usage_summary.json',{'all_calls':calls,'aggregate':paid,'per_task':per_task_cost})
 lines=['# APP-EVAL-V1（2026-10-06）','', '当前固定演示身份、单 Parlant 实例应用的自定义验收基线；不是官方 benchmark、真人浏览器测试或基础设施改造的因果增益。业务代码、规则和依赖未优化。', '', '## 自然语言场景','', '| 分组 | 计划分母 | 已执行 | 可判定覆盖 | 通过 | 计划通过率 | 状态 |','| --- | ---: | ---: | ---: | ---: | ---: | --- |']
 for label,g in [('全部',summary['all'])]+[(f'重复{n}',v) for n,v in summary['repeats'].items()]+list(summary['groups'].items()):
  lines.append(f"| {label} | {g['planned_denominator']} | {g['executed']} | {g['judgment_coverage']} | {g['passed']} | {g['pass_rate_planned']:.2%} | {g['statuses']} |")
 lines+=['','pass 仅表示预定客观业务验收项通过。needs_review 不默认通过；自由文本事实正确性仍 unreviewed，不伪造人工标签。合法请求完成率的主分母为8个合法场景×2；拒绝组为4×2，边界组另列。固定回执与 query_response 不计为模型事实准确率。','','## 无模型故障','',f"计划18次，执行{len(faults)}次，有效注入{fault_summary['injection_valid']}次，通过{fault_summary['passed']}次。与40次对话不合并成功率。",'', '| 类型 | 计划 | 执行 | 注入有效 | 通过 | 自动恢复 | 人工恢复 |','| --- | ---: | ---: | ---: | ---: | ---: | ---: |']
 for k,g in fault_summary['by_kind'].items():lines.append(f"| {k} | 3 | {g['executed']} | {g['valid_injections']} | {g['passed']} | {g['automatic_recovered']}/{g['automatic_recovery_applicable']} | {g['manual_recovered']} |")
 lines+=['','delivered 仅表示完整回执事件落库，不表示已读。query_response 不算重复提交回执。故障夹具明确标记，无模型；每次使用新会话并保存实际触发、重启前后快照及 SQL/MCP 证据。','','## 用量与成本','',f"唯一客户端调用 {len(calls)}；已知输入 {total_input}、输出 {total_output}、cache hit {total_hit}、miss {total_miss}；未知 usage 调用 {len(unknown)}。已知参考估算小计 USD {cost:.6f}。预算 USD5。",'', '当前[DeepSeek官方价格页](https://api-docs.deepseek.com/quick_start/pricing)未公布 deepseek-chat 别名费率，保存执行前页面及抓取时间，使用最高峰时 Pro 价格作保守参考预算估算；不能称为别名实际计费或账单。未知用量保留 unknown 与已知小计。启动/正式/异常与每题成本详见 usage_summary.json；物理 HTTP/内部重试 unavailable。', '', f"全部计划任务平均已知小计 USD {cost/40:.6f}；已知小计/通过场景数：{paid['known_subtotal_divided_by_successes_usd']}。若有未执行/未知用量，不能当作完整任务平均实际账单。",'', '## 延迟与错误','', 'latency 与 observations 完整见 metrics.json（中位数、P95 nearest-rank、样本量；失败/超时单列）。模型并行耗时没有加总成任务耗时。错误工具尝试、后端拒绝和实际错误写入分别统计。', '', '## 复现与完整性','', '场景/两轮顺序：scenarios.json / plan.json；初始夹具：initial_fixture.json；源码/迁移/依赖/模型/有效元数据：protocol_source_hashes.json、source_baseline.json、dependencies.txt、embedding_files.json、effective_metadata.json。各轮/各次重启的完整内部元数据也单独保存，只允许原生 transient Store 创建时间字段差异，ID、规则及参数保持一致。', '', '续跑命令（私密配置仅保存在本地）：', '', '```bash',f"export APP_EVAL_CONFIG=runtime-data/retail-demo/app_eval_v1/{c['stamp']}/private.json",'apps/retail_demo/.venv/bin/python -m apps.retail_demo.eval_v1.runner run','apps/retail_demo/.venv/bin/python -m apps.retail_demo.eval_v1.faults','apps/retail_demo/.venv/bin/python -m apps.retail_demo.eval_v1.report','```','', '已评分 attempt 不再调用模型；STOP 不自动清除或追加预算。setup 不可对本轮重复执行。所有旧 failed 报告原样保留，原演示状态与停止测试记录见 final_isolation.json。上传包与 SHA-256 见审计目录 UPLOAD_SHA256.txt。']
 if (r/'STOP.json').exists():lines+=['','暂停原因：`'+(r/'STOP.json').read_text().strip()+'`；未完成计划仍在 task_results.json。']
 (audit/'REPORT.md').write_text('\n'.join(lines)+'\n');(r/'REPORT.md').write_bytes((audit/'REPORT.md').read_bytes())
 print(json.dumps({'overall':summary['all'],'faults':fault_summary,'calls':len(calls),'known_reference_usd':cost},ensure_ascii=False,indent=2))

if __name__=='__main__':main()
