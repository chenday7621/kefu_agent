# 当前版本：PostgreSQL Outbox 与严格快照

验证日期：2026-10-05（Asia/Shanghai；证据文件名为 UTC 20261004）。本轮只做新增可靠性和必要业务回归，**模型/API 新调用 0**，没有收费对话、历史 benchmark、依赖升级或 commit/push。旧冒烟失败原始报告仍为 failed，未覆盖。

| 验收 | 实际结果 |
| --- | --- |
| 准备即关联 | 真实 HTTP MCP 返回资格检查结果前，操作及可信会话关联已在业务事务中保存；保存 native tool 事件前即能查询；不产生确认授权 |
| A/B 定位 | 同会话 A submitted 后准备 B 未确认；进程重启后追问返回 B 尚未提交，显式查询 A 返回原申请；重复后台处理 A 不改变当前指针 |
| 业务回滚 | 实际触发器拒绝 Outbox INSERT，申请/Outbox/数量占用均为 0；去掉故障后同一确认操作可提交 |
| 提交后中断 | MCP 事务提交后实际退出 77，通知保留 pending；重启应用，无任何用户追问即自动保存原会话回执 |
| 回执写入失败 | 实际数据库触发器连续拒绝回执 INSERT；offset 不增加、无残留事件；5 次后 failed，记录下次时间/错误/累计次数；重启后手动 retry 成功，总尝试 6 次 |
| 投递提交后中断 | offset/完整事件/delivered 已提交后 Parlant 进程实际退出 78；重启不重复插入，同 Outbox 仍只有 1 个回执 |
| 并发交错 | worker、两次直接处理、用户按需恢复和 MCP 同键重放交错；申请 1、占用 1、通知 1、canonical 回执 1；再次主动查询正常回复 |
| UI 通知 | 原生 HTTP 事件长轮询在回执提交前开始，观察到已提交的 outbox_id 事件；不触发模型/新业务操作 |
| Worker 协调 | 真实 native BackgroundTaskService 的明确标记离线任务已发 ready、仍待写状态时，worker 不投递/不扣重试次数；完成后自动投递 |
| 严格快照 | ready 出现后采样仍等待实际任务结束，最终状态完整保存；原事件/完整 session/current operation 重启后相同；只允许已声明且已 delivered 的具体 Outbox 尾部；任意 ready、原事件变动或状态变动均拒绝 |
| 原约束回归 | HTTP MCP 发现/参数传递、身份/明细/数量/资格、真实确认、同键参数冲突、8 同键及 8 异键并发、金额/不可变/过期约束通过 |
| 有效期补验 | 完整展示口令后，实际 HTTP 确认因过期返回 409；另一操作先真实确认再过期，MCP 返回 OPERATION_EXPIRED；两者申请/Outbox/占用均为 0 |
| 历史保护 | 与升级前完整 PG dump 比较原 248 条业务记录，无修改或缺失；001/002 hash 未变；历史申请重放不创建通知 |
| 最终启动 | PG 默认存储、原生 chat/services HTTP 200、内部上下文令牌不进入模型 schema；原浏览器申请仍为同一申请；9 条新验证通知全部 delivered，无积压 |
| 隔离审查 | 9,127 个历史文件 hash 不变；40 个公开文件无根目录密钥匹配；原失败报告 hash 留存、状态仍为 failed；语法及 Ruff E4/E7/E9/F 通过 |

## 可核对的故障与多操作样本

| 场景 | 会话 | operation_id | request_id / 结果 |
| --- | --- | --- | --- |
| MCP 提交后退出 | `WjeB1pO3zA` | `78cb350d-8e0f-44ed-9fa9-66a6b7d9a0ab` | `36d77e0f-c069-42bb-ad3b-52ca41927780` |
| 投递提交后退出 | `0SotrlrsTl` | `8e1ffd99-4829-4c51-a3b8-539743e9cd23` | `e2510ed8-a0a5-4e42-b58e-71d9bce5a391` |
| 同会话已提交 A | `X9Cp0QeOjj` | `2759ae60-c4e9-4cc7-8039-5754b787c667` | `fde0839c-4a46-436e-b2c0-2b32c296a142` |
| 后准备未确认 B | `X9Cp0QeOjj` | `be0de9f2-0931-45ca-95db-f50b66c9581a` | 尚未提交，没有 request |

提交回执金额均依据后端事实（这些夹具为 12900 分），明确“尚未退款”。通知 delivered 只表示事件已持久化，不等于用户已读。申请/数量/确认均保留原约束；通知失败重试不会重提申请。

