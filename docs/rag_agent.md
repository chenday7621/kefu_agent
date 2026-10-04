# RAG Knowledge Agent：冻结 R1-B

本实验使用 IBM MTRAG 人工主集的 Cloud 子域。目标是在固定 Parlant 生成规则下比较检索方式，并检查检索改善是否转化为回答改善。

## BM25 baseline

R0 使用完整官方 passage_level 语料，不重新分块。BM25 索引 `title + 原始 text`，Unicode casefold 与 word tokenizer，无 stemming/stopwords；`k1=1.2`、`b=0.75`。查询只取当前用户问题，按分数降序、完整 passage ID 升序处理并列。

语料实际为 **72,442 passages / 8,578 parent documents**，来自冻结 commit 的 Cloud LFS 完整资产。上游 README 的 61,022 与该冻结资产的实际段落计数不一致；以实际资产、LFS 与 hash 核验为准，未因 README 数量改变语料，差异根因仍 unavailable。下载、LFS OID、文件 hash 与 qrels 映射曾逐项核对，没有用去偏移 ID 的方式凑匹配。公开仓库仅保留来源与 hash 元数据，数据自行获取。

## BGE dense retrieval

R1 开发阶段固定比较当前问题 BM25、history-aware BM25、当前问题 BGE、history-aware BGE 和 RRF。按照预先固定的 Recall@5 / nDCG@5 / Recall@10 选择顺序和提升门槛选中 **R1-B**，随后冻结并验证 HOLDOUT。本次展示整理没有重新运行候选选择。

最终配置使用现成 `BAAI/bge-base-en-v1.5`，仅当前问题，加 model-card query instruction；passage 不加 query instruction。CLS pooling、L2 normalize、float32 CPU；归一化向量 dot product 排序，完整 ID 决定并列次序。最大输入 512 tokens，编码截断不改变 Retriever 返回的完整片段。完整 corpus 只编码一次并缓存。

离线评分取 Top-10，回答取 Top-5 全文。没有 rewrite、reranker、hybrid 或模型训练。运行输入仅为 speaker/text 历史、当前问题与实际检索结果；reference、qrels、answerability、官方 rewrite 和现成 contexts 均不进入生成。

## HOLDOUT 验证

26 个 Cloud conversation 按 ID 排序，再用 Random(42) 固定 60% DEV / 40% HOLDOUT，得到 15 / 11 个对话；同一对话不拆分。HOLDOUT 为自定义划分，不是官方 test。全部语料可检索，不只索引正确证据。

| Metric | BM25 R0 | BGE R1-B |
| --- | --- | --- |
| Recall@5 | 0.223092 | 0.365462 |
| Recall@10 | 0.267269 | 0.406024 |
| nDCG@5 | 0.188095 | 0.332931 |
| nDCG@10 | 0.206481 | 0.352282 |
| Recall@10 = 0，任务数 | 53 | 35 |

83 个有 qrels 的任务计入 task macro 均值，6 个无标注任务记 unavailable。Recall@5 增加 14.24pp；没有根据 HOLDOUT 结果继续调参，也不作统计显著性结论。

另用 Random(42) 在每个 HOLDOUT conversation 选一个目标轮，共 11 题，BM25/BGE 各生成一次，共 22 个正式回答。每题新会话，静默加载原历史，只触发当前问题。两组 lexical F1 为 0.281087 / 0.246341，ROUGE-L 为 0.188228 / 0.174511，说明此次检索提升未表现为词面指标的生成提升。词面相似度和来源 ID 有效性均不等于事实正确率。该轮有一次基础设施恢复，部分原 prompt 文件与调用用量缺失，完整成本不可用；不能将其审计描述为所有原始 prompt 均完整。

## R2：Evidence-aware Generation 负实验

R2 使用完全相同的 BGE Top-5、顺序、全文、问题、历史和三条 RAG 规则，比较同期 CONTROL 与显式证据包装：Evidence 编号、Source ID、Title、Content 及四条生成提醒。原历史仍保留，包装重复呈现历史和当前问题，因而该消融包含重复上下文及提示变化。

原 11 个目标轮次两组各生成一次，22 attempts 均完成。旧 R1-B 答案只读保留，没有冒充本轮 CONTROL。结果如下：

| Metric | CONTROL | R2-EVIDENCE |
| --- | --- | --- |
| lexical F1 | 0.295370 | 0.287674 |
| ROUGE-L | 0.215459 | 0.210909 |
| 平均回答词数 | 100.73 | 115.55 |
| 输入 tokens | 174,936 | 175,001 |
| 输出 tokens | 7,711 | 7,945 |

两组各 9/11 个回答有引用，引用 ID 均来自实际 Top-5；全部 22 个最终 prompt 的证据覆盖曾独立核验。更显式的包装没有显示整体生成收益，回答更长。输入 token 增加约 0.037%，输出增加约 3.03%，并非输入成本显著膨胀。

定性检查观察到：bots 相关问题存在足够文档证据时 R2 仍过度保留；Cloudant Lite 的具体限额缺少当前 Top-5 支持时，两组仍可能沿用历史信息。也有局部澄清改善，不能据此宣称整体获益。人工抽样的 8 份回答标签仍为 **unreviewed**，没有事实准确率、人工完成率或 LLM judge 分数。

这 11 题已在上一轮观察过，不构成新的未见泛化验证。**R2 保留为负实验，不作为最终版本**；最终展示版本保持 R1-B 原生 Retriever 与原生成规则。检索质量提升不一定直接带来生成提升。

## 源码与复现

配置与模型文件 hash 见 [protocol.json](../benchmarks/parlant_rag_r1/protocol.json)、[model_lock.json](../benchmarks/parlant_rag_r1/model_lock.json)。[HOLDOUT runner](../benchmarks/parlant_rag_holdout/run.py) 保留 BM25/BGE 配对协议；[R2 evidence.py](../benchmarks/parlant_rag_r2/evidence.py) 仅作消融源码记录。资产来源、冻结限制及命令见 [复现说明](reproduce.md)。公开仓库不包含语料、问题全文、答案、运行缓存或完整审计。
