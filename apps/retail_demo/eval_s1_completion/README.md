# APP-S1 评测收尾（离线阶段）

此包不启动服务、不导入模型客户端、不发聊天请求，只复核已经落盘的 V1/S1 轨迹。原业务、Guideline、模型配置、旧评分和报告均不改。新增退款语境自动观察版本；不构成人工事实评估。

项目根目录运行：

```bash
apps/retail_demo/.venv/bin/python -m apps.retail_demo.eval_s1_completion.test_refund_v2 -v
apps/retail_demo/.venv/bin/python -m apps.retail_demo.eval_s1_completion.offline \
  --audit _reviews/20261007_084043_APP_S1_COMPLETION \
  --results results/app_s1_completion_20261007_084043
```

复核会验证旧评分逐项可重现，以及除退款观察外的全部字段未改变。独立结果包含全部计划单元、执行状态、旧/新评分、命中原文、证据路径及 hash。`interrupted_budget` 的原 fail 保留；`not_run` 评分为 unavailable。

授权前阶段为 `WAITING_BUDGET`。没有本轮数值授权，不启动任何可能调用模型的服务或预检。原 S1 全量 runner 不用于本次收尾；它会跳过已有 fail 文件，不能安全处理预算中断。不得删除旧 fail 或 STOP 文件解锁。r2_R1/r2_R6 的后续白名单入口及调用计量要在授权后冻结并定向执行，该离线阶段尚未执行；授权后的完成结果见文末。

仅允许两个逻辑单元，恢复链取时间顺序第一份完整结果，正常失败不重跑。旧 5 个未知调用及 USD0.3673098 预留保留，旧 guard 已知小计 USD1.587178824；不能用 Flash 参考小计重置预算。建议追加 USD1，累计上限 USD3；建议不是授权。

部署仍须单独批准。部署流程和本轮审计见 [APP_S1_COMPLETION.md](../APP_S1_COMPLETION.md)。

## 已授权完成

USD1补充授权见本轮审计BUDGET_AUTHORIZATION.json。仅白名单两单元执行完成，旧评分15/16、新版16/16，旧账本未知仍保留。runner已完成单元会拒绝重复执行；不得删除attempt解锁。独立summary模块可零模型重新汇总，具体命令见本轮REPORT.md。测试进程和容器已停止；部署仍等待单独批准。
