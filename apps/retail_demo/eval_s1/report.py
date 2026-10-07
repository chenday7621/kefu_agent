"""Append-only S1 report using frozen V1 score plus separately recorded observations."""
import json,statistics,math
from collections import Counter
from pathlib import Path
from .common import config,save,digest
from .launcher import guard_cost

def stats(v):
 v=sorted(v);return {'n':len(v),'median':statistics.median(v) if v else None,'p95_nearest_rank':v[math.ceil(.95*len(v))-1] if v else None}

def main():
 c=config();r=Path(c['result_dir']);a=Path(c['audit_dir']);plan=json.loads((r/'plan.json').read_text());rows=[]
 for p in plan:
  d=r/'tasks'/p['attempt_id'];attempt=json.loads((d/'attempt.json').read_text()) if (d/'attempt.json').exists() else None;score=json.loads((d/'score.json').read_text()) if (d/'score.json').exists() else None
  baseline=json.loads((Path('results/app_eval_v1_20261006_151637/tasks')/p['attempt_id']/'score.json').read_text())
  rows.append({**p,'attempt':attempt,'score':score,'status':score['status'] if score else 'unscored' if attempt else 'not_run','V1_status':baseline['status'],'V1_errors':baseline['errors']})
 calls=[json.loads(p.read_text()) for p in (r/'calls').glob('*.json')];perphase={};usage=Counter();known=0;unknown=[];flashcost=0;flashunknown=[]
 for x in calls:
  phase=x['category'];perphase.setdefault(phase,{'calls':0,'known_guard_usd':0,'unknown_usage':0});perphase[phase]['calls']+=1
  u=x.get('usage')
  if u and all(u.get(k) is not None for k in ('prompt_tokens','completion_tokens','prompt_cache_hit_tokens','prompt_cache_miss_tokens')):
   for k in ('prompt_tokens','completion_tokens','prompt_cache_hit_tokens','prompt_cache_miss_tokens'):usage[k]+=u[k]
   cost=guard_cost(x);known+=cost;perphase[phase]['known_guard_usd']+=cost
   # Official page/time reference only; deepseek-chat alias actual bill unknown.
   from datetime import datetime
   t=datetime.fromisoformat(x['started_utc']);offpeak=t.hour not in (1,2,3,6,7,8,9) or t.weekday()>=5
   if x.get('returned_model')=='deepseek-flash' and offpeak:
    flashcost+=(u['prompt_cache_hit_tokens']*.003+u['prompt_cache_miss_tokens']*.15+u['completion_tokens']*.6)/1e6
   else:flashunknown.append(x['invocation_id'])
  else:unknown.append(x['invocation_id']);perphase[phase]['unknown_usage']+=1
 for p in ('startup','formal','offline','closing'):perphase.setdefault(p,{'calls':0,'known_guard_usd':0,'unknown_usage':0})
 passed=sum(x['status']=='pass' for x in rows);scored=[x for x in rows if x['score']]
 conflicts=[];bad_schema=[];submission_count=0
 mcp=[json.loads(p.read_text()) for p in (r/'mcp_calls').glob('*.json')]
 for x in mcp:
  if x['name']=='submit_confirmed_return':
   submission_count+=1
   if set(x['arguments'])!={'operation_id'}:bad_schema.append(x)
  data=x.get('result',{}).get('data',{});err=data.get('error') if isinstance(data,dict) else None
  if err and err.get('code')=='IDEMPOTENCY_CONFLICT':conflicts.append(x)
 totals={'planned':16,'executed':sum(x['attempt'] is not None for x in rows),'scored':len(scored),'completed':passed,'completion_rate':passed/16,'statuses':dict(Counter(x['status'] for x in rows)),
  'rounds':{str(n):{'planned':8,'completed':sum(x['repeat']==n and x['status']=='pass' for x in rows),'statuses':dict(Counter(x['status'] for x in rows if x['repeat']==n))} for n in (1,2)},
  'actual_wrong_writes':sum(len(x['score']['actual_wrong_writes']) for x in scored),'wrong_tool_attempts':sum(len(x['score']['wrong_tool_attempts']) for x in scored),'backend_rejections':sum(len(x['score']['backend_rejections']) for x in scored),
  'submission_tool_attempts':submission_count,'model_submit_extra_parameters':len(bad_schema),'formal_idempotency_conflicts':len(conflicts),'canonical_receipts':sum(x['score']['canonical_receipts'] for x in scored),'response_type_counts':dict(sum((Counter(x['score']['response_type_counts']) for x in scored),Counter())),
  'text_factual_accuracy':'unreviewed; fixed receipts not model accuracy'}
 duplicates=[];snapshot_mismatches=[]
 for x in scored:
  d=r/'tasks'/x['attempt_id'];db=json.loads((d/'final_database.json').read_text());initial=json.loads((d/'initial_database.json').read_text());ops={op['id']:op for op in db['return_operations']};requests=[q for q in db['return_requests'] if q['id']!='11111111-1111-4111-8111-111111111111']
  for q in requests:
   if any(q[k]!=ops[q['operation_id']][k] for k in ('order_id','item_id','quantity','reason')):snapshot_mismatches.append({'task':x['attempt_id'],'request':q})
   n=sum(t['operation_id']==q['operation_id'] for t in requests);out=[o for o in db['return_outbox'] if o['operation_id']==q['operation_id']];receipts=[ev for ev in db['parlant_events'] if (ev['doc'].get('metadata') or {}).get('outbox_id') in {o['id'] for o in out}]
   if n!=1 or len(out)!=1 or len(receipts)!=1:duplicates.append({'task':x['attempt_id'],'requests':n,'outbox':len(out),'receipts':len(receipts)})
 totals.update(snapshot_parameter_mismatches=snapshot_mismatches,duplicate_findings=duplicates)
 model_usage={'unique_calls':len(calls),'unique_response_ids':len({x.get('response_id') for x in calls if x.get('response_id')}),'statuses':dict(Counter(x['status'] for x in calls)),'tokens':dict(usage),'known_guard_reference_usd':known,'unknown_usage':unknown,'budget_usd':2,'by_phase':perphase,'returned_models':dict(Counter(x.get('returned_model','unavailable') for x in calls)),'Flash_offpeak_known_reference_usd':flashcost,'Flash_reference_unknown_calls':flashunknown,'alias_billing_price':'unavailable','actual_bill':'unavailable','physical_HTTP_retries':'unavailable','mean_guard_known_usd_per_planned':known/16,'guard_known_usd_per_success':known/passed if passed else None}
 latency={'tasks':stats([x['attempt']['seconds'] for x in rows if x['attempt']]),'turns':stats([t['seconds'] for x in rows if x['attempt'] for t in x['attempt']['turns'] if t.get('seconds') is not None]),'MCP':stats([x['seconds'] for x in mcp]),'failures':[{'attempt':x['attempt_id'],'seconds':x['attempt']['seconds'],'status':x['status']} for x in rows if x['attempt'] and x['status']!='pass']}
 metric={'legal_return':totals,'usage':model_usage,'latency':latency,'offline':json.loads((r/'offline/report.json').read_text()),'offline_live_checks':json.loads((r/'offline_live_checks/report.json').read_text())}
 save(r/'metrics.json',metric);save(r/'task_results.json',rows);save(r/'usage_summary.json',{'aggregate':model_usage,'calls':calls});save(r/'conflicts.json',conflicts)
 lines=['# APP-S1：已确认参数快照提交','', '2026-10-07（Asia/Shanghai）；自定义固定身份、单实例应用有限回归。原 APP-EVAL-V1 与历史失败原样保留，不是同期A/B、新留出测试、官方benchmark或浏览器验收。','', '## 实现与授权边界','', '- 模型工具为 `submit_confirmed_return(operation_id)`；HTTP MCP 另接收服务端能力 `session_token`，在原生客户端的模型schema中隐藏/由可信ToolContext注入。operation_id不是授权；额外业务字段在客户端及真实MCP线上都拒绝。','- 后端锁住原会话/操作，读取不可变参数快照，核对实际持久用户确认；首次提交重验有效期、归属、资格、实时数量/金额。复用原请求、数量约束及同事务Outbox，已提交过期返回原申请。','- 旧全参数业务函数保留严格冲突检查及同一提交检查，不再注册MCP。确认入口仍只记授权；自然语言正式任务首次提交全部由Parlant调用HTTP MCP。','- 新增005迁移，001–004不改。未来同会话/同订单明细不同数量或原因的未提交草稿持久化为superseded；跨明细改同一申请明确使用replaces_operation_id。不依靠当前指针撤销独立申请，已提交A不撤销。旧已确认事件保留，但被替代操作不能提交。历史操作不自动回填替代关系/通知。','', '## 有限回归','',f"合法退货完成 **{passed}/16 = {passed/16:.2%}**；历史V1 **13/16**，差 {passed-13} 个。不能替换原40题整体82.5%，不能从16题推断泛化或因果收益。",'', '| attempt | V1 | APP-S1 | 用户消息 | 新申请 | 错误 |','| --- | --- | --- | ---: | ---: | --- |']
 for x in rows:lines.append(f"| {x['attempt_id']} | {x['V1_status']} | {x['status']} | {len(x['attempt']['turns']) if x['attempt'] else 'unavailable'} | {x['score']['new_requests'] if x['score'] else 'unavailable'} | {x['score']['errors'] if x['score'] else 'not scored'} |")
 lines+=['',f"两轮结果：{totals['rounds']}。正式提交工具调用{submission_count}，多余模型参数{len(bad_schema)}，IDEMPOTENCY_CONFLICT {len(conflicts)}。快照/申请参数差异{len(snapshot_mismatches)}，实际错误写入{totals['actual_wrong_writes']}，重复申请/Outbox/提交回执发现{len(duplicates)}。后端拒绝{totals['backend_rejections']}与错误工具尝试{totals['wrong_tool_attempts']}分开。",'', '原失败原因“尺寸不合适→尺寸不合”不做题目硬编码；接口移除再填写业务参数的机会。正常失败不重跑。原目标/夹具/可见脚本保持；8题顺序是原20题shuffle的合法子序列，seed42/43保持，不对8题重新洗牌。日期锚点仅调整为本轮时间使合成可退夹具仍处窗口，所有题同锚点。无ID题仍允许V1预设ID澄清。','', '评分调用原V1源文件（hash保留）及一份只读最小适配：新工具映射旧工具名、从已保存操作投影旧观测参数；原始MCP/事件完全保留。原B3金额正则和B2 needs_review都不改，本轮不运行它们。自由文本没有人工/LLM事实裁判，固定回执与query_response单列。','', '## 离线实际执行','',f"14项边界检查与4项授权/实时事实检查通过，真实PG/HTTP MCP/native Store，模型调用0。实际发送旧完整口令（未确认与已确认两种旧草稿），均409/superseded且无申请；新操作仍待确认。八路并发唯一提交，真实提交后MCP exit77响应丢失、重启及四路重试仍唯一；A/B定位、query_response、新工具trace回执和后续普通原生消息不被覆盖通过。严格实际处理屏障前后快照一致。",'', '离线序列化失败两次、测试夹具调用签名失败一次保留原目录/日志；仅测试脚本修正，无付费预检。离线固定消息/offer明确标fixture；不是自然语言对话通过。','', '## 调用与费用','',f"唯一SDK调用 {len(calls)}，响应ID {model_usage['unique_response_ids']}，usage未知 {len(unknown)}；输入{usage['prompt_tokens']}，输出{usage['completion_tokens']}，cache hit {usage['prompt_cache_hit_tokens']}，miss {usage['prompt_cache_miss_tokens']}。请求deepseek-chat，实际返回{model_usage['returned_models']}，未主动换模型；托管别名后台实现仍非客户可冻结。",'',f"执行前核对[官方价格页](https://api-docs.deepseek.com/quick_start/pricing)，保存页面/hash/时间/币种USD。最高峰Pro保守参考预算小计 **USD{known:.6f}** / 上限USD2；实际返回Flash且离峰时段参考小计 **USD{flashcost:.6f}**，未知参考{len(flashunknown)}。别名适用账单费率/实际账单 unavailable。沿用V1预留与计量，物理HTTP重试不可见，不伪造0。",'',f"全部16题保守已知平均 USD{known/16:.6f}，保守已知总额/成功数 {model_usage['guard_known_usd_per_success']}。启动/正式/异常由唯一调用ID归属原attempt，阶段：{perphase}；完整请求/响应/参数/时间保留。",'', '## 证据与复现','', '源代码最小diff：APP_S1_MINIMAL.diff；工具schema：offline/tool_schema.json；授权/替代/故障证据：offline/、offline_live_checks/；冻结：PAID_FREEZE.json、protocol_source_hashes.json、effective_metadata.json、dependencies/embedding文件hash；16题对照：task_results.json，每题原始turn/events/处理完成snapshot/MCP/initial/final数据库。','', '命令（在根目录；本轮已执行，已评分题续跑不会再次生成）：','', '```bash',f"export APP_EVAL_CONFIG=runtime-data/retail-demo/app_s1/{c['stamp']}/private.json",'apps/retail_demo/.venv/bin/python -m apps.retail_demo.eval_s1.runner run','apps/retail_demo/.venv/bin/python -m apps.retail_demo.eval_s1.report','```','', '新独立执行先 init_run→setup→scorer_checks→offline→offline_live_checks→runner preflight→freeze→runner run；不要对现有结果重复setup/离线重置。完整启动/停写升级见应用README/MIGRATION。','', '演示库没有应用005或重启原服务：原演示进程/worker运行状态保持。项目源码为APP-S1，现有演示正在运行旧加载版本，须停写备份→db init→重启后才切换；不能把测试验证称为原演示已升级。历史CLOSEOUT校验清单代表当时版本，APP-S1版本由本轮冻结hash标识。最终隔离、旧资产hash、停止测试/清除钩子、上传清单另见FINAL_ISOLATION.json与UPLOAD_SHA256.txt。']
 if (r/'STOP.json').exists():lines+=['','停止条件触发，未执行题仍保留：'+(r/'STOP.json').read_text()]
 report='\n'.join(lines)+'\n';(r/'REPORT.md').write_text(report);(a/'REPORT.md').write_text(report)
 print(json.dumps({'completed':passed,'planned':16,'statuses':totals['statuses'],'guard_usd':known,'Flash_reference_usd':flashcost,'calls':len(calls)},ensure_ascii=False))

if __name__=='__main__':main()
