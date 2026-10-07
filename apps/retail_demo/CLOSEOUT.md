# Retail Demo 收尾记录（2026-10-05）

当前能力：本机固定客户的订单查询、真实确认后的退货申请、原操作查询恢复，以及 PostgreSQL 原生会话/客户/上下文变量存储与提交回执 Outbox。申请、关联和待通知同业务事务；回执事件、offset 和 delivered 同投递事务。金额来自订单事实，`submitted` 表示申请提交，尚未退款。

## 本轮小修与有限检查

- `outbox.py` 收紧固定回执定位：只看最新用户事件之后、本服务成功提交的工具结果，核对 operation/request/session；后续准备或失败提交不沿用旧成功。已 delivered 的旧轮次/trace 回执不能阻止新回复落库。
- `postgres_stores.py` 明确让 `query_response` 绕过自动提交回执替换。后台回执不改变当前操作指针；显式查询历史操作仍可答复。
- `coordination.py` 启动时核对已审计的 Parlant 3.3.2 内部接口，不兼容时直接报错。没有建立通用适配层。
- README 的配置复制改为文件不存在且不是符号链接时才执行；MIGRATION 区分当前 PG 升级与首次 local 导入，强调停写及历史数据不回填。
- 新增 `tests/test_closeout_boundaries.py`：3 项针对性检查通过，含实际 SDK 接口、5 个不兼容分支、真实 PostgreSQL 临时表的 12 个回执定位分支。临时表事务回滚，不提交新申请；另只读复核了既有 A/B 场景。
- 对修改源码执行 Ruff E4/E7/E9/F 与编译检查；核对 5 个 CLI 的 `--help` 和安全复制命令的 Bash 语法。没有重跑完整故障套件或历史 benchmark，没有模型调用。

应用已加载小修并恢复原运行状态：MCP/Parlant 运行，数据库 healthy，worker enabled；9 条通知均 delivered，无积压。订单/申请/操作/日志/原生事件/通知数量均与收尾前一致，应用配置及 API 调用日志 hash 未变。

本轮证据：`runtime-data/retail-demo/closeout/20261004T172252Z/` 的 `scope_bug_before.json`、`checks.json`、`commands_checked.json`、`final_audit.json`。旧报告和原始快照按 SHA-256 核对保留；[VERIFICATION.md](VERIFICATION.md) 未改写。

## 已有证据与未验证范围

| 范围 | 证据（项目根目录下，运行数据不入 Git） |
| --- | --- |
| 真实 PG/HTTP MCP、业务提交后退出、回执 INSERT 失败/重试、投递提交后退出、幂等恢复交错、A/B 定位 | `runtime-data/retail-demo/outbox/20261004T160138Z/outbox_report.json` |
| 原生处理完成屏障、严格原事件/状态比较、明确识别 Outbox 尾部 | `runtime-data/retail-demo/outbox/20261004T160604Z_snapshot/snapshot_report.json` |
| HTTP MCP、并发/幂等及业务约束 | `runtime-data/retail-demo/verification/20261004T160733Z/mcp_report.json` |
| 确认/过期及原业务数据未覆盖 | `runtime-data/retail-demo/outbox/20261004T154724Z/{constraint_regression,expiry_followup,existing_business_audit}.json` |

这些是此前的数据库/MCP/Outbox/快照验证，本轮复用证据，不能称为本轮全套重验。历史真实三轮对话曾完成提交，但 `runtime-data/retail-demo/verification/20261004T145150Z/dialogue_report.json` 原脚本仍为 **failed**：重启核对失败且原采样覆盖问题无法追溯精确差异。另有离线恢复验证，不把它改写成对话通过。**最新代码的自然语言对话和浏览器完整交互尚未重验**；本轮仅重启并读取服务状态，未发送对话。

## 运行与通知处理

所有命令在项目根目录执行：

