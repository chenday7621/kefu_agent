# PostgreSQL 会话迁移、切换与回滚

本迁移只新增应用的 native Store、操作关联与 Outbox；不清空、不覆盖订单、明细、确认记录或退货申请。已应用 `001_initial.sql` / `002_parlant_storage.sql` 的 hash 保持不变；本阶段新增 `003_outbox.sql` / `004_outbox_control.sql`。Parlant/DeepSeek/依赖版本不升级，没有 MongoDB。

## 已使用 PostgreSQL 的实例升级 Outbox

在项目根目录停写备份后应用新迁移，不重新导入旧 local 文件：

`manage stop` 只管理其启动的进程；手动前台启动的 MCP/Parlant 也须全部停止，停止浏览器/脚本写请求，数据库保持开启。`outbox pause` 不能代替停写；它不阻止 API 或按需恢复写入。备份/切换工具检查应用端口及数据库实例锁，发现仍在运行时拒绝继续。

```bash
apps/retail_demo/.venv/bin/python -m apps.retail_demo.manage stop
apps/retail_demo/.venv/bin/python -m apps.retail_demo.storage_migration backup
apps/retail_demo/.venv/bin/python -m apps.retail_demo.db init
apps/retail_demo/.venv/bin/python -m apps.retail_demo.outbox status
apps/retail_demo/.venv/bin/python -m apps.retail_demo.manage start
```

本轮实际升级前备份 `runtime-data/retail-demo/backups/20261004T154723.585728Z/`，含完整业务/会话 PG dump。`003` 为现有会话初始化当前操作指针，依据原关联 offset 选择当前项；不创建任何历史通知。未来准备关联的单调 sequence 决定新指针，确认、查询及后台回执不改变指针。

新增 `tool_session_contexts` 由应用的可信 native ToolContext 签发随机能力令牌，原生 HTTP MCP 仍负责真实参数传递/返回。准备操作、会话关联及准备结果在同一业务事务保存，令牌重放返回相同准备；模型可见 schema 隐藏令牌，不能凭任意 customer/session 字段授权。独立 MCP 管理员无令牌的资格检查仍只准备，不能直接赋予会话确认。

`return_outbox` 的申请 INSERT 触发器只对新的、关联真实确认及存活会话的申请入队，原关联和待通知 INSERT 同事务。无会话的旧 operator-only 验证夹具不通知。唯一通知键、事件 outbox_id 索引和投递行锁保证幂等；迁移不删除或回填旧回执。`004` 增加持久化暂停控制、累计次数及错误历史表。

对话 UI 使用既有事件长轮询，worker 不另开端口或模型连接。暂停/查看/失败重试命令见 [README.md](README.md)。旧 local 只读回滚不启动 worker，恢复 PG 后继续 pending；不要回滚新业务申请或删除 Outbox 表来切换代码。快照必须等待实际原生 task，保存完整会话状态；本轮故障和采样记录见 [VERIFICATION.md](VERIFICATION.md)。

## 数据结构与接口

[postgres_documents.py](postgres_documents.py) 实现 Parlant `DocumentDatabase/DocumentCollection` 扩展接口，复用其原生 `SessionDocumentStore`、`CustomerDocumentStore`、`ContextVariableDocumentStore` 的序列化、metadata、标签、变量和 API 语义。[postgres_stores.py](postgres_stores.py) 扩展事件追加：会话计数器更新、完整事件 INSERT、真实确认事件关联及业务确认在同一事务提交。

- `parlant_sessions`：完整 native 会话 JSONB，含 agent_states、consumption_offsets、metadata、labels；客户/Agent/时间生成列及索引。
- `parlant_events`：完整 native 用户/AI 消息、工具结果、状态、自定义事件、trace、metadata、deleted；会话外键、唯一 `(session_id,event_offset)`、种类/trace/可见事件索引。按 offset 返回顺序，时间相同或乱序也不会改变事件顺序。
- `parlant_customers` / `parlant_customer_tags`：完整客户资料、extra、标签。
- `parlant_variables` / `parlant_variable_values` / `parlant_variable_tags`：完整上下文变量、客户/全局 key 的值和标签；变量/key 唯一索引。
- 各 Store 独立版本 metadata 表；不支持的文档版本报错，不默默丢弃。SDK 自带的合法标签文档转换会一次性持久化。
- `session_operations`：会话、原 operation_id、实际 customer 确认事件 ID/完整快照、原 request_id。确认快照保留，原生删除事件/会话不会删除业务申请或操作审计。
- `parlant_imported_documents`：已导入 ID/源文档 hash，重复导入不覆盖之后的 PostgreSQL 更新或原生删除。