## 命令与原始证据

迁移及停写备份见 [MIGRATION.md](MIGRATION.md)，worker 启停/积压/失败重试见 [README.md](README.md)。保持真实 PG 开启，先停止应用：

```bash
apps/retail_demo/.venv/bin/python -m apps.retail_demo.manage stop
apps/retail_demo/.venv/bin/python -m apps.retail_demo.tests.verify_outbox
apps/retail_demo/.venv/bin/python -m apps.retail_demo.tests.verify_snapshot
apps/retail_demo/.venv/bin/python -m apps.retail_demo.tests.verify_mcp
apps/retail_demo/.venv/bin/python -m apps.retail_demo.tests.verify_constraints
```

两份新增验收均通过；`verify_outbox` 实际启动 HTTP MCP 和禁用模型的 Parlant 进程做崩溃/重启/查询，`verify_snapshot` 验证真实 Store/BackgroundTaskService/worker 的采样与协调。offer/task 夹具明确标记，未冒充模型对话。故障触发器均已清理，单次进程退出钩子默认关闭，不接受模型参数控制。

```text
runtime-data/retail-demo/backups/20261004T154723.585728Z/
runtime-data/retail-demo/outbox/20261004T154724Z/source_hashes.json
runtime-data/retail-demo/outbox/20261004T154724Z/existing_business_audit.json
runtime-data/retail-demo/outbox/20261004T154724Z/constraint_regression.json
runtime-data/retail-demo/outbox/20261004T154724Z/expiry_followup.json
runtime-data/retail-demo/outbox/20261004T154724Z/final_startup_report.json
runtime-data/retail-demo/outbox/20261004T154724Z/final_isolation_audit.json
runtime-data/retail-demo/outbox/20261004T160138Z/outbox_report.json
runtime-data/retail-demo/outbox/20261004T160138Z/before_restart_snapshot.json
runtime-data/retail-demo/outbox/20261004T160138Z/after_restart_snapshot.json
runtime-data/retail-demo/outbox/20261004T160604Z_snapshot/snapshot_report.json
runtime-data/retail-demo/outbox/20261004T160604Z_snapshot/before_delivery_snapshot.json
runtime-data/retail-demo/outbox/20261004T160604Z_snapshot/after_delivery_snapshot.json
runtime-data/retail-demo/outbox/20261004T160604Z_snapshot/after_store_restart_snapshot.json
runtime-data/retail-demo/verification/20261004T160733Z/mcp_report.json
```

## 当前边界

限定本机可信演示身份、一个 Parlant 实例；会话写请求等待该会话正在运行的处理结束，worker 随应用生命周期运行，不能独立启动第二投递进程。内部适配显式依赖本仓库 3.3.2 的 `_process_session` 和 BackgroundTaskService task tag，版本升级需重验。无需 LangGraph/外部队列/多实例调度。

本轮没有新自然语言端到端对话或浏览器点击自动化；原三轮失败报告保留，不能把离线固定回执验证称为新模型对话通过。没有验证生产认证、长期负载。已过期且未提交的操作仍不能重试业务。删除原会话/回执的管理员操作不会自动转移或重新创建通知。数据库完全不可用时数据库内重试计数无法更新，进程日志记录异常，pending 保留、恢复连接后继续；没有 local 回退。

所有运行证据/备份/日志/密钥被 Git 忽略。检查完成后恢复 PG 应用及 worker，停止本轮扩展。

---

# 上一阶段：PostgreSQL 会话与按原操作恢复

验证日期：2026-10-04（Asia/Shanghai）。当前实际会话、客户及上下文变量存储为 PostgreSQL，原 local 文件保留为备份。本节是本次可靠性验收；下方保留初版记录，不代表当前默认仍使用 local。

