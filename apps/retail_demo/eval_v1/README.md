# APP-EVAL-V1

仅测试当前固定身份、单实例应用。20 个预定场景 × 2 次真实 HTTP 对话（seed 42/43）与 6 类 × 3 次无模型故障分别统计，不是官方 benchmark、真人浏览器测试或基础设施变更的因果增益。

脚本不修改业务入口、Guideline、迁移或生成参数。独立 PostgreSQL 容器/卷、数据库 `app_eval_v1`、端口 55433/8910/8911/8912、独立 PARLANT_HOME。每题保存完整数据库/会话/工具证据后才重置测试业务夹具；原演示进程与 55432 数据库不重置。不得将评测脚本指向演示库。

真实任务只 POST 当前聊天入口；确认口令从实际可见回复解析并核对既定目标，不从数据库拼接。至多一次预定澄清/纠正，不重跑失败。B2 对旧口令作明确“不是确认”的负向引用，不发送与当前目标不符的旧授权。Q4 初始未确认历史、Q3 初始已存在申请明确属于业务夹具，不伪装成新的模型生成。无模型故障使用标记的 offer/确认夹具、真实 MCP/native Store/HTTP；这些结果不能代替自然语言任务。

使用应用独立 `.venv`，从项目根目录：

```bash
# 新评测先生成独立配置（输出实际 stamp；不执行模型）
apps/retail_demo/.venv/bin/python -m apps.retail_demo.eval_v1.init_run
# 将输出的 private.json 路径代入；续跑既有评测跳过 init_run/setup
export APP_EVAL_CONFIG=runtime-data/retail-demo/app_eval_v1/<stamp>/private.json
apps/retail_demo/.venv/bin/python -m apps.retail_demo.eval_v1.setup
apps/retail_demo/.venv/bin/python -m apps.retail_demo.eval_v1.runner preflight
apps/retail_demo/.venv/bin/python -m apps.retail_demo.eval_v1.runner run
apps/retail_demo/.venv/bin/python -m apps.retail_demo.eval_v1.faults
apps/retail_demo/.venv/bin/python -m apps.retail_demo.eval_v1.report
```

首次完成 setup 后再执行 `scorer_checks` 可获得正例及9类反例验证；官方费用页面若变化，必须在付费前重新审查本协议价格依据，不静默换模型或费率。

setup 仅用于新评测，不能对既有评测重复执行。续跑只执行 runner run/faults：已评分任务不再生成，遇到未评分的旧 attempt 停止等待记录审查；预算/连续 API 错误 STOP 不自动清除、不加预算。测试进程由 context manager 清理；完成后另停止独立测试容器，保留卷和全部证据。

预算默认 USD5。当前官方页面未公布旧 `deepseek-chat` 别名费率，使用页面最高峰时 Pro 价格作保守参考估算与预算停止线，并保留别名费率 unavailable；不是账单或未知费率的数学保证。每次调用预留 UTF-8 输入字节及原 max_tokens 的参考费用，未知失败用量保留 unknown 与预算预留，不伪造 0。不做收费预检，首个计划任务即 canary。

评分先冻结。pass 表示预声明的可判定业务验收项通过，自由文本事实正确性保持 unreviewed；无法可靠匹配的拒绝/状态文字 needs_review，不默认 pass。固定回执、query_response 与普通模型回复分开计数。模型调用按唯一客户端 invocation ID/实际 response ID 记录，SDK 内部物理 HTTP/重试不可见记 unavailable。

本轮首题因原生 JSON `metadata=null` 的只读读取兼容问题中断；保存原 attempt 后，仅使用已保存的完整第一轮回复继续剩余消息，恢复一次，没有重发已完成轮次。兼容投影在 reader.py，原评分源 SHA 保持冻结；原始 JSON不改写。`readonly_adapter_revision.json` 保存该修正。续跑已评分题不会重新生成。

本轮金额正则在A申请/B准备同回复中产生两条多实体归属标记；原自动 fail 保留，review_notes.json 另记 needs_review，不把它称为已证实金额错误，不人工改分。B2只测旧口令的非授权引用，字面旧授权有效性未测；R2/R3允许预设目标ID澄清，不作为纯自主无ID定位成功率。自然语言任务包含两题固定恢复入口，是否调用模型按实际记录逐题区分。
