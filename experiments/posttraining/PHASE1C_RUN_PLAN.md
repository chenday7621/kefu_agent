# Phase 1-C DEV baseline run plan（本轮 baseline=0）

冻结状态 READY_FOR_QWEN_DEV_BASELINE 指模型接口、代码版本与 DEV/scorer/split 资产已准备完成。本轮没有实现或验证完整 30-episode APP/PG controller，不能把 smoke CLI 当作完整 baseline launcher。需要在下一轮单独接入 controller 后才确定真正执行30题的命令；下面的现有命令是可直接运行的校验/评分命令，不会冒充 baseline 启动命令。

## 冻结入口与现有命令

Bundle: `/nas/chenyi/posttraining-qwen/datasets/20261010_104508_POSTTRAIN_PHASE1C_FREEZE_DEV_V1`。

```bash
cd /home/chenyi/kefu_agent
source /home/chenyi/miniconda3/etc/profile.d/conda.sh
conda activate /mnt/nvme3/chenyi/posttraining/conda-envs/qwen-policy
source /mnt/nvme3/chenyi/posttraining/runtime/activate.sh
source experiments/posttraining/phase1b_env.sh
export CUDA_VISIBLE_DEVICES=<加载前重新确认的一张空闲A100物理index>
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
python -m experiments.posttraining.dev_protocol verify --bundle /nas/chenyi/posttraining-qwen/datasets/20261010_104508_POSTTRAIN_PHASE1C_FREEZE_DEV_V1
python -m experiments.posttraining.offline_checks --review /nas/chenyi/posttraining-qwen/runtime/phase1c-preflight
python -m unittest experiments.posttraining.test_dev_protocol -v
```

未来 controller 导出30个完整episode的canonical审计JSONL后，评分命令为：

```bash
python -m experiments.posttraining.scorer \
  --bundle /nas/chenyi/posttraining-qwen/datasets/20261010_104508_POSTTRAIN_PHASE1C_FREEZE_DEV_V1 \
  --traces /nas/chenyi/posttraining-qwen/runtime/phase1c-baseline/episode-evidence.jsonl \
  --output /nas/chenyi/posttraining-qwen/runtime/phase1c-baseline/scores.jsonl
```

不生成新题替换冻结题，不隐式改 manifest。启动前校验 committed dev_protocol_lock.json、bundle hash、generator/scorer hash、冻结业务hash、revision/weights/tokenizer hashes。

## Controller 接入要求与执行配置

1. 在独立 post-training 入口中加载一次锁定 Qwen3-8B；仅 SingleToolBatchSchema 更换为本地Qwen policy，其他 schema 保留冻结 provider，APP always_apply / native converter / validator / gate 原样。
2. 本地 backend 从单次 smoke backend扩展为明确总预算、顺序GPU调度的多decision接口；保持 bf16、eval、inference_mode、official template、enable_thinking=False、use_model_defaults=False、do_sample=False、max_new_tokens<=1024，断言class decoder配置。不能仅解除单次guard后无限重试。
3. controller materialize当前logical fixture到隔离测试PG/session/MCP，再验证FK、confirmation、snapshot、request/reservation初始状态。禁止使用生产DB，禁止把 trusted_context/history、capability、customer identity 原文送Qwen。当前阶段没有执行这种 materialization。
4. 控制器按 frozen user_followups发送真实 customer事件，展示后按native exact phrase确认，记录真实event ID；不能用字符串“已确认”替代服务端确认绑定。F2修改与撤回/恢复按注册触发执行，F3不得提交或准备新操作。
5. canonical trace adapter使用原始native结果和DB读取作权威投影，保留raw payload hash，显式 policy_step_id→gate ToolCall.id→ToolResult/event.id→next iteration parents。先用CPU fixture与隔离backend检验投影，再执行任何30题推理。
6. 每task只运行一次。最长12个customer turn、24个policy decision、18个actual tool call；最多30×24=720次Qwen policy生成。超过预算记录truncated，不重试、不调prompt、不换模型。记录所有parse/native gate/backend拒绝与危险提议。
7. 当前完整APP的冻结response provider可能需要外部API；本轮未调用或验证该provider。下一轮执行前必须明确其冻结provider启动方式与API预算，不以fake回复替代后宣称端到端baseline。本地Qwen policy不使用API fallback。
8. 终态由权威DB快照与冻结scorer判定。把 infrastructure/evidence缺失隔离并保留reward null，不用猜测的成功标志评分。30个DEV episode不是正式benchmark或clean holdout结果。

## 单卡预算与存储

- 只用1×A100-PCIE-40GB；模型权重约15.26GiB，本次短CALL peak reserved见MEMORY_PROFILE，长history容量尚未测量。
- 在controller预注册输入token上限（建议8192，超限停止并记录，不静默truncate），以及运行级wall-clock预算后再启动；不改已冻结task/scorer。
- 以本次greedy latency/生成token测量估算，720个满1024tokendecision约需数小时；建议预留单卡8小时上限，实际耗时依prompt长度、candidate数量、PG/MCP和response provider而变。不能把smoke延迟当成30题实测。
- 权重/env/cache留NVMe；datasets/runtime/logs使用NAS。启动前重新df，当前NAS可用约44GiB；本轮不预占GPU、不训练、不生成checkpoint。
- controller构建/CPU与隔离服务检查完成后，在下一轮报告真实30题launcher命令、provider配置及所有source hashes，再执行30题。这里没有捏造不存在的launcher。

## Split

当前F1/F2/F3及其所有seed、step仅DEV，不能换seed变成clean holdout。H1–H4为未生成的未来异步Outbox、并发session、多明细协同和未知提交结果的transport recovery结构，必须在最终checkpoint/prompt/decoder/scorer锁定后才实例化。LEGACY_TAU_TEST只用于legacy evaluation；不读39个未见Retail train正文、不从历史APP-EVAL/S1题复制样例。不得根据本次DEV结果改clean family定义或读取正文调试。
