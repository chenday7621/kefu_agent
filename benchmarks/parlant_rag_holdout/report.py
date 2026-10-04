"""Read-only summaries plus new audit artifacts, no generation or tuning."""
from common_holdout import *
import collections,subprocess,socket
def table(headers,rows):
    return '| '+' | '.join(headers)+' |\n|'+ '|'.join(['---']*len(headers))+'|\n'+'\n'.join('| '+' | '.join(map(str,r))+' |' for r in rows)+'\n'
def fmt(x):return f'{x:.6f}' if isinstance(x,float) else str(x)
def main():
    state=json.loads((RESULT/'smoke_status.json').read_text());assert state['service_stopped']
    before=json.loads((REVIEW/'PRESERVATION_BEFORE.json').read_text())
    changes=[p for p,h in before.items() if sha(PROJECT/p)!=h];assert not changes,changes
    deps=subprocess.run([str(PROJECT/'.venv/bin/python'),'-m','pip','freeze'],capture_output=True,text=True,check=True).stdout
    assert deps==(REVIEW/'DEPENDENCIES_BEFORE.txt').read_text()
    for manifest in ['ASSETS_FROZEN.json','RETRIEVAL_FROZEN.json','GENERATION_FROZEN.json','RESUME_FROZEN.json']:
        for p,h in json.loads((REVIEW/manifest).read_text())['files'].items():assert sha(PROJECT/p)==h,p
    details=readl(RESULT/'answer_details.jsonl');metrics=json.loads((RESULT/'offline_metrics.json').read_text());groups=json.loads((RESULT/'grouped_metrics.json').read_text());aux=json.loads((REVIEW/'AUXILIARY_COMPARISON.json').read_text())['groups'];usage=json.loads((REVIEW/'USAGE.json').read_text())
    complete=[x for x in details if x['status']=='completed'];assert len(complete)==22
    assert all(x['evidence_in_final_prompt'] is True for x in complete)
    from metrics_utils import read_obs
    obs=[x for x in read_obs() if x['attempt_id']!='offline-checks']
    assert sum(x['kind']=='history_loaded' for x in obs)==23
    assert all(x['model_calls_during_load']==0 and x['processing_task_absent'] for x in obs if x['kind']=='history_loaded')
    assert sum(x['kind']=='session_cleanup' for x in obs)==22
    open_ports=[]
    for port in [18902,18920]:
        with socket.socket() as s:
            s.settimeout(.3)
            if s.connect_ex(('127.0.0.1',port))==0:open_ports.append(port)
    assert not open_ports
    prompt_available=sum(bool((RESULT/'attempts'/x['attempt_id']/'final_generation_prompt.txt').read_text()) for x in complete)
    write(REVIEW/'PROMPT_TEXT_AVAILABILITY.json',{'runtime_coverage_observations':22,'nonempty_original_final_prompts':prompt_available,'empty_original_final_prompts':22-prompt_available,'empty_files_never_reconstructed':True})
    write(REVIEW/'FINAL_VALIDATION.json',{'preserved_files':len(before),'modified_original_files':changes,'dependencies_unchanged':True,'frozen_manifest_hashes_passed':True,'completed_answers':22,'attempts':len(state['outcomes']),'runtime_observed_prompt_full_text_ID_coverage':22,'nonempty_original_final_prompts':prompt_available,'empty_original_final_prompts':22-prompt_available,'silent_history_loads':23,'model_calls_loading_history':0,'explicit_session_cleanup':22,'interrupted_transient_session_cleanup':'process exited; explicit event/session deletion not observed','all_owned_ports_closed':True,'service_stopped':True,'retrieval_configuration_unchanged':True,'no_DEV_run_or_tuning':True})
    columns=['Recall@5','Recall@10','nDCG@5','nDCG@10','Recall@10_zero_tasks']
    overall=table(['方法']+columns,[[c]+[fmt(metrics[c][k]) for k in columns] for c in ['R0','R1-B']])
    grouped=''
    for key in ['current_turn','prior_history_messages','history_length_words']:
        rows=[]
        for label,a in groups['R0'][key].items():
            b=groups['R1-B'][key][label]
            rows.append([label,a['annotated_tasks'],a['unannotated_tasks']]+[fmt(a[k])+' → '+fmt(b[k]) for k in columns])
        grouped+='\n### '+key+'\n\n'+table(['组','有qrels','unavailable']+columns,rows)
    comparisons=readl(RESULT/'retrieval_comparison.jsonl')
    counts=collections.Counter('unavailable' if r['delta']['Recall@5']=='unavailable' else 'improved' if r['delta']['Recall@5']>0 else 'declined' if r['delta']['Recall@5']<0 else 'unchanged' for r in comparisons)
    paired=[]
    for pair in readl(RESULT/'R0_vs_R1B_answers.jsonl'):
        a=pair['R0'];b=pair['R1-B'];paired.append([pair['task_id']]+[fmt(a[k])+' → '+fmt(b[k]) for k in ['lexical_f1','rouge_l_f1']]+[fmt(a['retrieval_metrics']['Recall@5'])+' → '+fmt(b['retrieval_metrics']['Recall@5']),str(a['evidence_in_final_prompt'])+' / '+str(b['evidence_in_final_prompt']),fmt(a['seconds'])+' / '+fmt(b['seconds'])])
    paired_table=table(['task_id','F1 R0→R1-B','ROUGE-L R0→R1-B','Recall@5 R0→R1-B','prompt覆盖','秒 R0/R1-B'],paired)
    usage_rows=[]
    for name,u in [('startup',usage['startup']),('formal R0',usage['formal']['R0']),('formal R1-B',usage['formal']['R1-B']),('abnormal',usage['abnormal']),('total',usage['total'])]:
        t=u['token_totals'];usage_rows.append([name,u['schematic_calls'],u['physical_http_requests']]+[t[k] for k in ['prompt_tokens','prompt_cache_hit_tokens','prompt_cache_miss_tokens','completion_tokens']]+[u['cost_estimate_usd'],u['known_cost_estimate_usd']])
    usage_table=table(['归属','schema','HTTP','input','cache hit','cache miss','output','估算USD','已知USD小计'],usage_rows)
    assets=json.loads((REVIEW/'ASSETS_FROZEN.json').read_text())['files']
    hashkeys=['benchmarks/parlant_rag/data/split.json','benchmarks/parlant_rag/data/cloud_passages.jsonl','benchmarks/parlant_rag_r1/model_lock.json','benchmarks/parlant_rag_r1/model/model.safetensors','benchmarks/parlant_rag_r1/index/corpus.npy','benchmarks/parlant_rag_r1/index/queries.npy','benchmarks/parlant_rag_r1/index/queries.json']
    hashes=table(['冻结资产','SHA-256'],[[k,assets[k]] for k in hashkeys])
    report=f'''# RAG R1-B Cloud HOLDOUT 验证

冻结的 R1-B 在本次自定义 HOLDOUT 上检索指标改善：Recall@5 **{metrics['R0']['Recall@5']:.6f} → {metrics['R1-B']['Recall@5']:.6f}**，绝对增加 **{metrics['R1-B']['Recall@5']-metrics['R0']['Recall@5']:.6f}**。这是 MTRAG 人工 Cloud 子域自定义 conversation-level HOLDOUT 验证，非官方 test 或完整 MTRAG 成绩；不作统计显著性或回答事实正确率结论。

用户确认 HOLDOUT 仅 11 个 conversation，因此调整为 **11 个目标题、每题 R0/R1-B 各一次，共 22 个完成回答**。Random(42) 从按 task_id 排序的 89 轮 shuffle，取每对话首次出现的一轮；题单在检索和标注读取前冻结，未按答案类型、质量、难度或检索挑题。两组先后按题序交替；所有题新建独立会话，官方历史逐字静默装载。

## 资产、配置与输入隔离

IBM MTRAG commit `2c618bb98db3c8526433e22d8a2f7320f10a7470`，仓库 Apache-2.0；Cloud passage_level ZIP SHA-256 `a0c5427013c65556aeddfdc246dd6068f3a7fad08ce6ae7adddaa9e87aae246d`，Git blob `1a97d1c8fa9bdef184edaf669680b1b5f162eb07` 与冻结 commit tree 一致，非 LFS pointer，CRC 正常。72,442 唯一 `ibmcld_数字-start-end` passages，8,578 parent IDs，全部 Cloud URL；ZIP 内 JSON records 与 R0 完整一致，仅 JSONL whitespace 序列化不同。README 61,022 与官方资产不一致，沿用已核对资产，编辑/版本差异根因 unavailable，不重下载、不剥 passage offsets。

BGE-base-en-v1.5 revision `a5beb1e3e68b9ab74eb54cfd186867f64f240e1a`，MIT，CPU float32 CLS、L2 normalize、512 右截断；query instruction 为 `Represent this sentence for searching relevant passages: `，当前问题；passage title+space+原 text 无 query instruction。原 BM25 k1=1.2/b=.75、tokenizer 与排序不变。Dense 点积 descending / full ID ascending（原 ID 已排序，stable argsort），Top10 评分，Top5 全片段返回；完整 corpus.npy 未重编码。原 query cache 只读复用，新 89 个 current query 均 cache miss，按冻结函数一次编码到 HOLDOUT overlay，overlay hash 见 RETRIEVAL_FROZEN.json。无 history query、reranker、rewrite、hybrid 或新分块。

{hashes}

原 DEV/HOLDOUT split 不变，HOLDOUT 11 对话/89 轮，DEV 未运行或重新分析。运行入口只解码 task/conversation 路由和 input.speaker/text，跳过 gold/rewrite/metadata 字段；最终运行输入不含 qrels、reference、contexts、answerability、feedback、enrichments。官方历史本来是任务提供的旧 agent 话语，允许作为历史，不再生成。题单和检索结果落盘、hash 冻结后，独立 score.py 才读取 HOLDOUT qrels；回答全部完成并服务停止后，analyze.py 才读取本轮选题 targets.text，参考答案只进入评分侧。用户的禁止读取金标按“运行/检索输入禁用，独立评分侧允许”执行，否则无法计算要求的指标。官方 rewrite 内容和 answerability 均不用于运行或评分。

## HOLDOUT 检索

83 轮有 qrels，6 轮无 qrels 保持 unavailable，不计均值。官方 qrels 文件名 dev.tsv 是上游资产名称，本轮按已冻 conversation HOLDOUT task_id 过滤；不把官方文件名当本地 DEV。评分逐题复用 R0 common.metric：binary Recall、linear-gain DCG/log2(rank+1) 及 ideal DCG，task macro。full passage ID 精确映射遗漏 0，无答案/无标注不强记 0。

{overall}

按 Recall@5：改善 {counts['improved']}，下降 {counts['declined']}，不变 {counts['unchanged']}，unavailable {counts['unavailable']}。完整四指标 delta 及任务清单见 results 的 retrieval_comparison.jsonl、improved_tasks.jsonl、declined_tasks.jsonl、zero_recall10 文件；其他指标可能有不同方向，不以单一方向覆盖它们。

{grouped}

多轮各组平均 Recall@5 均改善，7+轮增加 {groups['R1-B']['current_turn']['7+']['Recall@5']-groups['R0']['current_turn']['7+']['Recall@5']:.6f}；这来自冻结 current-query dense，不是加入历史 query。仍有困难任务和个别回归，例如首轮 Recall@10=0 由 3 增至 4；1–100 历史词组 Recall@10 有下降。未据 HOLDOUT 结果调整任何配置。

## Parlant 对照与辅助效果

Parlant v3.3.2，官方 DeepSeek 请求 deepseek-chat，原三条 RAG 规则/原内部 ID、创建时间与元数据完全恢复，CANNED_FLUID、BasicOptimizationPolicy、原 generation 参数不变。单一 Agent 对两组仅更换原生 Retriever 的 Top5；不直接调用模型回答、不调用模拟用户、LLM judge、回复改写/纠错或 Agent Plan。启动没有新规则评价/API调用；两次服务启动均复用同一 frozen metadata。history 装载 23 次均未触发模型或后台 processing；22 个完成会话显式删除 session/events，所有实际最终生成 prompt 的 staged tool 部分解码后与返回的 5 段 full ID/text 完整相等。进入 prompt 不等于模型事实使用正确。

{paired_table}

R0/R1-B 的 macro lexical F1 为 **{aux['R0']['lexical_f1']:.6f} / {aux['R1-B']['lexical_f1']:.6f}**，ROUGE-L F1 为 **{aux['R0']['rouge_l_f1']:.6f} / {aux['R1-B']['rouge_l_f1']:.6f}**；使用原 R0 规范化 word overlap 和 LCS F1（casefold、去标点/冠词、max reference）。这是辅助相似度，不能称回答准确率。引用来自实际 Top5 的答案数 {aux['R0']['all_citation_ids_from_actual_top5']}/11 与 {aux['R1-B']['all_citation_ids_from_actual_top5']}/11；没有引用时该检查 vacuously true，另报实际有引用 {aux['R0']['answers_with_citations']}/11 与 {aux['R1-B']['answers_with_citations']}/11。每题实际全文/ID、Top10、引用、最终 prompt、provider usage、耗时见 answer_details.jsonl、R0_vs_R1B_answers.jsonl 和 attempts。

失败分类见 FAILURE_ANALYSIS.md / failure_analysis.jsonl：A 用 qrels 做“全部/部分正确证据未进 Top5”的可核验检索诊断，标注并非完整语义证据集合；B/C 仅对逐题 reference、实际片段和回答做保守定性检查，不以 F1/ROUGE 或引用 ID 判事实。无法确定的 B/C 标 unverified，不把 qrels 缺失当 C，不给量化事实正确率。

**Prompt 留存限制：**最终交付复核发现首次进程的9份 original final prompt 文本为空，原 per-call prompt 文本也有缺失。运行时 fsynced observations 仍保存各 passage ID、full_text_in_prompt / exact_staged_payload 检查和 original prompt SHA-256，22份现场覆盖均通过；但只有恢复后13份原文本可以独立解码重验。空文件没有重构、冒充原始 prompt 或额外生成，具体文件丢失机制 unavailable。因此不能宣称22份原 prompt 文本完整留存/全部可离线独立重验，详见 PROMPT_ARTIFACT_AUDIT.json。

## 异常、调用与成本

前 9 个正式回答完成后，05_R1-B_attempt1 在 3 个 guideline schema 调用阶段进程意外退出；退出原因/status unavailable。现场有部分已返回 usage、未结束请求和缺 usage，**没有 draft 生成或最终回答记录**。按冻结协议仅恢复该未完成执行一次为 attempt2；9 个已完成回答全部跳过，剩余题首次运行。共 23 attempts、22 完成回答，无正常错误/拒答重跑。异常瞬态会话随进程退出而消失，但显式删除日志缺失，不伪造清理成功；恢复后完成的 22 会话均有删除验真。初次进程与恢复的日志分别保留 RUN.log / RESUME.log，恢复代码和约束见 RESUME_FROZEN.json，原 generation code/hash 未改。

{usage_table}

schema、provider create 与真实 httpx2 physical send 都记录；22 个目标回答并非 22 次API请求。startup/formal 两组/abnormal 互斥，异常请求缺 usage 时完整总成本与相关 token 为 unavailable，已知小计单列。离线 mock transport 的 1 次内存测试从真实 API 统计剔除；不是付费预检。[DeepSeek 官方价格](https://api-docs.deepseek.com/quick_start/pricing/)执行前核对：仅对实际 deepseek-flash 响应套价，本轮周末 USD/百万 hit=.003、miss=.15、output=.6；未知模型/字段 unavailable，估算非账单，余额与发票 unavailable。实际请求参数及响应 model 在 observations.jsonl，不按名称假设响应型号不变。

## 完整性与复现

原 R0/R1 代码、数据、split、模型/缓存、results、reviews 和核心源码 {len(before)} 个既有文件 hash 全部不变，依赖不变。REPRODUCE.md 仅追加本轮。没有 commit/push、DEV重跑、检索调参、reranker、rewrite、hybrid、prompt优化或新优化循环。服务已停止，18902/18920 端口已关闭。新 results `{RESULT.relative_to(PROJECT)}`；独立运行存储 `{PATHS['runtime']}`。

复现与只读核验命令见 REPRODUCE_COMMANDS.md；现有结果禁止再次付费生成。完整资产 hashes 在 ASSETS_FROZEN/RETRIEVAL_FROZEN/GENERATION_FROZEN，交付清单在 HASH_MANIFEST.json。脱敏上传包与 SHA-256 见 UPLOAD_SANITIZED.tar.gz / UPLOAD_SHA256.txt，不含 .env、key、权重、全 corpus/index、DEV结果或原人类全集，保留公开 benchmark IDs 以便核对。
'''
    (REVIEW/'REPORT.md').write_text(report)
    print('REPORT/INTEGRITY COMPLETE',flush=True)
if __name__=='__main__':main()
