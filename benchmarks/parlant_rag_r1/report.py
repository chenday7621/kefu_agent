"""DEV comparisons and final audit, no model calls."""
from common_r1 import *
from metrics_utils import f1,rouge,usage_group
import re,csv,subprocess,datetime

def table(header,rows):
    return '| '+' | '.join(header)+' |\n|'+'|'.join(['---']*len(header))+'|\n'+'\n'.join('| '+' | '.join(str(x) for x in row)+' |' for row in rows)

def main():
    metrics=json.loads((RESULT/'offline_metrics.json').read_text());selection=json.loads((REVIEW/'SELECTION.json').read_text());source=json.loads((REVIEW/'CORPUS_SOURCE_AUDIT.json').read_text());grouped=json.loads((RESULT/'grouped_metrics.json').read_text())
    status=json.loads((RESULT/'smoke_status.json').read_text()) if (RESULT/'smoke_status.json').exists() else {'planned':10,'outcomes':[],'service_stopped':True,'reason':'offline gate not passed; no paid generation'}
    old={s['task_id']:s for s in readl(R0_RESULT/'smoke_samples.jsonl')};targets=readl(RESULT/'selected_ten_retrieval.jsonl');rankings={name:{r['task_id']:r for r in readl(RESULT/f'{name}_per_task.jsonl')} for name in ['R0',selection['selected']]}
    successes={s['task_id']:s for s in status['outcomes'] if s['status'].startswith('completed')};formal_attempts={s['attempt_id'] for s in successes.values()}
    observations=[r for p in sorted(RESULT.rglob('observations.jsonl')) for r in readl(p) if r['task_id']!='offline'];price=json.loads((BASE/'pricing.json').read_text())
    usage={name:usage_group(rows,price) for name,rows in {'startup':[r for r in observations if r['task_id']=='startup'],'formal':[r for r in observations if r['attempt_id'] in formal_attempts],'abnormal':[r for r in observations if r['task_id']!='startup' and r['attempt_id'] not in formal_attempts],'total':observations}.items()}
    write(REVIEW/'USAGE.json',usage)
    samples=[];comparisons=[]
    for i,target in enumerate(targets,1):
        tid=target['task']['task_id'];r0sample=old[tid];new=successes.get(tid);attempt=new['attempt_id'] if new else None
        answer=new['answer'] if new else 'unavailable';refs=r0sample['reference_answers'];actual=new.get('actual_evidence',[]) if new else []
        citations=sorted(set(re.findall(r'ibmcld_\d+-\d+-\d+',answer))) if new else []
        sample={'task_id':tid,'conversation_id':target['task']['conversation_id'],'runtime_input':target['task'],'query':target['query'],'selected_candidate':selection['selected'],'top10':target['top10'],'actual_evidence':actual,'answer':answer,'reference_answers':refs,'reference_evidence_ids':r0sample['reference_evidence_ids'],'citations':citations,'citation_ids_in_actual_retrieval':{cid:cid in {p['document_id'] for p in actual} for cid in citations},'retrieval_metrics':rankings[selection['selected']][tid]['metrics'],'lexical_f1':max(f1(answer,ref) for ref in refs) if new else 'unavailable','rouge_l_f1':max(rouge(answer,ref) for ref in refs) if new else 'unavailable','evidence_coverage_verified':new.get('evidence_coverage_verified','unavailable') if new else 'unavailable','status':new['status'] if new else 'unavailable','attempt_id':attempt,'seconds':new['seconds'] if new else 'unavailable','factual_correctness':'unavailable','usage':usage_group([r for r in observations if r['attempt_id']==attempt],price) if new else 'unavailable'}
        samples.append(sample)
        mentions=sorted(set(re.findall(r'ibmcld_\d+(?:-\d+)*',answer))) if new else []
        urls=sorted(set(url.rstrip('.,;') for url in re.findall(r'https?://[^\s\]\)<>]+',answer))) if new else []
        sample['all_passage_ID_mentions_in_top5']={cid:cid in {p['document_id'] for p in actual} for cid in mentions}
        sample['source_URL_mentions_in_top5']={url:url in {p['url'] for p in actual} for url in urls}
        sample['citation_audit_method']='all full and partial IBM Cloud ID mentions exact-match against actual Top5; URL exact match separately; absent citations not success; no fact judgment'
        write(RESULT/f'samples/{i:02d}.json',sample)
        r0attempt=r0sample['attempts'][0];r0rows=readl(R0_RESULT/'attempts'/r0attempt/'observations.jsonl')
        comparisons.append({'order':i,'task_id':tid,'R0_first_formal_attempt':r0attempt,'same_input':r0sample['runtime_input']==target['task'],'R0_answer':r0sample['answer'],'R1_answer':answer,'R0_retrieval':rankings['R0'][tid]['metrics'],'R1_retrieval':sample['retrieval_metrics'],'R0_lexical_f1':r0sample['lexical_f1'],'R1_lexical_f1':sample['lexical_f1'],'R0_rouge_l_f1':r0sample['rouge_l_f1'],'R1_rouge_l_f1':sample['rouge_l_f1'],'R0_citations_in_top5':r0sample['citation_ids_in_actual_retrieval'],'R1_citations_in_top5':sample['citation_ids_in_actual_retrieval'],'R0_usage':usage_group(r0rows,price),'R1_usage':sample['usage'],'R0_seconds':r0sample['seconds'],'R1_seconds':sample['seconds']})
    assert all(row['same_input'] for row in comparisons)
    jsonl(RESULT/'smoke_samples.jsonl',samples);jsonl(RESULT/'R0_vs_R1_ten.jsonl',comparisons)
    summary={}
    for name,ss in [('R0',[old[t['task']['task_id']] for t in targets]),('R1',[s for s in samples if s['answer']!='unavailable'])]:
        summary[name]={'completed':len(ss),**{key:sum(s[key] for s in ss)/len(ss) if ss else 'unavailable' for key in ['lexical_f1','rouge_l_f1']},'factual_accuracy':'unavailable'}
    write(REVIEW/'AUXILIARY_COMPARISON.json',summary)
    preservation=json.loads((REVIEW/'PRESERVATION_BEFORE.json').read_text());changed=[p for p,h in preservation.items() if not (PROJECT/p).exists() or sha(PROJECT/p)!=h];assert not changed,changed
    current={str(p.relative_to(PROJECT)) for root in [R0,R0_REVIEW,R0_RESULT,PROJECT/'src'] for p in root.rglob('*') if p.is_file()}
    added=sorted(current-set(preservation));assert not added,added
    write(REVIEW/'CODE_HASHES.json',{str(p.relative_to(PROJECT)):sha(p) for p in sorted(BASE.glob('*')) if p.is_file() and p.suffix in ('.py','.json')})
    deps=subprocess.check_output(['uv','pip','freeze','--python',str(PROJECT/'.venv/bin/python')],text=True);assert deps==(REVIEW/'DEPENDENCIES_BEFORE.txt').read_text()
    write(REVIEW/'FINAL_VALIDATION.json',{'R0_and_src_files_unchanged':len(preservation),'changed_files':changed,'added_R0_or_src_files':added,'dependencies_unchanged':True,'same_original_ten_order_and_inputs':True,'completed_R1_targets':len(successes),'new_answers':len(successes),'attempts':len(status['outcomes']),'evidence_verified':sum(s['evidence_coverage_verified'] is True for s in samples),'service_stopped':status['service_stopped'],'HOLDOUT_run':False,'paid_rewrite':False,'LLM_judge':False,'no_commit_push':True})
    metric_table=table(['候选','Recall@5','nDCG@5','Recall@10','nDCG@10','R@10=0'],[[name,*[f'{metrics[name][k]:.6f}' for k in ['Recall@5','nDCG@5','Recall@10','nDCG@10']],metrics[name]['Recall@10_zero_tasks']] for name in PROTOCOL['candidates']])
    groups=[]
    for key in PROTOCOL['grouping']:
        rows=[]
        for label in PROTOCOL['grouping'][key]:
            if label in grouped['R0'][key]:
                baseline=grouped['R0'][key][label];rows.append([label,baseline['annotated_tasks'],*[f"{grouped[name][key][label]['Recall@5']:.4f} / {grouped[name][key][label]['nDCG@5']:.4f}" for name in PROTOCOL['candidates']]])
        groups.append(f'### {key}\n\n'+table(['组','有标注轮数',*PROTOCOL['candidates']],rows)+'\n\n各候选单元格为 Recall@5 / nDCG@5；@10 和零召回任务数详见 grouped_metrics.json。')
    sample_table=table(['序号','task_id','R0 R@5','R1 R@5','R0 F1','R1 F1','R1证据覆盖'],[[row['order'],row['task_id'],row['R0_retrieval']['Recall@5'],row['R1_retrieval']['Recall@5'],round(row['R0_lexical_f1'],4),round(row['R1_lexical_f1'],4) if isinstance(row['R1_lexical_f1'],float) else row['R1_lexical_f1'],samples[i]['evidence_coverage_verified']] for i,row in enumerate(comparisons)])
    usage_table=table(['归属','schema','provider create','物理HTTP','输入','cache hit','cache miss','输出','估算USD'],[[name,u['schematic_calls'],u['provider_create_calls'],u['physical_http_requests'],*[u['token_totals'][k] for k in ['prompt_tokens','prompt_cache_hit_tokens','prompt_cache_miss_tokens','completion_tokens']],u['cost_estimate_usd']] for name,u in usage.items()])
    card='https://huggingface.co/BAAI/bge-base-en-v1.5';upstream=f'https://github.com/IBM/mt-rag-benchmark/blob/{source["upstream_commit"]}/corpora'
    generation_note='十题正式生成路径已按门槛进入；完成和异常数量以 FINAL_VALIDATION 与各题 status 为准。' if selection['gate_passed'] else '门槛未通过，R1 十题回答全部 unavailable，未启动正式生成会话。以下路径仅完成离线准备／验证，并未进行真实模型回答。'
    report=f'''# RAG R1 Cloud 候选检索消融与原十题验证

本轮是 MTRAG Cloud 自定义 DEV 检索优化与固定十题冒烟，不是官方 test 或完整 MTRAG 成绩。保持 R0 数据、划分、代码、首次正式回答和生成配置。未运行 HOLDOUT、收费 query rewrite、LLM 裁判、reranker 或下一轮调参。

## 语料来源核查

官方 commit `{source['upstream_commit']}`，路径 `corpora/passage_level/cloud.jsonl.zip`；本地 ZIP SHA-256 `{source['zip_sha256']}`，普通 Git blob SHA-1 `{source['local_git_blob_sha1']}` 与锁定 commit 的 tree 完全一致。ZIP CRC 正常，解压后逐记录等于 R0 cloud_passages.jsonl，实际 72442 个唯一 full passage ID、8578 个 parent ID，均为 `ibmcld_数字-start-end` 和 IBM Cloud URL。494 qrels 行、188 query 的完整偏移 ID 均映射；没有剥离偏移来凑匹配。此 passage_level ZIP 不匹配 `.gitattributes` 的四个 legacy LFS 路径，且不是 LFS 指针。

[官方资产]({upstream}/passage_level/cloud.jsonl.zip) 与 [corpora README]({upstream}/README.md) 的 Cloud 61022 passages / 57638 documents 统计不一致；实际资产和 R0 已观察的 Cloud conversation collection metadata 都是 72442。README Government 的 8578 documents / 72422 passages 数字接近，不能由此断言本资产是 Government；Cloud 前缀、URL、官方 blob 及所有 qrels 的证据支持它确为指定 Cloud 资产。上游具体编辑或版本原因 unavailable；记录为文档／发布资产统计不一致，没有重下载或改变 R0。仓库 Apache-2.0，BGE MIT。

## 预先固定协议

PROTOCOL_FROZEN.json 在候选计算之前保存。仅 runtime_dev.jsonl 的 speaker/text 生成 query，无 targets、enrichments、rewrite、参考答案、反馈、现成 contexts 或 answerability 输入。R0 当前问题 BM25；A 为上一轮 user、上一轮 agent、当前 user 用换行拼接，无角色标签；首轮退化当前问题。原 BM25 tokenizer/k1=1.2/b=.75/index/完整语料/score-desc-ID-asc 不变。

B 为当前问题 BGE，C 为与 A 字符串完全相同的历史 query；[官方 BGE model card]({card}) 的 query instruction 为 `{PROTOCOL['query_instruction']}`，passage 无 instruction。模型 revision `{json.loads((BASE/'model_lock.json').read_text())['revision']}`，已有 Transformers / PyTorch CPU，float32 CLS、L2 normalize、normalized dot-product 作为 cosine；排序 score 降序、full ID 升序。整库编码一次并落 mmap/cache，B/C/D 共用；title + space + 原文与 BM25 字段相同。模型长度512，仅2段编码时截断，原语料和送入 Parlant 的完整文本不变；不额外分块。D 为 A/C 各 Top100，固定 equal-weight RRF k=60，不使用 qrels 调权重。模型、index、query cache hash 在 MODEL_HASHES/INDEX_HASHES/QUERY_CACHE_HASHES。

R0 的 116 DEV 中 105 有 qrels 用同一 binary-recall / linear-gain ndcg_cut 数学定义求 macro，11 无 qrels 保持 unavailable 并排除。沿用官方定义的本地实现，不声称执行了官方 pytrec_eval CLI。完整 Top100 与每轮失败见 `{PATHS['result']}/R*_per_task.jsonl` 和 failures.jsonl。R0 四个指标复现一致。

## 全 DEV 检索结果与选择

{metric_table}

候选提前排序：Recall@5 主指标，并列看 nDCG@5、Recall@10，最后候选名。唯一选择 **{selection['selected']}**；Recall@5 绝对变化 **{selection['absolute_Recall5_gain']:.6f}**，nDCG@5 变化 **{selection['nDCG5_change']:.6f}**。门槛为 Recall@5 增益至少 .05 且 nDCG@5 不下降，判定 **{'通过，进入原十题回答' if selection['gate_passed'] else '未通过，停止且不付费生成'}**。无后续调参。未读取官方 rewrite 内容或用官方结果挑参数。

## 按轮位置与历史长度分组

{chr(10).join(groups)}

以上为固定分组描述性结果，样本少且未做显著性检验；从同组 R0 与 R1 比较多轮收益，不把总体改善自动解释为每轮改善。

## 原固定十题 R0 对照 R1

{generation_note}

{sample_table}

原 task_id、顺序、speaker/text 输入逐条相同；R0 使用冻结的第一份正式回答，包含首题 attempt1，不采用额外 audit recovery 答案，R0 不重新生成。每题 Top10、实际 native Retriever Top5、query、完整返回文本／ID、回复、参考答案、引用、单题指标和原始 events/prompts/用量见 samples、smoke_samples.jsonl、R0_vs_R1_ten.jsonl、attempts。

保持 v3.3.2、官方适配器请求 deepseek-chat、原三条 RAG 规则、CANNED_FLUID、原 BasicOptimizationPolicy 和生成参数。独立 `{PATHS['runtime']}`，逐字段还原 R0 已评价的 Agent/Guideline ID、时间戳、metadata、priority 等，没有再次启动规则评价。Retriever 保持原内部 tool ID 以固定 prompt 结构，仅替换 payload。runtime 根据实际 interaction.messages 重建并核对 query 与官方既有历史后返回冻结检索缓存，不含 gold。原 MiniLM 仍只用于 Parlant 内部检索；BGE 用于 corpus 候选检索。

每题新 session，静默装载全部既有历史、零模型调用／零处理任务，仅当前轮 trigger 一次；180秒上限；未完成基础设施故障最多恢复一次。原生 CannedResponseGenerator._build_draft_prompt 与 MessageGenerator builder 都安装观测，并解析实际 prompt 的双重渲染 staged events，逐字比较5个 full ID 与全文。已完成答案遇审计错误时停止而不再生成，避免 R0 canary 协议偏差。会话/后台任务清理与最终服务停止见 FINAL_VALIDATION。证据覆盖只说明进入最终上下文，不证明事实推理正确。

辅助 R0 lexical F1={summary['R0']['lexical_f1']} / ROUGE-L F1={summary['R0']['rouge_l_f1']}；R1 F1={summary['R1']['lexical_f1']} / ROUGE-L={summary['R1']['rouge_l_f1']}。使用 R0 相同小写/去标点与冠词、max-reference token overlap 与 LCS F1。逐条引用是否在实际 Top5 保存；没有引用时为空集合，不能把空集当成功。未运行事实裁判，回答准确率 unavailable，不能把词面指标或引用有效称为事实正确。

## 调用、缓存、时间和成本

{usage_table}

R0 历史完整用量直接引用原 USAGE.json；十题首次正式用量另在 R0_vs_R1_ten.jsonl，与原启动/异常额外费用分开。R1 startup/formal/abnormal 互斥；所有 schema 内部调用与 provider create 均记录。HTTP 观测改挂实际客户端的 httpx2.AsyncClient.send，离线确认类身份；观察到的物理请求、状态与重试以日志为准，缺失 usage 或字段保持 unavailable，不伪造0。鉴权／余额错误或3次持续接口错误停止，不发付费预检。成本按 [DeepSeek 官方页面](https://api-docs.deepseek.com/quick_start/pricing/) 核对的 flash 单价和真实 UTC 时段/cache hit/miss/output 估算，仅对已知实际 response model 适用；发票/余额 unavailable。检索离线不调用模型 API。

## 完整性、复现与交付

R0 与 src 共 {len(preservation)} 文件 hash 不变，依赖列表不变；REPRODUCE.md 仅追加本轮。所有新代码在 benchmarks/parlant_rag_r1，results/review/runtime 独立。未 commit/push、未运行 HOLDOUT、未自动进入混合检索以外的新候选或下一轮。复现见 REPRODUCE_COMMANDS.md；付费脚本阻止重跑现有十题。UPLOAD_SANITIZED.tar.gz 与 UPLOAD_SHA256.txt 为脱敏上传包／SHA-256，不含根 .env、密钥、权重、整库 embedding、原始全量 human、HOLDOUT 答案；带模型/语料/index hash 和完整 DEV/十题审计。
'''
    (REVIEW/'REPORT.md').write_text(report)
    print('REPORT WRITTEN',selection,len(successes),usage['total'],flush=True)

if __name__=='__main__':main()