| 验收 | 实际结果 |
| --- | --- |
| 停写备份/迁移 | 5 个会话、189 个完整事件、1 个持久客户、0 个变量/值及 3 份版本 metadata，共 198 文档；ID、内容、每会话数量和顺序无差异 |
| 幂等导入 | 同一源跳过全部 198 文档；另在一个旧验证会话增加 metadata 后重复导入，更新被保留，没有被旧文件覆盖；清理验证字段后再次严格核对通过 |
| 原业务保护 | 与迁移前实际 pg_dump 逐字段比较原 121 条业务记录，没有修改或缺失；新增验证只添加独立合成记录 |
| 真实 Store 接口 | 24 个并发事件 offset 唯一且连续；完整 metadata、软删除、过滤、分页、客户 extra/标签、变量值与异步生命周期验证通过 |
| 真实确认 | actual customer 事件、会话/operation_id 关联与确认日志同事务；错误口令/无先前展示回滚事件及计数器；模型 custom/metadata confirmed 无法授权 |
| 待确认重启 | 重启后仍未授权，实际确认前不能提交；既有有效期与幂等/并发检查继续通过 |
| MCP 响应丢失 | 实际 MCP 进程在事务提交后、响应发送前退出 77；重启后查回同一申请，不因未知响应另发操作键 |
| 最终回复未保存 | 数据库触发器真实拒绝 native AI message INSERT；另验证实际 HTTP 回执写失败返回 503；重启后再次查询返回原申请 |
| 重复恢复 | 重复追问/恢复/重试保持每个故障样本申请 1 份、占用数量 1；已提交操作的口令过期不影响读取 |
| 归属与失败关闭 | 他人操作/会话拒绝；同一客户的不同会话不能通过恢复入口读取未关联操作；数据库不可用报错，没有 local 回退；第二 Parlant 实例被锁拒绝 |
| 明确回滚 | 当前 PG 状态导出至独立 local 只读目录，原会话可读取，HTTP 写入返回 503；原三份 local 备份未覆盖；随后恢复 PostgreSQL |
| 现有功能回归 | 真实 HTTP MCP 发现六个工具、参数解析、订单/申请越权、数量/资格、同键及异键并发、数据库金额/不可变约束通过 |
| 最终交互启动 | PostgreSQL 模式、LLM 正常入口、`/chat/` HTTP 200；全部 5 个原会话可访问，事件数仍为 0/1/22/39/127；原申请可查询，启动核对没有模型调用 |
| 隔离审查 | 9,127 个历史文件 SHA-256 无变化；32 个公开文件未发现根目录密钥；应用环境、日志、数据库备份及运行数据继续被 Git 忽略；语法和 Ruff E4/E7/E9/F 通过 |

离线可靠性和故障注入没有模型调用，没有执行历史 benchmark。真实 HTTP 恢复使用数据库固定回执，明确“申请提交，尚未退款”，不冒充模型新回答。

## 故障恢复的可核对 ID

| 故障 | 会话 | 原 operation_id | 同一 request_id |
| --- | --- | --- | --- |
| 提交后 MCP 退出 | `6NlzwV9KpV` | `fd473b06-cf62-4567-8d61-6f6e130888e3` | `0eda346c-7a2b-481b-84ad-b27c1bc983bb` |
| native 最终 AI INSERT 拒绝 | `TRKTzRVoyU` | `efb1b913-7e14-4d84-9022-d83759e45ffb` | `c8d2e679-4da1-46bc-8181-e00bf28bb30d` |
| HTTP 恢复回执 INSERT 拒绝 | `718gq7tAp3` | `eb1a4e8a-d27e-4d40-91fb-69163890c8ab` | `d3054b9b-d09c-49fd-8a3e-0d7147d7376f` |

前两种故障发生在重启之前，不是对正常保存的回复做普通重启。第三种记录了实际 HTTP 503；恢复后两份固定回执都指向同一申请。各故障样本金额 12900 分、状态 submitted，重复恢复没有新增申请或占用。

## 唯一一组真实模型冒烟及核对异常

本轮只执行一组真实 Parlant/DeepSeek 三轮对话，没有自动重跑。订单查询、展示申请/原口令、实际客户确认、MCP 提交、最终回复编号与后端一致均完成：

- 会话 `A5mWUiW0mw`，原操作 `ac6afe8e-3e06-465f-a353-e7f226565784`。
- 申请 `5961b43c-46c9-4143-aec8-7c8ca7c7c7d1`，12900 分、submitted；确认事件 `9WM7TDq3nh` 与操作/request_id 关联一致。
- 21 次 API 客户端调用：输入 168716、输出 15285、cache hit 103296、miss 65420；各轮约 9.65、10.63、9.90 秒。SDK 内部 HTTP 重试不另计，不等同三次 API 请求。

原冒烟脚本的 `restored == transcript` 断言失败，因此**原始完整脚本结果仍为 failed，未改写为 passed**。该脚本在回答出现时就截取快照且覆盖了重启前文件；保存的重启后记录含两个 ready 状态，但原快照未保存，无法完整还原那次差异原因。已修正脚本：等待对应生成 trace 的 ready，分别保存重启前后快照，允许停机期间追加的 ready 状态，同时逐项校验原事件前缀。