```bash
apps/retail_demo/.venv/bin/python -m apps.retail_demo.manage start
apps/retail_demo/.venv/bin/python -m apps.retail_demo.manage status
apps/retail_demo/.venv/bin/python -m apps.retail_demo.outbox status
apps/retail_demo/.venv/bin/python -m apps.retail_demo.outbox pause
apps/retail_demo/.venv/bin/python -m apps.retail_demo.outbox resume
apps/retail_demo/.venv/bin/python -m apps.retail_demo.outbox retry --id <failed_outbox_uuid>
apps/retail_demo/.venv/bin/python -m apps.retail_demo.manage stop
```

worker 随 Parlant 启停，无独立守护进程。`pause` 只暂停后台投递，API/正常提交/按需恢复仍可写入，不是停写或快照屏障。严格快照等待实际原生处理结束；迁移备份须停止全部 MCP/Parlant（含手工进程）和浏览器/脚本写入，数据库保持开启。

`delivered` 仅表示回执已落库，不表示用户已读。每轮最多 5 次自动尝试，达到上限 failed；人工 retry 仅将失败通知重新排入队列并重置本轮次数，保留累计次数、last_error 和错误历史，不重提申请。数据库完全不可用期间无法持久化失败次数，pending 保留并在恢复后继续；不静默回退 local。

停写后 `storage_migration backup` → `db init` → `manage start` 用于当前 PG 升级。不要重新 `import-local` 或 switch 旧 local 备份。只读回滚：停写 → `storage_migration rollback` → 启动；回 PG：停写 → `storage_migration resume-postgres` → 启动。均用 `apps/retail_demo/.venv/bin/python -m apps.retail_demo.<模块>`，完整命令与备份限制见 [MIGRATION.md](MIGRATION.md)。不要删除数据库卷或恢复旧业务表覆盖现有申请。

仅新提交创建通知，历史申请及其幂等重放不补发。重复 setup/seed 不重置已有退货、确认、数量占用或演示订单日期；日期自然过期不会被初始化刷新。限制单实例、固定演示身份，无支付/物流/审批/多用户认证；本机管理 UI 不应直接公开。

## 版本与未来维护

当前 Git HEAD：`e121b45bb0e66709a33bc390ef483f717fdc5ccb`。工作区为 `M README.md`、`?? apps/`，应用仍未提交；根 README 原修改保留，本轮未改。未 commit/push。实现版本同时由 [CLOSEOUT_SHA256.txt](CLOSEOUT_SHA256.txt) 标识（应用源码、测试、配置样例、文档、全部迁移及相关 SDK 源文件；不含密钥/数据/本说明本身）。清单文件 SHA-256：`acd3ae798005659accc58a6940a8f2e2562222dc1eebff8b26d32da8a5f3d132`。在根目录执行：

```bash
sha256sum -c apps/retail_demo/CLOSEOUT_SHA256.txt
```

内部依赖及调用点：

- `parlant_app.py:348` 安装会话协调；`coordination.py:15` 包装 native `SessionModule._process_session(session)`，以同一会话锁协调 worker/HTTP；`coordination.py:48` 查询 `BackgroundTaskService._tasks` 的 `process-session({sid})`，快照等待实际 task 完成。不得用 ready 或已有回答替代。
- SDK `src/parlant/core/app_modules/sessions.py:420/428` 的 dispatch/private process 与 `src/parlant/core/background_tasks.py:32/88/124` 的 `_tasks`/restart/collect 是维护边界；collect 会替换字典，因此协调器每次读取服务当前字典，不缓存旧引用。
- `postgres_stores.py` 使用原生序列化/反序列化及数据库事务入口；`session_tools.py` 在应用实例内包装原生 MCP 客户端/registry，URL、schema 与可信上下文仍需按实际版本核查。启动仍有上游 `Tool.inputSchema` 弃用警告，当前可用；未来升级应核对 MCP v2 `input_schema`，本轮不修改 SDK。

未来升级须重新审计上述签名、task tag、结束/取消语义、存储序列化、MCP 参数与事件通知，执行相关离线检查后再单独安排真实对话/UI 验证。当前兼容检查只防明显接口漂移，不证明未来版本语义兼容；不要仅放宽版本断言。已应用 001–004 迁移保持原 hash，变更只能新增迁移。本轮不升级依赖、不改业务规则或冻结资产，交付后停止扩展。
