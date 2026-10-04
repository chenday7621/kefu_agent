# 复现入口与资产边界

公开版本展示已冻结的 Retail O1-B 和 RAG R1-B。源码、协议和精简结果可检查；官方数据、模型权重、缓存、结果目录、完整审计和运行快照留在本地。此次整理只做文件及 Git 检查，没有执行 benchmark、模型推理或 DeepSeek 请求。

## 环境

| Component | 历史运行环境 |
| --- | --- |
| Python | 3.12.3 |
| Parlant | 本仓库 3.3.2，上游基点 `61bba3b2b3fffd677d345e393e8c942dbd400297` |
| PyTorch | 2.8.0+cpu |
| Transformers | 4.53.3 |
| NumPy | 2.5.3 |
| huggingface_hub | 0.36.2 |
| python-dotenv | 1.2.4 |
| DeepSeek | 官方 API，请求模型 `deepseek-chat` |
| Retail environment package | 外部环境 tau2 1.0.1 |

安装 Parlant 及已有 DeepSeek extra：

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

这个命令安装 `pyproject.toml` 声明的依赖，不承诺与历史环境逐包一致。需要历史 CPU torch 时先从 PyTorch CPU wheel index 安装 `torch==2.8.0`，再安装项目；已有 uv 用户可以 `uv sync --frozen --extra deepseek` 使用锁文件。上述命令是安装说明，本次没有执行安装或改变依赖。

Parlant 内部 embedding 为本地 `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`；RAG 检索 embedding 为 BGE，二者作用不同。自行准备模型文件。根目录 `.env` 自行配置 `DEEPSEEK_API_KEY`，运行前 `chmod 600 .env`。不要将密钥提交到 Git。

Retail 使用独立虚拟环境，因为官方 tau2 的 LiteLLM 约束与 Parlant 的可选 LiteLLM extra 不同。不要为运行 Retail 将两套依赖合并。官方数据与 package 必须来自下述锁定源码，而不是仓库内的自造环境。

## 官方资产与冻结版本