没有追加模型调用；用同一真实已保存会话做离线补验，31 个事件在两次进程重启后完全一致，三个原客户事件 ID/内容及三份 AI 答案逐字一致，原确认事件和申请关联正确。补验报告通过；这与原脚本失败记录分开保留。

## 命令与原始证据

迁移/切换/回滚命令见 [MIGRATION.md](MIGRATION.md)，启动和恢复入口见 [README.md](README.md)。数据库启动后、MCP/Parlant 停止时运行：

```bash
apps/retail_demo/.venv/bin/python -m apps.retail_demo.tests.verify_reliability
apps/retail_demo/.venv/bin/python -m apps.retail_demo.tests.verify_http_reply_fault
apps/retail_demo/.venv/bin/python -m apps.retail_demo.tests.verify_mcp
apps/retail_demo/.venv/bin/python -m apps.retail_demo.tests.verify_constraints
```

本地证据均 Git 忽略：

```text
runtime-data/retail-demo/backups/20261004T141707.444523Z/manifest.json
runtime-data/retail-demo/backups/20261004T141707.444523Z/verification_initial.json
runtime-data/retail-demo/reliability/repeat_import_after_update.log
runtime-data/retail-demo/reliability/existing_business_audit.json
runtime-data/retail-demo/reliability/20261004T143221Z/reliability_report.json
runtime-data/retail-demo/reliability/20261004T144133Z/http_reply_fault_report.json
runtime-data/retail-demo/reliability/rollback_check/rollback_report.json
runtime-data/retail-demo/verification/20261004T143902Z/mcp_report.json
runtime-data/retail-demo/verification/20261004T145150Z/dialogue_report.json
runtime-data/retail-demo/reliability/dialogue_followup/dialogue_persistence_followup.json
runtime-data/retail-demo/reliability/final_startup_report.json
runtime-data/retail-demo/reliability/final_isolation_audit.json
```

## 当前限制

限定本机可信演示身份、单 Parlant 实例；按需恢复最后关联操作或显式指定该会话 operation_id，不主动补发，不使用 Outbox/队列/多实例调度。已确认但未提交且已过期仍不能重试提交。明确 local 回滚只读，业务 PG 仍须可用。没有验证生产多用户登录、长期压力负载或浏览器点击自动化。

首次冒烟重启断言差异的完整原始原因无法追溯，这是记录限制；当前持久化内容和两种实际中断恢复另有通过的独立离线证据。未修改冻结评测资产，未 commit/push。

---

# 初版演示验证记录

验证日期：2026-10-04。本应用使用合成 `DEMO-*` / `VERIFY-*` 订单、真实 PostgreSQL 16 容器、实际 FastMCP HTTP Server 和仓库 Parlant 3.3.2。没有执行历史 benchmark，没有使用 mock 或其他数据库。

| 验证 | 实际结果 |
| --- | --- |
| 原生 Parlant MCPToolClient | 五个工具发现、integer schema、参数和结构化返回解析通过 |
| 归属与输入校验 | 他人订单/申请、错明细、非法数量、不可退/未送达/过期订单均拒绝；无错误申请/数量写入 |
| 人工确认门槛 | 未确认操作不允许创建；错误口令、过期操作拒绝 |
| 幂等与并发 | 同键同参数返回同一申请；同键不同参数拒绝；8 个同键并发返回同一申请；8 个不同键争抢同一明细仅 1 个成功，不超量 |
| 数据库约束 | 直接 SQL 未确认/金额错误插入被拒绝，事务回滚未增加占用数量；已提交申请更新/删除被拒绝 |
| 数据库/MCP 重启 | 实际重启 Compose db 容器和 MCP 进程，同一申请及三条操作日志完全一致 |
| 重复初始化 | 重复迁移和 seed 两遍，已有申请、日志和已申请数量没有覆盖或清空 |
| Parlant 原生 local 存储 | 重启后同一 Agent/客户/会话/人工备注可访问，客户不重复；模型调用被禁用且为 0 |
| 真实三轮对话 | 官方 DeepSeek + Parlant 引擎：查订单 → 展示申请/口令 → 用户确认 → MCP 实际提交申请通过 |
| 真实对话恢复 | 重启 Parlant 后，三轮真实客户与 AI 消息及工具事件完全一致 |
| 后台启动入口 | `manage start/status/stop` 实测通过；启动后可访问原三轮会话，启动过程没有追加模型调用 |
| 原生聊天 UI | `/chat/` HTTP 200；未自动化验证浏览器逐次点击 |

