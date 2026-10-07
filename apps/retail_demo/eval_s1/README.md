# APP-S1 有限回归

仅评测原V1的8个合法退货场景×2；从原20题seed42/43顺序中取合法子序列，不重新选题，不重跑V1。自然语言首次提交必须是Parlant→HTTP MCP，脚本仅复制实际展示且目标匹配的口令。原评分源码保持，scoring.py只是新工具名/数据库快照的观测适配；原始数据不改。

独立PostgreSQL容器/卷、app_s1数据库、55434/8920/8921/8922端口及Parlant目录。预算USD2沿用V1最高峰Pro参考预留；请求deepseek-chat、别名账单费率未知，实际model和usage逐调用保存。正常失败不重跑，已评分任务续跑跳过。STOP不自动清除/加预算；未完成基础设施故障需审查已落盘事件，最多一次显式恢复，已提交先只读查询。

```bash
apps/retail_demo/.venv/bin/python -m apps.retail_demo.eval_s1.init_run
# 将输出配置路径代入；后续所有命令使用同一私密配置
export APP_EVAL_CONFIG=runtime-data/retail-demo/app_s1/<stamp>/private.json
apps/retail_demo/.venv/bin/python -m apps.retail_demo.eval_s1.setup
apps/retail_demo/.venv/bin/python -m apps.retail_demo.eval_s1.scorer_checks
apps/retail_demo/.venv/bin/python -m apps.retail_demo.eval_s1.offline
apps/retail_demo/.venv/bin/python -m apps.retail_demo.eval_s1.offline_live_checks
apps/retail_demo/.venv/bin/python -m apps.retail_demo.eval_s1.runner preflight
apps/retail_demo/.venv/bin/python -m apps.retail_demo.eval_s1.freeze
apps/retail_demo/.venv/bin/python -m apps.retail_demo.eval_s1.runner run
apps/retail_demo/.venv/bin/python -m apps.retail_demo.eval_s1.report
apps/retail_demo/.venv/bin/python -m apps.retail_demo.eval_s1.finalize
```

setup/离线重置仅用于新运行，不能用于续跑现有题。freeze仅首次付费前执行；元数据/源码/模型/评分器/计划固定。runner保存每题初末数据库、实际完成屏障快照、完整对话/原生工具事件、MCP参数及API请求响应。离线offer固定为fixture，不能冒充对话成功。finalize只停止自己的测试容器，保留卷、测试证据与原演示状态，生成脱敏tar.gz与SHA256。若原演示库期间被浏览器修改，隔离核查会报告差异，不覆盖用户数据。