原始 local 三份文件不再写入。其他本地文件仅是模型缓存/运行日志，不是会话旁路同步存档。数据库异常直接报错，没有 local 自动降级。

## 仅首次从 local 切换：停写备份与导入

本节是首次迁移操作，不是已经使用 PostgreSQL 的实例升级步骤。当前实例不要执行 `import-local` / 旧备份 `switch`，按上方 PG 升级流程执行；原 local 三份文件只保留作备份。

在项目根目录，数据库保持运行。停止全部本应用进程；若手动启动过，也先停止对应前台进程。命令会检查端口和数据库单实例锁，写入仍在进行时拒绝迁移。

```bash
apps/retail_demo/.venv/bin/python -m apps.retail_demo.manage stop
apps/retail_demo/.venv/bin/python -m apps.retail_demo.storage_migration backup
```

备份命令输出 `runtime-data/retail-demo/backups/<timestamp>/`，包含旧 `sessions.json/customers.json/context_variables.json`、完整 `pg_dump -Fc`、文件 SHA-256 manifest 和业务表摘要。目录权限 700、数据文件权限 600，全部 Git 忽略。数据库备份供事故审查，不由脚本自动恢复业务表。

将刚输出的备份路径代入：

```bash
RET_DEMO_BACKUP=runtime-data/retail-demo/backups/<timestamp>
apps/retail_demo/.venv/bin/python -m apps.retail_demo.storage_migration import-local --backup "$RET_DEMO_BACKUP"
apps/retail_demo/.venv/bin/python -m apps.retail_demo.storage_migration verify --backup "$RET_DEMO_BACKUP"
apps/retail_demo/.venv/bin/python -m apps.retail_demo.storage_migration switch --backup "$RET_DEMO_BACKUP"
apps/retail_demo/.venv/bin/python -m apps.retail_demo.manage start
```

初次导入使用一个事务；原始 ID 和完整文档保留，事件 offset 不重编号，计数器取 max(offset)+1。相同 ID 不同内容的未导入文档会拒绝，不覆盖 PostgreSQL。初次核对逐文档内容、ID、每会话完整事件数量及顺序；验证通过后才切换。`verification_initial.json` 留存初次一致性证据。

首次迁移期间失败恢复可重复执行同一备份的 `import-local`；这是幂等机制，不是 PG 升级命令。导入 ledger 的源 hash 相同时跳过，保留之后的新事件、metadata 更新和原生删除；会报告迁移后差异但不会把它们还原。显式 `verify` 仍执行严格比较，因此运行一段时间后的正常变更可能被报告为差异，不能用旧备份覆盖它们。不同源文档复用已导入 ID 则拒绝，需人工审查。

历史确认关联仅在已有业务确认日志、该会话的实际 customer 消息及原参数一致时导入，不把历史 AI/custom metadata 中的 `confirmed` 字段提升为授权。

## 显式只读回滚

如果新存储实现需要排查，先停写，导出**当前** PostgreSQL 状态到新独立目录，不把旧快照覆盖到数据库，也不覆盖原 local 备份：

```bash
apps/retail_demo/.venv/bin/python -m apps.retail_demo.manage stop
apps/retail_demo/.venv/bin/python -m apps.retail_demo.storage_migration rollback
apps/retail_demo/.venv/bin/python -m apps.retail_demo.manage start
```

回滚文件位于 `runtime-data/retail-demo/rollback_exports/<timestamp>/`；SDK 明确选择该导出的 local Store。回滚模式 HTTP 写操作、MCP 准备/提交均拒绝，供浏览器查看会话和业务事实；不是自动故障转移。业务数据库仍须可用，业务表和申请不回滚。

恢复 PostgreSQL：

```bash
apps/retail_demo/.venv/bin/python -m apps.retail_demo.manage stop
apps/retail_demo/.venv/bin/python -m apps.retail_demo.storage_migration resume-postgres
apps/retail_demo/.venv/bin/python -m apps.retail_demo.manage start
```

模式选择位于 `runtime-data/retail-demo/parlant/storage_mode.json`（可通过 `DEMO_PARLANT_HOME` 改变目录）。若手动设置了 `DEMO_SESSION_STORAGE`，它优先于模式文件；切换命令发现冲突会拒绝，需先去掉该环境覆盖。不要通过修改 DB URL 指向一个空数据库来假装回滚。

