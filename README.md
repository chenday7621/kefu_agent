# Parlant Agent Optimization Framework

基于 Parlant 构建的企业级客服 Agent，通过事务决策优化和知识增强检索提升实际任务完成能力。

项目包含 **Transaction Agent** 和 **RAG Knowledge Agent**。当前展示版本冻结为 **Retail O1-B** 与 **RAG R1-B**；C1 降本和 R2 Evidence-aware Generation 保留为实验记录。

## Features

### Tool-use Agent

- 对接官方 Retail 环境，由 Parlant 调用官方工具。
- 账户与订单查询、商品及变体属性匹配、退换货流程。
- O1-B 改进账户查询推理和商品约束推理，保留官方业务政策与评分。

### RAG Agent

- 使用 IBM MTRAG 人工主集的 Cloud 子域。
- 比较本地 BM25 lexical retrieval 与 BGE dense retrieval。
- 将实际检索的 Top-5 完整片段通过原生 Retriever 交给 Parlant，记录证据进入生成 prompt 的情况及来源引用。

## Architecture

```text
User
 |
Parlant Agent
 |
+-------------------------------+
| Transaction: official tools   |
| RAG Retriever: BGE Top-5       |
+-------------------------------+
 |
DeepSeek API
```

两条路径均通过 Parlant 引擎生成回答。事务路径使用官方工具桥接；RAG 路径使用本地检索证据。运行记录包含内部模型调用与用量。

## Benchmark

### Transaction Agent

Benchmark: **tau³-bench Retail**（使用官方 tau2-bench 仓库中的 Retail 环境）。

| Baseline | Optimization | Metric | Result | Change |
| --- | --- | --- | --- | --- |
| B0-R1 | O1-B | Success Rate | 37.50% → 58.75% | +21.25pp |

优化来自 account lookup reasoning 与 product constraint reasoning，包括订单证据查找、变体约束和操作范围澄清。这是 Agent 业务关联规则的改进，没有训练模型。

结果来自官方 Retail test 的 40 个任务、每组两轮，共每组 80 个执行单元（30/80 → 47/80），使用自定义 DeepSeek 配置，不是官方默认榜单成绩。包含一个用户授权的基础设施恢复预算例外；模拟用户、模型与评分过程存在随机性，未作统计显著性结论。案例与范围见 [Transaction Agent 文档](docs/transaction_agent.md)。

### RAG Agent

Benchmark: **MTRAG Cloud，自定义 conversation-level HOLDOUT**。

| Retrieval | Configuration | Recall@5 |
| --- | --- | --- |
| BM25 | R0，当前用户问题 | 22.31% |
| BGE Retrieval | R1-B，当前用户问题 | 36.55% |

绝对提升 **14.24pp**。该实验比较 lexical retrieval 和 dense retrieval；BGE 使用现成的 `BAAI/bge-base-en-v1.5`，没有重新训练。固定完整 Cloud 官方 passage_level 语料、conversation 级划分及检索参数。

HOLDOUT 共 11 个对话、89 个任务，其中 83 个有 qrels 的任务计入均值；6 个无标注任务保持 unavailable。此结果不是完整 MTRAG test 成绩，也不是回答事实准确率。详见 [RAG Agent 文档](docs/rag_agent.md)。

## Ablation

**R2 Evidence-aware Generation** 使用相同 BGE Top-5，改变生成阶段证据包装及提示。11 个固定目标轮次的同期 CONTROL/R2 对照未显示整体生成收益：lexical F1 和 ROUGE-L 均小幅下降，回答变长。

检索质量提升不一定直接带来生成提升。R2 保留为负实验，不作为最终版本；C1 降本也不作为默认配置。词面相似度和引用命中不能直接称为事实准确率。

## Installation

已验证环境为 Python **3.12.3**、Parlant **3.3.2**、CPU PyTorch **2.8.0+cpu**、Transformers **4.53.3**。项目支持范围见 [pyproject.toml](pyproject.toml)；Retail 外部评测环境需要 Python 3.12–3.13，并使用独立虚拟环境。

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

[requirements.txt](requirements.txt) 引用项目已有 `deepseek` extra；上述 pip 命令按依赖约束安装，不保证得到历史环境的全部精确版本。已有 uv 用户可使用 `uv sync --frozen --extra deepseek` 读取 [uv.lock](uv.lock)。CPU wheel 来源及精确环境要求见 [复现说明](docs/reproduce.md)。

自行在根目录 `.env` 配置 `DEEPSEEK_API_KEY`，限制文件权限为 `600`。模型请求使用 DeepSeek 官方 `deepseek-chat`。RAG 检索模型为 `BAAI/bge-base-en-v1.5`；Parlant 内部 embedding 使用本地 `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`。模型权重需要自行获取。

## Reproduce

1. Retail：入口为 [heldout supervisor](benchmarks/parlant_retail_heldout/supervise.py)，O1-B 业务配置见 [policy_config.py](benchmarks/parlant_retail_o1b/policy_config.py)。
2. RAG：入口为 [HOLDOUT retrieval](benchmarks/parlant_rag_holdout/retrieve.py)、[offline scoring](benchmarks/parlant_rag_holdout/score.py) 与 [Parlant generation](benchmarks/parlant_rag_holdout/run.py)。最终版本只采用 R1-B 当前问题 dense retrieval。

具体命令、数据来源和前置条件见 [docs/reproduce.md](docs/reproduce.md)。评测入口保留历史冻结校验；公开仓库不包含运行所需的原始数据、结果及启动快照，因此 fresh clone 需要另行准备资产与冻结文件，不能直接恢复历史成绩。API key 由用户自行配置；生成和 Retail 评测可能产生费用。

不上传数据集、密钥、模型权重、缓存、完整审计报告或结果包。展示默认选择记录在 [configs/release_defaults.json](configs/release_defaults.json)，该文件是展示清单，不会自动改变已有 runner 的行为。

## Results

| Experiment | Scope / metric | Baseline / CONTROL | Final / variant |
| --- | --- | --- | --- |
| Retail O1-B | 40 test tasks × 2；Success Rate | 37.50% | **58.75%** |
| RAG R1-B | 83 annotated HOLDOUT tasks；Recall@5 | 22.31% | **36.55%** |
| R2 ablation | 11 target turns；lexical F1 | 0.295370 | 0.287674 |
| R2 ablation | 11 target turns；ROUGE-L | 0.215459 | 0.210909 |

## Attribution and License

基于 [Parlant](https://github.com/emcie-co/parlant) v3.3.2，保留上游源码、作者与 [Apache-2.0 LICENSE](LICENSE)。评测环境来自 [tau2-bench](https://github.com/sierra-research/tau2-bench)，RAG 数据来自 [IBM MTRAG](https://github.com/IBM/mt-rag-benchmark)，检索模型来自 [BAAI BGE](https://huggingface.co/BAAI/bge-base-en-v1.5)。数据及模型须遵守各自许可。
