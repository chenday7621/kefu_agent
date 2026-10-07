# APP-S1：已确认参数快照提交

2026-10-07；新MCP工具 `submit_confirmed_return(operation_id)` 仅引用后端不可变快照，仍须真实用户确认与可信客户/会话绑定。旧全参数函数不再暴露为MCP工具。首次提交仍由Parlant调用MCP，确认入口仅记授权。

新增005迁移，001–004不改。相同会话/订单明细的数量或原因修改替代旧未提交草稿；跨明细修改显式使用replaces_operation_id；独立不同明细不因当前指针变化撤销，已提交A可查询或幂等返回。旧确认和历史申请保留，历史操作不自动回填。

本轮独立数据库app_s1，端口55434/8920/8921/8922。18项无模型PG/HTTP MCP/native Store检查通过（14项主检查+4项实时/授权检查），包括真实旧完整口令、并发唯一、MCP实际提交后exit77及重启恢复、原生快照与新工具trace回执。原始失败脚本记录保留。

原8个合法场景×2计划16次：开始15、完整脚本14、预算中断1（r2_R1）、未运行1（r2_R6）。原冻结评分13 pass / 2 fail / 1 not_run，计划分母13/16=81.25%，两轮8/8、5/8。不能替换APP-EVAL-V1原40题整体82.5%，也不是同期A/B或新留出测试。

14份真实申请与14份唯一提交回执，参数逐字一致、正式IDEMPOTENCY_CONFLICT=0、实际错误写入/重复发现=0。r1_R3 原失败已通过；r2_R4 落库正确，正则在否定句“不代表已退款”匹配“已退款”，原fail保留，定性复核/人工标签unreviewed；r2_R6缺测，不能宣称三个历史失败全部复测通过。

257个SDK dispatch：252返回usage、5在预算中断时仍in_flight，原记录保留、实际响应/成本未知，不重发。252个已知输入2,041,336/输出169,956、cache hit1,395,306/miss646,030。保守已知小计USD1.587178824，加5个未知预留USD0.3673098为USD1.954488624，下一预留超USD2，立即停止。Flash离峰已知参考小计USD0.203064018；总额和实际账单unavailable，不能将未知算0。

审计：`_reviews/20261006_165858_APP_S1_CONFIRMED_SNAPSHOT/REPORT.md`；结果：`results/app_s1_confirmed_snapshot_20261006_165858/`。source/迁移/评分/最小diff/有效元数据见PAID_FREEZE.json、protocol_source_hashes.json、APP_S1_MINIMAL.diff；15次原始会话/MCP/数据库差异/完成快照、5个未知调用及新离线故障证据完整保留。历史报告和评分源不改，既有未提交工作保留，不commit/push。自由文本无人工/LLM裁判，固定回执不是模型事实准确率。

运行与复现见 [eval_s1/README.md](eval_s1/README.md)，受控停写迁移见 [MIGRATION.md](MIGRATION.md)。原演示库及进程本轮没有升级，保持原运行状态；源码启动前须备份、db init、重启，不能直接在旧schema混用新代码。