## 可核对的持久化证据

- MCP/数据库重启：申请 `4c9b1d51-bd76-4068-b3a6-2b2f55ab43a7`；日志顺序 `prepared → confirmed → submitted`。重启前后数据库统计均为申请 7 份、操作记录 50 条、申请占用数量合计 9。这些统计包括独立验证样本，不是 benchmark 任务数。
- 离线 Parlant 恢复：会话 `jIsAeevtaC`，客户 `demo-alice`，Agent `retail-persistent-demo`，重启后事件完全相同，无重复客户。
- 真实对话：会话 `W4NtdRHxyU`，订单 `VERIFY-AC652CEDF162`，操作 `066981a8-5a65-483e-a050-4e563accb4e3`，申请 `20191d3d-3f16-47df-bf70-0f85f82a7ab7`。
- 最后申请金额 **12900 分**、状态 **submitted**；确认日志来源为 `parlant-human-message:W4NtdRHxyU`。确认前没有申请，最终回复包含数据库同一申请 ID，并明确未执行退款。

本地原始证据（不进入 Git）：

```text
runtime-data/retail-demo/verification/20261004T120431Z/mcp_report.json
runtime-data/retail-demo/verification/20261004T120431Z/mcp_calls.json
runtime-data/retail-demo/constraints_verification.json
runtime-data/retail-demo/verification/20261004T120000Z/parlant_persistence_report.json
runtime-data/retail-demo/verification/20261004T120650Z/dialogue_report.json
runtime-data/retail-demo/verification/20261004T120650Z/dialogue_events.json
runtime-data/retail-demo/verification/20261004T120650Z/api_calls.json
```

## 对话冒烟用量与首次失败

首次尝试的资格检查因 Parlant 整数参数实际为字符串 `"1"`，被 MCP 严格整数校验拒绝；没有创建申请。这是实际集成失败，记录保留，没有算作成功。修复新应用边界的规范整数转换，离线 MCP 验证后，仅恢复一次三轮对话；未修改上游 SDK、未进行提示优化循环。

| 尝试 | 客户轮次 | API 客户端调用 | 输入 token | 输出 token | cache hit | cache miss |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 首次接口失败 | 2 | 12 | 85,433 | 9,389 | 28,544 | 56,889 |
| 修复后的三轮冒烟 | 3 | 18 | 139,522 | 13,421 | 89,088 | 50,434 |
| 合计 | 5 | 30 | 224,955 | 22,810 | 117,632 | 107,323 |

三轮成功回答分别耗时约 9.06、10.08、9.12 秒。API 客户端调用来自实际 DeepSeek `response.usage`，不是目标回答数量，也未声称统计了 SDK 内部每次 HTTP 重试。没有查询余额或另跑收费预检，未进行 LLM 评分；金额账单未核对，成本金额 unavailable。

## 范围与未验证项

这是一次功能冒烟，不是成功率/事实准确率测量。没有测试生产多用户认证、真实退款、审批、物流、长期压力负载或浏览器端自动化；首版每条订单明细只有一份待处理申请，没有申请撤销/完成流程。自然语言路径已实际完成，不能将离线 MCP 验证代替它。

逐一校验历史源文件/数据/结果的 **9,127 个文件 SHA-256**，无修改或缺失；新增公开文件没有检测到根目录密钥，应用 `.env`、虚拟环境、运行数据、日志和权重均被 Git 忽略。隔离报告位于 `runtime-data/retail-demo/isolation_audit.json`。新代码语法与 Ruff E4/E7/E9/F 检查通过。

历史 Retail/RAG 冻结源文件与结果只读保留；本应用的开发、数据、会话和验证结果独立。未 commit/push。

## APP-S1 有限回归（2026-10-07，独立测试环境）

新增已确认快照提交MCP工具和005迁移；旧001–004、V1原分数/失败/待复核及本文件上述记录原样保留。APP-S1只针对新工具、替代确认及相关Outbox/恢复路径，不重跑历史benchmark或完整V140题。

