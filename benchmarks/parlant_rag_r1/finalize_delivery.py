"""Readable comparison and final archive checks; no changes to retrieval/generation."""
from common_r1 import *
import collections,re,subprocess
from report import table

def main():
    comparisons=readl(RESULT/'R0_vs_R1_ten.jsonl');samples=readl(RESULT/'smoke_samples.jsonl');summaries={}
    for name in ['R0','R1']:
        usages=[r[f'{name}_usage'] for r in comparisons]
        totals={key:sum(u['known_token_totals'][key] for u in usages) for key in ['prompt_tokens','prompt_cache_hit_tokens','prompt_cache_miss_tokens','completion_tokens']}
        summaries[name]={'formal_targets':10,'schematic_calls':sum(u['schematic_calls'] for u in usages),'provider_create_calls':sum(u['provider_create_calls'] for u in usages),'physical_http_requests':sum(u['physical_http_requests'] for u in usages) if all(isinstance(u['physical_http_requests'],int) for u in usages) else 'unavailable','observed_provider_token_totals':totals,'observed_provider_cost_estimate_usd':sum(u['known_cost_estimate_usd'] for u in usages),'sum_sample_wall_seconds':sum(r[f'{name}_seconds'] for r in comparisons),'mean_sample_wall_seconds':sum(r[f'{name}_seconds'] for r in comparisons)/10}
    write(REVIEW/'TEN_USAGE_COMPARISON.json',summaries)
    count=sum(len(s['all_passage_ID_mentions_in_top5']) for s in samples)
    invalid=[{'task_id':s['task_id'],'citation':cid} for s in samples for cid,valid in s['all_passage_ID_mentions_in_top5'].items() if not valid]
    no_cites=[s['task_id'] for s in samples if not s['all_passage_ID_mentions_in_top5']]
    write(REVIEW/'CITATION_SUMMARY.json',{'method':'full and partial IBM Cloud ID mention exact-match against actual Top5; missing citations are not success','R1_ID_mentions':count,'R1_ID_mentions_outside_Top5':invalid,'R1_tasks_without_ID_citation':no_cites,'factual_accuracy':'unavailable'})
    citation_rows=[]
    for row,sample in zip(comparisons,samples):
        r0c=row['R0_citations_in_top5'];r1c=sample['all_passage_ID_mentions_in_top5']
        citation_rows.append([row['order'],f"{row['R0_retrieval']['nDCG@5']} / {row['R0_retrieval']['nDCG@10']}",f"{row['R1_retrieval']['nDCG@5']} / {row['R1_retrieval']['nDCG@10']}",f"{sum(r0c.values())}/{len(r0c)}" if r0c else '无 ID 引用',f"{sum(r1c.values())}/{len(r1c)}" if r1c else '无 ID 引用',f"{row['R0_seconds']:.2f} / {row['R1_seconds']:.2f}"])
    usage_rows=[[name,u['schematic_calls'],u['provider_create_calls'],u['physical_http_requests'],*[u['observed_provider_token_totals'][key] for key in ['prompt_tokens','prompt_cache_hit_tokens','prompt_cache_miss_tokens','completion_tokens']],f"{u['observed_provider_cost_estimate_usd']:.8f}",f"{u['mean_sample_wall_seconds']:.3f}"] for name,u in summaries.items()]
    ndcg_table=table(['题号','R0 nDCG@5 / @10','R1 nDCG@5 / @10','R0 ID引用在Top5','R1 ID引用在Top5','R0 / R1秒'],citation_rows)
    usage_table=table(['十题首次正式','schema','provider create','物理HTTP','输入','cache hit','cache miss','输出','已观察成本USD','均秒/题'],usage_rows)
    report=REVIEW/'REPORT.md';text=report.read_text().replace('\n### ','\n\n### ').replace('未自动进入混合检索以外的新候选或下一轮','未追加候选或开启下一轮')
    addition=f'''

## 交付核对补充

R1-B 在所有当前轮分组中都比 R0 提高 Recall@5 和 nDCG@5：第2–3轮 Recall@5 为 .2083→.3417，第4–6轮为 .1789→.2921，第7轮及以后为 .2847→.3875。因此提升也出现在多轮任务中；本轮获选方案仍只使用当前问题。拼接上一轮问答的 A/C/D 在这些多轮组中均未超过 R0，不能宣称这次 history-aware query 带来了收益。217个去重 query 中，没有当前问题或历史 query 达到编码截断长度；仅2个 passage 的编码输入截断。没有为此调参。

72442 个向量一次编码，4528批，重复行0，完整批次 hash／L2norm 检查通过；CPU编码实际耗时16857.742秒（4.683小时），之后 query 编码、B/C排名与RRF评测共38.783秒。R0/A BM25阶段完整耗时 unavailable；未将未记录时间补为0。

### 原十题 nDCG、引用与耗时

{ndcg_table}

ID比例只核对实际 Top5；无引用记“无 ID 引用”，并非成功。R1 共 {count} 个提取到的完整／不完整 ID mention，Top5外 {len(invalid)} 个，无 ID 引用 {len(no_cites)} 题；URL严格匹配另见 samples 中 source_URL_mentions_in_top5，不能代替事实判断。R0保留原冻结的完整ID抽取口径。第10题无 qrels，检索指标保持 unavailable。

### 首次正式十题用量对照

{usage_table}

表内成本仅按已返回 provider usage 和执行时单价估算。R0物理HTTP/retry次数 unavailable，不能把它当0；R1 40个物理请求均为200、40个响应usage完整，未观察到重试。R0原启动9次、首题额外审计恢复4次仍保留在原USAGE.json，未计入这张首次正式十题对照表。R1复用冻结metadata而不重评规则，启动调用0、异常调用0、目标回答10、每题生成message数1。两轮都是4次内部调用/目标，缓存命中差异和provider延迟具有时序因素，成本差异不能归因为单一检索技术。发票/余额 unavailable，无收费裁判，F1/ROUGE、ID引用有效及证据覆盖都不是回答准确率。
'''
    assert '## 交付核对补充' not in text
    report.write_text(text+addition)
    commands=REVIEW/'REPRODUCE_COMMANDS.md'
    with commands.open('a') as f:f.write('\n交付明细补充由 `finalize_delivery.py` 在 `report.py` 后、`package.py` 前生成；只汇总已有冻结结果，零模型调用。\n')
    freeze=json.loads((REVIEW/'GENERATION_FROZEN.json').read_text())
    for path,value in freeze['files'].items():assert sha(PROJECT/path)==value,path
    original=(REVIEW/'REPRODUCE_BEFORE.md').read_text();assert (PROJECT/'REPRODUCE.md').read_text().startswith(original)
    observations=[r for p in (RESULT/'attempts').glob('*_attempt*/observations.jsonl') for r in readl(p)]
    assert sum(r['kind']=='physical_http_started' for r in observations)==40
    assert sum(r['kind']=='physical_http_finished' and r['status']==200 for r in observations)==40
    assert sum(r['kind']=='history_loaded' and r['model_calls_during_load']==0 for r in observations)==10
    assert sum(r['kind']=='session_cleanup' and r['events_removed'] for r in observations)==10
    status=json.loads((RESULT/'smoke_status.json').read_text());assert len(status['outcomes'])==10 and all(s['generated_message_count']==1 and s['target_trigger_count']==1 and s['evidence_coverage_verified'] for s in status['outcomes']) and status['service_stopped']
    write(REVIEW/'DELIVERY_AUDIT.json',{'generation_frozen_files_all_still_match':True,'added_reporting_only_script':str(Path(__file__).relative_to(PROJECT)),'prior_archive_sha256':(REVIEW/'UPLOAD_SHA256.txt').read_text().split()[0],'exactly_ten_messages_and_triggers':True,'physical_HTTP_40_all_200':True,'all_ten_history_load_no_inference':True,'all_ten_cleanup_verified':True,'REPRODUCE_append_only':True,'new_model_calls_for_delivery':0,'service_stopped':True})
    write(REVIEW/'CODE_HASHES.json',{str(p.relative_to(PROJECT)):sha(p) for p in sorted(BASE.glob('*')) if p.is_file() and p.suffix in ('.py','.json')})
    print('DELIVERY AUDIT PASSED',summaries,flush=True)
if __name__=='__main__':main()