重复 `db setup` 使用 insert-only seed，不重置已有退货申请、确认日志、数量占用或演示订单送达日期；演示订单日期过期是现有业务规则的真实结果，不靠重复初始化刷新。历史申请不自动创建 Outbox，历史幂等重放不补发旧通知。收尾校验值和升级内部接口注意事项见 [CLOSEOUT.md](CLOSEOUT.md)。

## 本次迁移证据

实际备份：`runtime-data/retail-demo/backups/20261004T141707.444523Z/`。

初始导入 5 个会话、189 个完整事件、1 个持久客户、0 个变量/值，以及各 Store metadata，共 198 个文档；初次核对无 ID/内容/顺序差异。SDK 的 virtual guest 仍按原生语义提供，不算一个落库客户。

可靠性验收命令及实际故障恢复结果见 [VERIFICATION.md](VERIFICATION.md)。需要恢复期间的完整数据库备份时，应先另行恢复到独立数据库审查；本工具不会清空当前业务数据。

## APP-S1 升级：005_confirmed_snapshot.sql

005新增持久化 `superseded_by`、不可变准备参数保护、旧口令/直接写入的替代校验。001–004文件未修改。迁移不会删除既有确认、申请或回执；不会自动判断旧草稿是否被替代，也不补发历史通知。重复初始化仍不重置申请、占用或演示订单日期。

模型及MCP提交入口为 `submit_confirmed_return(operation_id)`，其余业务参数读取后端已确认的不可变快照；内部 `session_token` 由可信服务端上下文注入，不对模型暴露。旧 `create_return_request` 全参数函数仅保留作内部兼容及严格冲突检查，执行相同授权校验，不注册为模型候选或MCP工具。

APP-S1评测期间演示库没有执行005，原MCP/Parlant/worker进程仍为升级前已加载版本；独立APP-S1测试库已应用005。该历史状态由下方2026-10-07受控部署记录更新。其他现有PG实例升级时，退出浏览器写入、等待实际原生处理完成并确认没有其他手工进程，再执行：

```bash
apps/retail_demo/.venv/bin/python -m apps.retail_demo.manage stop
apps/retail_demo/.venv/bin/python -m apps.retail_demo.storage_migration backup
apps/retail_demo/.venv/bin/python -m apps.retail_demo.db init
apps/retail_demo/.venv/bin/python -m apps.retail_demo.manage start
```

不能把pause当停写屏障；备份应在全部写进程停止后完成。当前PG实例不得 `import-local`。新增迁移不能撤销为旧数据库状态；原只读回滚命令仍是停写→storage_migration rollback→启动，只导出当前PG会话用于只读排查，不删除申请或恢复旧业务表。APP-S1冻结hash/最小diff与独立测试证据见 [APP_S1.md](APP_S1.md)。

### 原演示实例实际部署（2026-10-07）

005已在原 `retail_demo` 演示数据库应用，001–004校验值不变；正常模式MCP/Parlant/worker已完整重启，稳定身份和PG存储保留。后续 `db init` 验证既有迁移并跳过005，不能用 `db setup`、seed、旧local导入或删除卷代替升级。

停写过程先等待50个绑定客户会话的原生processing完成屏障，再停止本应用全部写进程，确认三个HTTP端口关闭和数据库实例锁可用；另三个其他客户历史会话完整保留。完整PG备份已验证文件校验值、归档目录及全部数据块可解码，备份和完整快照仅保存在Git忽略的私有运行目录。迁移前后全部业务行、确认及关联、占用、会话事件逐项核对；无历史替代/通知回填，原9条Outbox仍delivered，无新回执。delivered仍仅表示落库。

005为71个原操作增加 `superseded_by=NULL`；读取原生CustomerStore时，一条旧客户标签关联由SDK合法loader将文档版本0.1.0转换为0.2.0，ID、客户/标签关联及创建时间全部不变。此项单独核验，不将其他差异笼统忽略。升级期间观测读事务曾阻塞DDL，已取消该次迁移并核对整笔事务回滚后仅重试同一005；没有恢复旧备份覆盖数据库。

本轮未执行聊天或模型评价，最新浏览器流程待用户手动验收。审计 `_reviews/20261007_121408_APP_S1_DEMO_DEPLOYMENT/DEPLOYMENT.md` 记录实际命令、备份、校验、工具schema及剩余边界。需要排查时使用上方显式只读回滚流程，导出**当前**PG状态，不降级schema或直接启动不兼容旧代码；破坏性数据库恢复需另行授权。
