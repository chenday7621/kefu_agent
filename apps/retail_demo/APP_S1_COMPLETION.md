# APP-S1 收尾：评分修订与定向补齐

2026-10-07，授权前离线阶段为 **WAITING_BUDGET**，该阶段新模型调用 0。授权后的完成结果见文末及本轮最新REPORT。审计：`_reviews/20261007_084043_APP_S1_COMPLETION/`；独立结果：`results/app_s1_completion_20261007_084043/`。原 S1/V1 报告、评分及轨迹不改写。

原 S1 完整执行14：13 pass、1 fail；第二轮完整执行6，5 pass、1 fail。另有 r2_R1 预算中断（原评分 fail 保留）及 r2_R6 未运行。新评分只修退款语境观察：r2_R4 的“不代表已退款”不再触发 false_refund_claim。全部55份已有评分精确重现后复核，只有这一份 fail→pass；V1仍33 pass/5 fail/2 needs_review，合法子集13/16。S1新版计划口径14/16，完整执行子集14/14；不能冒称16次全完成。

引用、条件、疑问、双重否定等不明确语境保留 needs_review；混合句逐个命中，不因一个“不”豁免整段。40个离线文本用例通过。B3金额归属、B2待复核、业务目标与数据库判据保持原样。自由文本事实人工标签 unreviewed，固定提交回执不是模型事实准确率。复核及命令见 [eval_s1_completion/README.md](eval_s1_completion/README.md)。

未获本轮数值预算授权，不能启动可能调用模型的服务或预检。授权后仅 r2_R1/r2_R6，第一份完整结果即止，正常失败不重跑；额外基础设施恢复最多一次且同预算。r2_R1 要先读取实际持久状态，不能用旧报告替代当前核查：已提交只恢复原申请；未过期且已确认沿原操作续办；过期须真实聊天重新展示与确认，不能修改数据库时钟或从数据库补口令。不能清空原尝试状态从头冒充恢复。

## 部署建议与方案（本轮不执行）

授权前建议**暂缓部署 APP-S1 到演示实例**，先完成两个缺口的定向验证；即便补齐通过，也仅支持固定身份、单实例、本机演示，不构成生产认证。原演示进程仍加载升级前代码，最新浏览器完整交互尚未验证。005只在独立测试库验证，部署需要单独确认。

部署窗口在项目根目录执行；停写含浏览器/脚本及手工启动进程，`outbox pause`不是停写：

```bash
# 以下仅为待批准方案；本轮没有执行
apps/retail_demo/.venv/bin/python -m apps.retail_demo.manage stop
# 确认手工 MCP/Parlant 进程也停止，保持 PostgreSQL 开启
apps/retail_demo/.venv/bin/python -m apps.retail_demo.storage_migration backup
apps/retail_demo/.venv/bin/python -m apps.retail_demo.db init
apps/retail_demo/.venv/bin/python -m apps.retail_demo.manage start
apps/retail_demo/.venv/bin/python -m apps.retail_demo.manage status
apps/retail_demo/.venv/bin/python -m apps.retail_demo.outbox status
```

部署前保存001–004原迁移hash、当前客户/会话/确认/申请/占用/Outbox摘要及停写备份。`db init`应用005且核对既有迁移hash，不运行seed、不重导入local、不改历史申请。完整重启后用本仓库原生MCP客户端发现工具：模型可见 `submit_confirmed_return(operation_id)`，无 `create_return_request`；内部session_token仍由服务端注入。只读核对稳定Agent/客户/会话ID、旧确认/申请及占用和回执，与备份一致；worker enabled，历史申请不补发。需要自然语言部署冒烟或浏览器操作时另行授权，不自动追加。

只读回滚按 [MIGRATION.md](MIGRATION.md)：停写→`storage_migration rollback`→启动。导出当前PG到新目录供只读查看，不恢复旧业务表覆盖已有申请，也不是schema downgrade；恢复PG须再次停写、`storage_migration resume-postgres`、完整启动。单实例、固定演示身份、数据库错误不回退local仍适用。

## APP-S1 补齐完成（2026-10-07，授权后）

本轮追加保守USD1、累计USD3，仅r2_R1恢复及r2_R6首次执行，两者旧/新评分均pass。原14结果不重跑，原16逻辑单元：旧评分15/16，新退款语境评分16/16；原冻结13/16及中断fail保留。这是历史回归与跨日恢复，不是同期A/B、最新40题整体或生产准确率。

r2_R1在原会话核实过期未提交后真实聊天重新展示/确认，新3条消息、旧2条不重放；原18事件不变，新增25尾部逐项记录，旧操作仅按已有MCP替代规则设置superseded_by关联新操作。r2_R6用独立app_s1_completion_r6数据库及原夹具/seed43，原恢复数据库保留。新增35次SDK调用都有usage，已知保守USD0.246048528；累计已知1.833227352＋旧5未知预留0.3673098＝USD2.200537152，估算不是账单。

16份目标申请/快照参数/真实确认/数量/Outbox/唯一回执检查通过，正式改参冲突和错误写入/重复发现0；无正常重跑、额外基础设施恢复0。实际Agent/Guideline元数据及生成配置与冻结一致。演示库/进程未迁移或重启，测试进程和PG已停止、卷和证据保留。建议仅受控部署本机演示，需另行确认；最新浏览器完整交互及自由文本人工事实评估未验证。完整报告：`_reviews/20261007_084043_APP_S1_COMPLETION/REPORT.md`。