- 无模型：14项主边界+4项授权/实时事实检查通过；真实PostgreSQL/HTTP MCP/native Store。实际旧完整口令（未确认/已确认旧草稿）409拒绝；新草稿仍需新确认；八路并发唯一、真实MCP提交后exit77响应丢失/重启/四路原操作重试唯一，原生严格快照与新工具trace/后续问答边界通过。测试offer标fixture，不当自然语言。两次证据序列化、一次夹具签名错误保留原失败目录；未调用模型的脚本修正不改业务分数。
- 自然语言：计划原8合法场景×2=16，开始15、完整脚本14；预算中断r2_R1、未运行r2_R6。冻结V1评分经最小只读工具观测适配得到13 pass / 2 fail / 1 not_run；主计划分母13/16，轮次8/8和5/8。14份目标申请/占用/Outbox/提交回执唯一，正式参数改写冲突0，实际错误写入0。
- r2_R4 的请求正确提交，原正则在否定句“不代表已退款”中匹配“已退款”，自动fail不改，文字语义人工标记unreviewed。r2_R1真正确认保留但无申请，未自动恢复或绕过Agent提交。r2_R6未运行，不能宣称所有三个旧失败已通过新对话。
- 预算：257个SDKdispatch，252完整response/usage、5中断usage未知；已知保守参考1.587178824＋未知预留0.3673098=USD1.954488624，下一预留越USD2即停止，不加预算。Flash已知离峰参考0.203064018，总额/别名实际费率/账单unavailable。
- 版本：本轮冻结60个源码/迁移/评分/SDK路径及有效Agent/Guideline元数据；原评分源码SHA保持，B3正则/B2标签未调整。新增迁移仅测试库已应用；原演示库/进程/worker保持升级前状态。最新浏览器完整交互未验证，不把离线或有限8场景称为生产端到端认证。

证据：`results/app_s1_confirmed_snapshot_20261006_165858/{offline,offline_live_checks,tasks,calls,mcp_calls}`；`PAID_FREEZE.json`、`APP_S1_MINIMAL.diff`、`S1_CLOSEOUT_OBSERVATIONS.json`、预算中断只读原操作核查。最终报告/隔离清单/脱敏包：`_reviews/20261006_165858_APP_S1_CONFIRMED_SNAPSHOT/`。复现与停写迁移见 [APP_S1.md](APP_S1.md) / [eval_s1/README.md](eval_s1/README.md) / [MIGRATION.md](MIGRATION.md)。

## APP-S1 收尾离线补充（2026-10-07，独立评分版本）

原报告、旧fail、B2待复核及B3金额归属记录不改写。55份V1/S1旧评分精确重现并同版复核，40个退款语境文本用例通过；仅S1 r2_R4由否定句误报fail改为新评分pass。原完整执行14（第二轮6），新版完整子集14 pass，计划口径14/16；r2_R1仍预算中断，r2_R6未运行。新模型调用0，演示库/服务/worker未变；没有最新浏览器完整交互验证或人工事实标签。新证据：`_reviews/20261007_084043_APP_S1_COMPLETION/`，当前WAITING_BUDGET。见 [APP_S1_COMPLETION.md](APP_S1_COMPLETION.md)。

## APP-S1 补齐完成（2026-10-07，授权后）

本轮追加保守USD1、累计USD3，仅r2_R1恢复及r2_R6首次执行，两者旧/新评分均pass。原14结果不重跑，原16逻辑单元：旧评分15/16，新退款语境评分16/16；原冻结13/16及中断fail保留。这是历史回归与跨日恢复，不是同期A/B、最新40题整体或生产准确率。

r2_R1在原会话核实过期未提交后真实聊天重新展示/确认，新3条消息、旧2条不重放；原18事件不变，新增25尾部逐项记录，旧操作仅按已有MCP替代规则设置superseded_by关联新操作。r2_R6用独立app_s1_completion_r6数据库及原夹具/seed43，原恢复数据库保留。新增35次SDK调用都有usage，已知保守USD0.246048528；累计已知1.833227352＋旧5未知预留0.3673098＝USD2.200537152，估算不是账单。

16份目标申请/快照参数/真实确认/数量/Outbox/唯一回执检查通过，正式改参冲突和错误写入/重复发现0；无正常重跑、额外基础设施恢复0。实际Agent/Guideline元数据及生成配置与冻结一致。演示库/进程未迁移或重启，测试进程和PG已停止、卷和证据保留。建议仅受控部署本机演示，需另行确认；最新浏览器完整交互及自由文本人工事实评估未验证。完整报告：`_reviews/20261007_084043_APP_S1_COMPLETION/REPORT.md`。