| Asset | Source | Frozen revision |
| --- | --- | --- |
| Retail | [sierra-research/tau2-bench](https://github.com/sierra-research/tau2-bench) | `fc0055dc4e0a316c3f83133267fbd6faaa770992` |
| MTRAG human Cloud | [IBM/mt-rag-benchmark](https://github.com/IBM/mt-rag-benchmark) | `2c618bb98db3c8526433e22d8a2f7320f10a7470` |
| BGE retrieval | [BAAI/bge-base-en-v1.5](https://huggingface.co/BAAI/bge-base-en-v1.5) | `a5beb1e3e68b9ab74eb54cfd186867f64f240e1a` |

IBM 数据使用 Apache-2.0，BGE model card 标注 MIT；Parlant 保留 Apache-2.0。其他外部资产以各自仓库许可为准。模型文件逐项 SHA-256 见 [model_lock.json](../benchmarks/parlant_rag_r1/model_lock.json)。可公开的核心资产 hash 和实验范围汇总见 [results_summary.json](../configs/results_summary.json)。

## 运行前置条件

下面命令是**历史入口说明**，不是 fresh clone 的一键复现承诺。运行前需要完整外部资产、本地冻结文件与新建输出目录。不要在原已完成结果目录重跑，也不要关闭冻结校验。

Retail 的 `protocol_support.verify()` 校验代码、历史 Git 基点及冻结配置；RAG HOLDOUT 的 `prepare.py` 依赖 R0/R1 既有冻结文件、split、BM25 cache、BGE index 和 R0 启动配置。本次展示 commit 会改变 Git HEAD，因此原历史 Git 校验不能直接用于当前展示 HEAD。精确重放需要归档环境或独立的新冻结过程；这不是本次发布整理的范围。

这些未公开的运行文件无法仅由 README 中的指标恢复。公开源码用于检查协议及实现；如另行重建实验，需要自行下载合法资产并在独立工作副本生成自己的 freeze，不能将新结果冒充本项目历史结果。

本地完整 `REPRODUCE.md` 保留原审计命令，不进入公开 Git。带个人路径的本地报告/打包助手也保留在磁盘并忽略；核心检索、评分、Parlant runner 和版本配置仍公开。

## Retail benchmark 入口

O1-B 开发入口与正式 heldout 对照入口分别为：

```bash
# 需预先在 benchmarks/tau2-bench 准备锁定官方源码、数据及独立 .venv。
# 下列 supervisor 会调用模型、模拟用户和官方评分，可能产生费用。
benchmarks/tau2-bench/.venv/bin/python benchmarks/parlant_retail_o1b/supervise.py --result-dir results/tau3_retail_o1b_dev30_NEW
benchmarks/tau2-bench/.venv/bin/python benchmarks/parlant_retail_heldout/supervise.py --result-dir results/tau3_retail_heldout_NEW
```

正式对照配置是 `parlant_retail_heldout/configs/BASE`（B0-R1）和 `configs/OPT`（O1-B）；计划固定 40 个 test task、seed 42/43、每组两轮。具体参数见 [protocol.json](../benchmarks/parlant_retail_heldout/protocol.json) 与 [execution_plan.json](../benchmarks/parlant_retail_heldout/execution_plan.json)。历史恢复补充授权仅用于该次基础设施异常，不能作为正常失败重跑策略。

## RAG benchmark 入口

数据准备实现见 [R0 prepare.py](../benchmarks/parlant_rag/prepare.py)：仅下载锁定 IBM human Cloud 所需资产，核对 LFS/完整 passage ID，建立固定 split。模型下载与一次编码实现见 [download_model.py](../benchmarks/parlant_rag_r1/download_model.py) 和 [encode.py](../benchmarks/parlant_rag_r1/encode.py)。这些脚本会写入派生资产；重建必须使用独立工作副本及新 freeze，不能覆盖已有冻结数据。

HOLDOUT 的处理顺序如下，须先满足上述资产与 freeze 前置条件：

```bash
# 无付费模型调用：冻结输入/选择，再检索，最后在评分侧读取 qrels。
.venv/bin/python benchmarks/parlant_rag_holdout/prepare.py
.venv/bin/python benchmarks/parlant_rag_holdout/retrieve.py
.venv/bin/python benchmarks/parlant_rag_holdout/score.py
```

`retrieve.py` 使用 current query，对比 BM25 R0 与 BGE R1-B；原 corpus/query cache 只读，HOLDOUT 缺失 query 使用独立 overlay。检索 Top-10，Parlant 接收 Top-5 全文；评分复用 R0 `common.metric`，无 qrels 标 unavailable，不纳入均值。

生成入口（先完成 runtime checks 与 generation freeze）：

```bash
.venv/bin/python benchmarks/parlant_rag_holdout/offline_runtime_checks.py
.venv/bin/python benchmarks/parlant_rag_holdout/freeze.py
# 会调用 DeepSeek，要求合法本地密钥、原启动快照和未完成的新输出目录。
.venv/bin/python benchmarks/parlant_rag_holdout/run.py --execute
```

历史协议从 11 个 HOLDOUT 对话各取一个目标轮，R0/R1-B 各一次，合计 22 个正式回答。用户历史静默加载，reference/qrels/answerability 与检索输入隔离；每题独立会话，不把生成结果串到下一题。完整 MTRAG test 不在本项目展示成绩中。

## 展示默认与消融

[release_defaults.json](../configs/release_defaults.json) 声明展示选择 O1-B/R1-B，仅是版本清单，不是新增 runtime dispatcher。历史 runner 保留自己的固定比较协议；C1 和 R2 仅作消融源码记录，不会因这份清单自动执行。

本次没有启动服务、调用 API、运行检索评分或新一轮优化。GitHub 展示仓库为 [chenday7621/kefu_agent](https://github.com/chenday7621/kefu_agent)，仅发布已检查的源码和展示文档。

## GitHub CI 范围

`Verify and Test` 运行 Python 3.12 的 `python scripts/verify_release.py`，仅检查 tracked 文件的 Python 语法、展示配置、公开文档链接、凭据模式及私有资产排除规则。它使用标准库和 Git，不导入 Agent 模块、不运行 benchmark 或模型，不需要 API key。继承的上游 `just test-*` 工作流依赖未公开的 justfile 和模型测试配置，已从自动发布检查中移除。

该检查通过不代表完整 Parlant 单元/集成测试、实验复现或回答准确率通过；展示结果仍来自此前冻结实验。
