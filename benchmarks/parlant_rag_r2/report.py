from common_r2 import *
from metrics_utils import read_obs,usage_group
import collections,subprocess,socket
ARMS=['CONTROL','R2-EVIDENCE']
def table(headers,rows):return '| '+' | '.join(headers)+' |\n|'+ '|'.join(['---']*len(headers))+'|\n'+'\n'.join('| '+' | '.join(map(str,r))+' |' for r in rows)+'\n'
def f(x):return f'{x:.6f}' if isinstance(x,float) else str(x)
def main():
    state=json.loads((RESULT/'smoke_status.json').read_text());assert state['service_stopped'] and len(state['outcomes'])==22 and not state['stop_reasons']
    before=json.loads((REVIEW/'PRESERVATION_BEFORE.json').read_text());assert all(sha(PROJECT/p)==h for p,h in before.items())
    deps=subprocess.check_output([str(PROJECT/'.venv/bin/python'),'-m','pip','freeze'],text=True);assert deps==(REVIEW/'DEPENDENCIES_BEFORE.txt').read_text()
    for manifest in ['ASSETS_FROZEN.json','GENERATION_FROZEN.json']:
        for p,h in json.loads((REVIEW/manifest).read_text())['files'].items():assert sha(PROJECT/p)==h,p
    pairs=readl(RESULT/'CONTROL_vs_R2_answers.jsonl');details=readl(RESULT/'answer_details.jsonl');obs=[r for r in read_obs() if r['attempt_id']!='offline-checks']
    assert all(d['status']=='completed' and d['evidence_in_final_prompt'] and d['independent_original_prompt_reparse'] for d in details)
    assert all(p['CONTROL']['actual_top5']==p['R2-EVIDENCE']['actual_top5'] for p in pairs)
    assert all(p['CONTROL']['control_original_native_prompt_unchanged'] for p in pairs)
    assert sum(r['kind']=='session_cleanup' and r['events_removed'] for r in obs)==22
    assert sum(r['kind']=='history_loaded' and r['model_calls_during_load']==0 and r['processing_task_absent'] for r in obs)==22
    assert sum(r['kind']=='generation_evidence_format' and r['candidate']=='R2-EVIDENCE' and r['change']['all_other_section_objects_unchanged'] for r in obs)==11
    # All schematic-call prompt artifacts must be exact nonempty originals.
    calls=[r for r in obs if r['kind']=='model_call_started']
    for r in calls:
        path=RESULT/'attempts'/r['attempt_id']/'prompts'/f"{r['call_id']}.txt"
        assert path.stat().st_size and sha(path)==r['prompt_sha256']
    for port in [18903,18921]:
        with socket.socket() as s:s.settimeout(.3);assert s.connect_ex(('127.0.0.1',port))!=0
    write(REVIEW/'FINAL_VALIDATION.json',{'completed_attempts':22,'extra_attempts':0,'session_cleanup_verified':22,'history_model_calls':0,'original_final_prompts_reparsed':22,'all_schematic_original_prompts_nonempty_hash_verified':len(calls),'CONTROL_native_prompt_byte_identical':11,'R2_only_STAGED_EVENTS_changed':11,'original_top5_identical':11,'old_files_preserved':len(before),'old_files_modified':0,'dependencies_unchanged':True,'all_owned_ports_closed':True,'service_stopped':True,'retrieval_runs':0,'no_judge_or_extra_generation':True})
    stats=json.loads((REVIEW/'GENERATION_METRICS.json').read_text());usage=json.loads((REVIEW/'USAGE.json').read_text());a=stats['CONTROL']['macro'];b=stats['R2-EVIDENCE']['macro'];u=usage['formal']
    auto=table(['指标','CONTROL','R2-EVIDENCE','R2−CONTROL'],[[k,f(a[k]),f(b[k]),f(b[k]-a[k])] for k in a])
    per=[]
    for i,p in enumerate(pairs,1):
        x=p['CONTROL'];y=p['R2-EVIDENCE'];per.append([i,p['task_id'],f(x['lexical_f1'])+' → '+f(y['lexical_f1']),f(x['rouge_l_f1'])+' → '+f(y['rouge_l_f1']),str(x['answer_length_words'])+' → '+str(y['answer_length_words']),str(x['citation_mentions'])+' → '+str(y['citation_mentions'])])
    pertable=table(['序号','task_id','F1','ROUGE-L','words','citation mentions'],per)
    usage_rows=[]
    for name,x in [('startup',usage['startup']),('CONTROL',u['CONTROL']),('R2-EVIDENCE',u['R2-EVIDENCE']),('abnormal',usage['abnormal']),('total',usage['total'])]:
        t=x['token_totals'];usage_rows.append([name,x['schematic_calls'],x['physical_http_requests'],t['prompt_tokens'],t['prompt_cache_hit_tokens'],t['prompt_cache_miss_tokens'],t['completion_tokens'],x['cost_estimate_usd']])
    ut=table(['归属','schema','HTTP','input','cache hit','cache miss','output','估算USD'],usage_rows)
    schema_usage={arm:{} for arm in ARMS};price=json.loads((BASE/'pricing.json').read_text());prompt_comparisons=[]
    for arm in ARMS:
        rows=[r for r in obs if arm in r['attempt_id']]
        for schema in sorted({r['schema'] for r in rows if r['kind']=='model_call_started'}):
            ids={r['call_id'] for r in rows if r['kind']=='model_call_started' and r['schema']==schema};schema_usage[arm][schema]=usage_group([r for r in rows if r.get('call_id') in ids],price)
    for p in pairs:
        x=[r for r in obs if r['task_id']==p['task_id'] and r['attempt_id']==p['CONTROL']['attempt_id'] and r['kind']=='model_call_started' and r['schema']!='CannedResponseDraftSchema']
        y=[r for r in obs if r['task_id']==p['task_id'] and r['attempt_id']==p['R2-EVIDENCE']['attempt_id'] and r['kind']=='model_call_started' and r['schema']!='CannedResponseDraftSchema']
        prompt_comparisons.append({'task_id':p['task_id'],'non_generation_schema_prompt_hash_multiset_identical':collections.Counter((r['schema'],r['prompt_sha256']) for r in x)==collections.Counter((r['schema'],r['prompt_sha256']) for r in y)})
    write(REVIEW/'SCHEMA_USAGE.json',schema_usage);write(REVIEW/'NON_GENERATION_PROMPT_COMPARISON.json',prompt_comparisons)
    input_pct=100*(u['R2-EVIDENCE']['token_totals']['prompt_tokens']/u['CONTROL']['token_totals']['prompt_tokens']-1);output_pct=100*(u['R2-EVIDENCE']['token_totals']['completion_tokens']/u['CONTROL']['token_totals']['completion_tokens']-1)
    hashes=json.loads((REVIEW/'ASSETS_FROZEN.json').read_text())['files'];keyfiles=['benchmarks/parlant_rag/data/split.json','benchmarks/parlant_rag/data/cloud_passages.jsonl','benchmarks/parlant_rag_r1/model_lock.json','benchmarks/parlant_rag_r1/index/corpus.npy','results/mtrag_cloud_r1_holdout_20261004_130837/selected_retrieval.jsonl','results/mtrag_cloud_r1_holdout_20261004_130837/holdout_query_overlay.npy']
    ht=table(['冻结资产','SHA-256'],[[k,hashes[k]] for k in keyfiles])
    retrieval=json.loads((RESULT/'retrieval_metrics_reused.json').read_text())['R1-B']
    text=f'''# RAG R2：Evidence-aware Generation 小实验

完成 **CONTROL 11 + R2-EVIDENCE 11 = 22 attempts/22回答**，额外attempt/恢复0。旧R1-B回答只读保留；用户明确确认新跑同期CONTROL，未拿旧回答冒充本轮CONTROL。结果不显示整体生成收益：lexical F1 **{a['lexical_f1']:.6f} → {b['lexical_f1']:.6f}**、ROUGE-L **{a['rouge_l_f1']:.6f} → {b['rouge_l_f1']:.6f}**，均小幅下降；平均回答词数 **{a['answer_length_words']:.2f} → {b['answer_length_words']:.2f}**。这些都是辅助指标，不能称事实准确率或统计显著性。完成后停止，不进入新优化循环。

## 冻结资产与题单

沿用 BAAI/bge-base-en-v1.5 revision a5beb1e3e68b9ab74eb54cfd186867f64f240e1a、原完整72442个Cloud passage/index/原query cache及HOLDOUT overlay，**检索与编码0次**，直接使用冻结R1-B原Top5 payload（含全文、完整偏移ID、URL、原score），顺序/内容/数量逐题双组相同；Top10仅保留原记录。原Cloud split的11conversation/原11目标题task_id/原序号/原speaker-text历史及当前问题不变。沿原题序奇数CONTROL先、偶数R2先，各组新建独立session，无模拟用户、历史重生成、跨样本答案串接。

{ht}

原检索指标只复制未重算：全83个有qrels的HOLDOUT Recall@5/10={retrieval['Recall@5']:.6f}/{retrieval['Recall@10']:.6f}，nDCG@5/10={retrieval['nDCG@5']:.6f}/{retrieval['nDCG@10']:.6f}，Recall@10=0 {retrieval['Recall@10_zero_tasks']}；6轮无标注unavailable。这是历史冻结检索结果背景，不是R2新增检索成绩；R2只生成原11题，不运行全89题或完整MTRAG test。

## 唯一生成干预

独立代码 benchmarks/parlant_rag_r2，原src/旧benchmark未改。在 CANNED_FLUID 的 CannedResponseGenerator._build_draft_prompt **返回原生PromptBuilder后**，CONTROL保持build文本byte-identical；R2仅替换 BuiltInSection.STAGED_EVENTS，用可读的Evidence1..5包装取代Python/JSON tool-event表现。原Retriever事件仍不变，其他所有prompt section对象、section序、原生interaction history、guideline instructions、schema/shots/agent metadata保持不变；实际逐次保存native-before和final-after以及unified diff。不调用DeepSeek另写回答器、不改返回回答、不加事实纠错层。

包装内容如下，全部由官方既有history/current question和实际Top5确定，不从标注推导：

```text
User conversation history:
[原speaker/text历史JSON]
Current question:
[原当前问题]
Retrieved evidence:
[Evidence 1]
Source ID: [原完整passage ID]
Title: [原title]
Source URL: [原URL]
Content:
[完整原text]
[/Evidence 1]
... 原顺序共5段 ...
Generation rules:
1. Answer only based on retrieved evidence.
2. If evidence is insufficient, explicitly state uncertainty.
3. Do not invent details absent from evidence.
4. Prefer concise answer and mention source when useful.
```

这四条是用户指定的**生成prompt提醒**，未新增/重评价Agent的三条RAG规则，但会改变生成指令显著程度，因此不能把实验称为纯排版效应。为满足指定结构且保留其余原生prompt，wrapper重复列出历史/问题；内容未修改，原生history section仍在。未压缩、重排片段或变动query。标题在原Cloud资产中常为空，原样保留，不推导新标题。

保持 Parlant v3.3.2、官方DeepSeek deepseek-chat、原CANNED_FLUID/BasicOptimizationPolicy、原三规则/内部ID/时间戳/metadata和生成参数，单次startup复用冻结effective_configuration，无新evaluation或启动模型调用。逐schema actual params/hints与response model都保留；本轮实际response均deepseek-flash，未因价格页改变请求名称。不用Agent Plan、reranker、query rewrite、Hybrid或新检索优化。

## 指标与证据审计

{auto}

词面指标复用原R0 helper：casefold/Unicode word/去标点冠词/max reference，ROUGE-L为token LCS harmonic F1；length words使用未去冠词的Unicode word count，chars用Python字符数。citation_mentions统计完整 ibmcld_ID 的出现次数（重复计入），unique_citation_ids单列；source_url_mentions为URL出现次数，非语义判断。两组各9/11回答有引用，引用ID均来自实际Top5；无引用样本有效性检查vacuous，未把它计为来源正确/事实正确率。

{pertable}

Top5全文/ID实际进入生成prompt **22/22**，原文非空独立重解码22/22，全部88个schematic prompt文件非空且匹配发送前hash；11个R2的native→final diff仅变化证据section，11个CONTROL原native文本完全一致。所有prompt同步写入、fsync文件及目录；未复用旧9份空prompt，不补造旧缺件。模型真正读取/逐项正确使用证据不由“在prompt里”证明。

用户要求的“无证据新增事实（人工抽样标记）”单独做固定Random42四题×两组=8份抽样表，含完整Top5/回答，见 HUMAN_REVIEW_SHEET.md / human_annotation_sample.jsonl。**真实人工标记未提供，当前unreviewed，不把编码助手的定性分析冒充人工标签**，相关指标unavailable，不伪造0。样本标记yes/no/uncertain须附claim原文和证据理由；reference、qrels/answerability没有进入runtime/wrapper，参考答案和原qrels-derived IDs仅在服务停止后的离线分析读入。

## 差异案例 A/B/C/D

详见 FAILURE_ANALYSIS.md / failure_analysis.jsonl，来自当前编码助手的实际文本检查，没有调用LLM judge，不是人工事实评分：

- **A 检索有证据、生成失败**：第7题bots，gold Recall@5=1，CONTROL直接确认窃取私有数据，R2先否认文档能给yes/no，随后引用偷credentials/data的明确证据，出现过度保留。另第4题即使gold Recall=0，Top5的OpenWhisk sitemap明确有Carthage安装链接；CONTROL识别该入口，R2却否認有该方法证据，说明qrels缺失不等于语义证据全缺。
- **B 检索有证据、局部生成改善**：第11题annotators，R2在至少两人用于agreement条件之外，明确“文档没有指定固定总人数”；比CONTROL范围限定更清楚，参考词面分数上升，但CONTROL原核心条件也受支持，不称事实正确率提高。
- **C 检索目标证据缺失、prompt无法补足**：第1题image tag，当前问题检索偏tag其他语义，两组仍澄清，R2未产生正确X/Y/Z证据。
- **D 证据不足仍作具体断言**：第5题Cloudant Lite，实际Top5通用Lite/Discovery/Object Storage文档不含Cloudant 1GB/吞吐配置，两组仍复用历史相关事实。R2说不确定后仍给1GB等断言；这是retrieved-evidence grounding风险，不自动等同世界事实错误，人工标签仍unreviewed。

未按案例改prompt、换题、调参或再次生成。此11题此前已被观察，本实验在同一HOLDOUT小题单上开发生成干预，**不再作为从未见过的新泛化测试**。无新的独立留出评估，不宣称未见数据上的增益。

## 调用、token、成本

{ut}

两组各44次schema/provider create/physicalHTTP（3次普通guideline matcher+1次draft，每题4），总88次全部usage完整；startup/abnormal各0，缺usage时本实现仍保持unavailable规则。本轮总估算USD {usage['total']['cost_estimate_usd']:.9f}，非账户账单，balance/invoice unavailable。按执行前核对的[DeepSeek官方定价](https://api-docs.deepseek.com/quick_start/pricing/)只对实际deepseek-flash按周末off-peak百万tokenUSD hit .003 / miss .15 / output .6套价；未知响应名/usage则unavailable。

总input token变化 **{input_pct:+.4f}%**，output变化 **{output_pct:+.2f}%**，估算成本基本相同。本次并没有“token明显增加”，证据JSON转明文的节省抵消重复历史的部分开销；仍未见整体自动指标收益，平均回答更长、引用出现次数更多不等于事实改善。各schema token/cache/cost见SCHEMA_USAGE.json；非generation matcher prompt hash双组比较见NON_GENERATION_PROMPT_COMPARISON.json，模型匹配输出/采样、外部服务、缓存/先后顺序可能带来噪声，不把所有回答差异都归因格式。

## 交付与停止

原R0/R1/HOLDOUT代码/data/model/index/query cache/results/reviews及core源码 {len(before)} 既有文件hash不变，依赖不变；REPRODUCE.md仅追加。全部22个session和events删除验证，history装载无模型调用，最终agent/rule元数据不变；service停止、18903/18921关闭。无commit/push，无完整MTRAG test、LLM judge、新增retrieval优化、reranker/rewrite/Hybrid、response rewrite、事实纠错或下一轮。

结果 `{RESULT.relative_to(PROJECT)}`；审计 `{REVIEW.relative_to(PROJECT)}`；原Top5/题单/config diff/prompt差异与覆盖/逐题回答/usage/失败案例/hash/复现命令集中于此。只读验证命令见REPRODUCE_COMMANDS.md，现有结果禁止再次付费执行。脱敏上传包UPLOAD_SANITIZED.tar.gz及SHA-256在同目录，不含.env/key、weights、全corpus/index或旧DEV结果。实验没有显示采用R2的充分收益，保持冻结R1-B，停止不调参。
'''
    durable_text(REVIEW/'REPORT.md',text)
    print('REPORT AND INTEGRITY VERIFIED',len(before),'preserved files')
if __name__=='__main__':main()
