# APP-EVAL-V1 验证追加（2026-10-07）

独立测试库/端口/运行目录，40题已全部执行；合法退货13/16、正确拒绝8/8、原冻结自动整体33/40。后者含2条A/B金额归属歧义（原fail保留、另标needs_review）和2条旧口令范围needs_review；不是事实准确率。38题触发模型，2题走固定恢复路径。

18个新无模型故障样本执行18/18，通过18/18；自动恢复12/12，持续失败人工retry3/3，申请/占用/提交回执唯一性逐次核对。delivered仅代表落库。无原始failed记录被覆盖，无业务优化或commit/push。

详见 [本轮报告](../../_reviews/20261006_151637_APP_EVAL_V1/REPORT.md)、[评测入口](eval_v1/README.md) 与 results/app_eval_v1_20261006_151637/ 下的逐题/故障/调用/数据库/快照证据。原README/MIGRATION/VERIFICATION/CLOSEOUT与应用源码/迁移保留，版本hash见本轮报告。

API请求仍deepseek-chat，实际响应deepseek-flash；按返回模型官方价格推算约USD0.531017，保守预算估算USD4.111153 < USD5；账单/旧别名计费映射未核实。真人浏览器、多用户登录、生产负载、字面旧口令失效和广泛文本事实正确性尚未验证。
