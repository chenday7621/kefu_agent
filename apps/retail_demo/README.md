# 持久化订单与售后演示

独立应用：自建演示订单 → PostgreSQL 业务层 → FastMCP Streamable HTTP → Parlant 原生工具调用 → DeepSeek 回答。可查询订单、准备退货、明确确认后提交申请、查询申请及操作记录。

这是新应用，不使用 tau Retail/MTRAG 数据、冻结订单快照或评测 runner，不改变历史 O1-B/R1-B 配置和指标。本应用未测量 benchmark 成功率。

## 当前本机部署状态（2026-10-07）

已按单独部署授权将原演示实例升级为 **APP-S1 已确认参数快照提交**，数据库迁移为 **001–005**。MCP、Parlant及Outbox worker已正常启动，访问 **http://127.0.0.1:8810/chat/**，使用原 `retail-persistent-demo` / `demo-alice`；PostgreSQL继续使用原55432端口和持久卷，原运行目录未变。

实际服务API核对六条冻结Guideline及工具关联；模型提交入口为 `submit_confirmed_return(operation_id)`，内部 `session_token` 仅由可信上下文注入，旧全参数创建接口不再作为MCP/Agent候选。停写备份后仅执行 `db init`，未seed、导入评测数据或重导local。53个原会话、449个完整事件及全部旧业务记录保留；005只增加原操作的空替代字段，另有一条SDK客户标签文档版本转换，其余字段不变。历史申请未补发通知，worker无pending/failed积压。

部署只做HTTP、原生Store/MCP与数据库只读核验，**没有调用模型或新增退货申请**；最新真人浏览器完整退货流程仍待手动验收。已有APP-S1证据为8个合法场景×2，旧评分15/16、新版退款语境评分16/16，含跨日中断恢复；不是同期A/B、最新40题100%或生产准确率。旧评测记录不改写。完整部署审计：`_reviews/20261007_121408_APP_S1_DEMO_DEPLOYMENT/DEPLOYMENT.md`。停写及显式只读恢复见 [MIGRATION.md](MIGRATION.md)。

## 从零启动

在项目根目录执行，要求 Linux/WSL、可用 Docker Compose 和根目录项目 `.venv`。当前验证版本：Python 3.12.3、Parlant 3.3.2、FastMCP 4.0.10、MCP 2.2.0、PostgreSQL 16；沿用本项目 DeepSeek/CPU embedding 环境，不整体升级。

如果还没有项目环境，先按根 README 安装项目依赖；本应用的 bootstrap 复用其 SDK/模型库，只在独立虚拟环境增加 `psycopg[binary]==3.3.6`。

```bash
# 仅在配置路径不存在时创建；已有配置及符号链接不覆盖
if [ ! -e apps/retail_demo/.env ] && [ ! -L apps/retail_demo/.env ]; then
  cp apps/retail_demo/.env.example apps/retail_demo/.env
  chmod 600 apps/retail_demo/.env
fi
.venv/bin/python apps/retail_demo/bootstrap.py

# 首次启动前编辑应用 .env，将 DEMO_DB_PASSWORD 占位符替换为自己的本地密码；
# DSN使用dotenv插值读取该密码。根目录 .env 自行配置 DEEPSEEK_API_KEY。
docker compose --env-file apps/retail_demo/.env -f apps/retail_demo/compose.yaml up -d --wait
apps/retail_demo/.venv/bin/python -m apps.retail_demo.db setup
apps/retail_demo/.venv/bin/python -m apps.retail_demo.manage start
```

Parlant 需要根目录 `.env` 中的 `DEEPSEEK_API_KEY`（权限建议 `600`）。只读取此键，不读取 Git token。订单业务/MCP/数据库验证不需要模型密钥；实际对话会调用 DeepSeek 官方 `deepseek-chat` 并产生费用。

本地 embedding 使用 `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`，默认只读已有 `runtime-data/huggingface` 缓存，启动保持离线。fresh clone 没有权重时，需要先获取模型资产，例如：

```bash
HF_HOME="$PWD/runtime-data/huggingface" .venv/bin/python -c 'from transformers import AutoModel, AutoTokenizer; n="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"; AutoTokenizer.from_pretrained(n); AutoModel.from_pretrained(n, use_safetensors=True)'
```

或在应用 `.env` 中设置 `DEMO_HF_HOME` 指向已有 Hugging Face 缓存，或 `DEMO_EMBEDDING_MODEL` 指向上述模型的本地完整目录。这些是模型缓存，不是 benchmark 快照。本应用不新增或替换检索模型；这里的 encoder 仅供 Parlant 内部索引使用。

访问 **http://127.0.0.1:8810/chat/**，选择 Agent「持久化订单与退货演示客服」及客户 **demo-alice**，创建会话。稳定 Agent ID 为 `retail-persistent-demo`。当前接口不会接受 guest 或其他客户创建业务会话。

默认地址：数据库 `127.0.0.1:55432`、MCP `http://127.0.0.1:8811/mcp`、Parlant `127.0.0.1:8810`、SDK 内部工具服务 `8812`。变更端口需同步应用 `.env` 中数据库 DSN/Compose 端口；服务均绑定本机。

## 交互示例

1. 「查询我的订单 DEMO-1001，告诉我商品明细。」
2. 「我要退 DEMO-1001 的 DEMO-1001-HEADSET 一件，原因是尺寸不合适。请先检查并展示内容，等我确认。」
3. 阅读客服展示的商品、变体、数量、原因、金额，然后原样复制它给出的完整确认口令，例如：

```text
确认退货 <后端返回的operation_id> 订单DEMO-1001 明细DEMO-1001-HEADSET 数量1
```

4. 客服通过 MCP 提交后应返回真实申请 UUID 和 `submitted` 状态。「查询退货申请 <该UUID>」可查看同一申请。

普通「确认」不会解锁写入。真实用户确认事件、会话与 operation_id 关联、确认日志现在在同一数据库事务保存；应用/MCP 创建申请必须具有该关联，提交时同事务关联原 request_id。口令由后端生成、与订单/明细/数量及已准备的原因绑定，有效期 30 分钟；同一会话须先收到包含这些信息的客服回复。本机 HTTP 入口接收真实用户消息后才记录确认，模型不能通过 MCP 提供 `confirmed=true`。修改任何申请内容需要重新准备、重新确认。

## 演示规则与边界

- 身份是**服务端固定绑定的本机演示客户**，不是多用户登录。模型不传 `customer_id`；每个业务读写按服务配置检查订单/申请归属。Bob 的 `DEMO-2001` 用于越权拒绝检查。
- 仅已送达 30 天内、`returnable=true` 的明细可申请；每次只处理一条明细和其整数数量，原因不能为空；数量不能超过尚未申请的数量。
- 首版每条明细**最多一份待处理申请**。即使部分退货后仍有剩余数量，也不能再提交第二份；没有审批、撤销、完成、退款、物流功能。
- `DEMO-1001` 耳机/杯子可退，数字券不可退；`DEMO-1002` 未送达，`DEMO-1003` 超过窗口。日期在首次 seed 时生成，重复 seed 不刷新日期。
- 金额取订单的整数分价格 × 数量，展示使用 Decimal。`submitted` 只表示**退货申请已提交**，不表示退款到账或审批完成。
- 模型仍可能选择错误工具或答错；业务约束负责拒绝不合法写入，不能把一次对话冒烟称为客服准确率。
- 原生 Parlant UI/API 是本地调试界面，包含管理员操作能力。不要将此无登录演示直接开放为多用户生产服务。

## 持久化与业务实现

[迁移](migrations/001_initial.sql)建立客户、订单、明细、服务端操作键、申请、操作记录。迁移记录 SHA-256，已应用迁移若被改动会拒绝启动；扩展应增加新迁移。`db setup` 可重复执行，seed 只 `INSERT ... ON CONFLICT DO NOTHING`，不会清空或覆盖用户申请。

[business.py](business.py)是 MCP 和用户确认入口共享的业务层；每次操作使用 PostgreSQL 事务。操作键行锁、订单/明细行锁、申请唯一约束、数量约束及写入触发器共同避免并发重复和超量。相同键与参数返回同一申请；改用同键但改变参数返回 `IDEMPOTENCY_CONFLICT`。数据库拒绝未确认/金额不符的直接插入，申请在此首版不可更新或删除。

业务失败返回 `{ok:false,data:null,error:{code,message,details}}`；类型不符或未知参数属于 MCP 协议校验错误。成功返回 `{ok:true,data:...,error:null}`，不得将 `ok:false` 解读为成功。

数据库使用 Compose 命名卷 `kefu-retail-demo_retail_pgdata`。应用直接向 SDK `Server` 传入 PostgreSQL `SessionStore`、`CustomerStore`、`ContextVariableStore` 实例，作为实际存储；完整文档保留为 JSONB，会话/事件分表并建立查询索引。每会话使用数据库行锁计数器及 `(session_id,event_offset)` 唯一约束。Store 连接均为 psycopg 异步连接，由应用生命周期管理；业务层使用事务连接，在异步入口通过线程调用。数据库错误不会回退 local。原 `runtime-data/retail-demo/parlant/` 的三份 local 文件留作备份，不再同步写入。Agent/客户/会话 ID 保持稳定。

## 会话迁移与原操作恢复

仅首次从 local 切换的实例需要停写备份、幂等导入、核对，再切换；完整命令及只读回滚见 [MIGRATION.md](MIGRATION.md)。当前实例已经使用 PostgreSQL，后续升级只执行停写备份和新迁移，不能重新导入旧 local 数据。fresh clone 没有旧会话时，`db setup` 后默认使用 PostgreSQL，无须导入。

用户在原会话追问「刚才退货成功了吗」，入口按持久化关联查询原操作，用数据库事实生成固定回执，保存为带 `deterministic_operation_receipt` metadata 的原生 AI 消息，不再调用模型重新生成恢复答案。未收到申请 ID 也可恢复。一般订单/退货对话仍经 Parlant 引擎生成。

- 已 `submitted`：返回同一申请号、金额和状态，明确尚未退款；原确认口令过期不影响查询。
- 已确认但未提交：只在用户明确说「按原操作重试」时，取数据库原参数，通过原生 MCP 客户端重试原 `operation_id`；仍受有效期、幂等及业务约束控制。
- 未确认/未提交且过期：不授权提交。无法核实状态时保留原操作，提示正在核实，不自动准备新键。
- 准备时即在同一业务事务保存可信会话关联，**不代表已确认**。恢复按独立的当前准备操作指针定位；A 已提交后准备 B 未确认，追问会说明 B 尚未提交，显式查询 A 仍返回 A。后台回执和历史操作查询不改变这个指针。

本机辅助接口（均校验绑定客户及会话归属）：

```text
GET  /demo/sessions/{session_id}/operation-result
GET  /demo/sessions/{session_id}/operation-result?operation_id={uuid}
POST /demo/sessions/{session_id}/confirm   JSON: {"message":"完整确认口令"}
POST /demo/sessions/{session_id}/operations/{operation_id}/retry
POST /demo/sessions/{session_id}/snapshot
```

`GET` 和 MCP `get_operation_result` 只读；聊天追问会新增真实用户消息及数据库回执。`confirm` 仅持久化真实确认，不触发模型/提交；浏览器按原流程发送确认时仍触发 Parlant。`retry` 是用户明确发起的写操作，复用原参数。MCP 身份固定在服务端，不能自行传任意客户或会话来获得授权。

当前限定**单 Parlant 实例**，由数据库 advisory lock 拒绝第二实例。应用内 PostgreSQL Outbox worker 随 Parlant 启停；没有外部消息队列或多实例调度。

## 自动提交回执与 Outbox

新提交的申请、原 operation/request/session 关联和 `return_submitted` 待通知记录在同一业务事务提交；任一步失败全部回滚。`(operation_id,notification_type)` 唯一，同键重放不会重复入队。只处理启用后的新客服会话提交，不回填历史申请；历史幂等重放也不补发。旧回执保留。

正常提交后的原生消息、按需恢复和后台 worker 共用 [outbox.py](outbox.py) 的数据库固定回执。申请提交工具对应处理 trace 的最终消息使用同一回执事件，包含真实申请号、订单/明细、数量、金额及状态，明确尚未退款；后续模型流式更新不能改写它。用户再次主动查询仍可得到新的查询答复（标记 `query_response`），不产生第二份 Outbox 提交回执。

自动替换只匹配最新用户消息之后、本服务实际成功提交的工具结果及相同 operation/request/session；后续准备或失败提交不会沿用较早成功。已投递回执只在原处理轮次/trace 内复用，不能借旧事件阻止新轮次回复落盘。明确标记的 `query_response` 不进入自动替换路径。

worker 每秒检查到期通知。后台回执与当前 Parlant 处理/HTTP 写入共享会话锁，等待原生处理任务完成；不会触发新的处理任务、模型调用或业务提交。会话 offset、完整原生消息 INSERT、Outbox delivered 标记同事务提交，`metadata.outbox_id` 有唯一索引。原生 `PollingSessionListener`/UI 长轮询读取已提交事件；断线重连也能读取。**delivered 仅表示已持久化，不表示用户已读。**

失败记录错误、每轮/累计次数及下次时间；最多 5 次，退避 5/10/20/40 秒（上限 60 秒），到达上限变成 `failed`，需要人工明确重试。数据库暂不可用不会回退 local；进程重启会继续处理 pending。删除原会话导致无法投递时保留通知并记录失败，不迁移到其他会话。

```bash
# 应用启动/停止时自动启动/停止 worker
apps/retail_demo/.venv/bin/python -m apps.retail_demo.manage start
apps/retail_demo/.venv/bin/python -m apps.retail_demo.manage stop

# 查看积压/失败；暂停/恢复后台投递（暂停状态持久化）
apps/retail_demo/.venv/bin/python -m apps.retail_demo.outbox status
apps/retail_demo/.venv/bin/python -m apps.retail_demo.outbox pause
apps/retail_demo/.venv/bin/python -m apps.retail_demo.outbox resume

# 只允许 failed 通知重试；保留累计次数和错误日志，不重提申请
apps/retail_demo/.venv/bin/python -m apps.retail_demo.outbox retry --id <outbox_uuid>
```

`pause` 只暂停后台投递，worker 仍检查控制状态，不禁止正常请求或用户按需恢复写回执。**它不是停写命令，也不是快照屏障**；备份/迁移必须停止 MCP 和 Parlant，快照仍等待实际处理完成。没有独立 worker 进程可与第二 Parlant 实例同时启动。只读 local 回滚不启动 worker，pending 仍保留在 PG，恢复 PG 后继续。

## 严格快照

`POST /demo/sessions/{session_id}/snapshot` 等待该会话**实际原生处理任务结束**，在会话锁内用 repeatable-read 读取完整 native 会话状态、所有事件（含软删除）、offset、当前操作及 Outbox。不以回答出现或 ready 作为完成判据。超时不返回成功快照。

冒烟脚本分别保存 `before_restart_snapshot.json` / `after_restart_snapshot.json`，严格核对原事件内容/顺序、完整会话状态与当前操作。只允许逐项证明对应快照中 pending、后来 delivered 的唯一 Outbox 回执尾部；任意 ready 或其他差异都拒绝。采样期间应停止其他浏览器写入；原失败记录保留，本轮没有重跑付费对话。

## MCP 接口与 SDK 注意事项

| 工具 | 参数 | 行为 |
| --- | --- | --- |
| `list_my_orders` | 无 | 绑定客户的订单 |
| `get_order_details` | `order_id` | 所属订单与明细 |
| `check_return_eligibility` | `order_id,item_id,quantity,reason,replaces_operation_id?` | 准备操作键/确认内容；修改原草稿时指定替代引用，不提交 |
| `submit_confirmed_return` | `operation_id` | 读取已确认参数快照，事务写入/幂等重放 |
| `get_return_request` | `request_id` | 所属申请与操作记录 |
| `get_operation_result` | `operation_id` | 只读恢复原操作与真实申请，已提交结果可过期查询 |

通过 `ServiceRegistry.update_tool_service(kind='mcp')` 注册和原生 Guideline 的 `ToolId` 关联。此版 Parlant MCP 客户端会在根 URL 后添加 `/mcp`，注册使用 **http://127.0.0.1:8811**，不能带 `/mcp`。它不转发 `ToolContext.customer_id`，所以可信身份必须在业务服务器固定绑定。

实测 Parlant 工具推理会把数量作为字符串 `"1"` 传递。新应用 MCP 边界仅将规范十进制字符串转成整数，schema 仍为 integer；小数、布尔值、`"1.5"`、`"1e0"`、`"01"` 等均拒绝。没有修改上游 SDK/历史评测协议。

## 验证

以下保留升级前历史验证命令，它们调用旧全参数 MCP 工具，仅适用当时源码；不作为当前 APP-S1 的验证入口，不在当前版本运行。原报告原样保留：

```bash
apps/retail_demo/.venv/bin/python -m apps.retail_demo.manage stop
apps/retail_demo/.venv/bin/python -m apps.retail_demo.tests.verify_outbox
apps/retail_demo/.venv/bin/python -m apps.retail_demo.tests.verify_snapshot
apps/retail_demo/.venv/bin/python -m apps.retail_demo.tests.verify_mcp
apps/retail_demo/.venv/bin/python -m apps.retail_demo.tests.verify_constraints
```

历史检查覆盖实际 HTTP MCP/Parlant MCPToolClient、准备即关联、A/B 定位、真实故障退出、事务回滚、重试/并发、worker 协调、完整快照和数据库约束。`--disable-llm` 禁止模型生成；明确标记的人工 offer/task 夹具仅用于离线验证，不能替代自然语言对话。上一阶段 `verify_reliability` / `verify_http_reply_fault` 保留作为当时的恢复验证记录；其旧回执计数口径不是当前自动 Outbox 的验收套件。

以下同样为旧入口的历史三轮冒烟命令记录（当前新提交工具不使用此旧脚本）：

```bash
apps/retail_demo/.venv/bin/python -m apps.retail_demo.tests.smoke_dialogue
```

单轮最多 180 秒，总流程最多 720 秒；无自动重跑。脚本查询新合成订单、准备内容、复制实际客服口令确认，检查原生工具事件和真实申请，再以禁用模型模式重启、核对真实对话恢复。验证会增加独立 `VERIFY-*` 数据，不删除用户数据。历史验证及原脚本 failed 记录见 [VERIFICATION.md](VERIFICATION.md)；最新代码的自然语言对话和浏览器完整交互尚未重验，本轮不执行此付费命令。收尾小修、版本校验值和维护要点另见 [CLOSEOUT.md](CLOSEOUT.md)。

## 单独启动、观察与停止

也可在两个终端分别前台启动：

```bash
apps/retail_demo/.venv/bin/python -m apps.retail_demo.mcp_server
# 另一个终端，先等 MCP 启动
apps/retail_demo/.venv/bin/python -m apps.retail_demo.parlant_app
```

后台启动器只管理自身创建的进程，不停止其他容器/应用：

```bash
apps/retail_demo/.venv/bin/python -m apps.retail_demo.manage status
apps/retail_demo/.venv/bin/python -m apps.retail_demo.manage stop
docker compose --env-file apps/retail_demo/.env -f apps/retail_demo/compose.yaml stop db
```

`stop` 保留数据库与会话；下次 `up -d --wait` / `manage start` 恢复。不要用 `down -v`，该命令会删除数据库卷。

运行日志/验证证据位于 `runtime-data/retail-demo/`；模型调用记录为 Parlant 目录中的 `model_calls.jsonl` 与 API 客户端实际返回 usage 的 `api_calls.jsonl`。后者包括缓存 hit/miss，未知值为 null；SDK 内部 HTTP 重试不单独计数。环境文件、运行数据、日志、数据库、模型权重均被 Git 忽略。

## APP-S1：已确认快照提交（2026-10-07）

模型提交工具现在是 `submit_confirmed_return(operation_id)`。订单、明细、数量、原因及金额由后端已保存且数据库保护的不可变快照读取，模型不能再次填写。可信会话能力由应用原生 ToolContext 适配器注入，不进入模型参数。operation_id 只是引用；真实客户确认、归属、会话绑定、有效期及首次提交资格仍由后端校验。旧 `create_return_request` 仅保留为严格业务兼容/测试函数，不注册为 MCP 工具，不能以模型 confirmed 或辅助 stamp 绕过真实确认。

同会话同明细数量/原因变化会持久化替代旧未提交草稿；修改同一退货但更换订单/商品时，准备工具显式带 `replaces_operation_id`。旧完整口令及已确认旧草稿不能继续提交，新草稿须重新展示/确认。独立的不同明细操作不因当前指针变化而撤销；已提交A始终可查询或幂等返回。历史确认/申请不删除，也不自动回填替代关系。

新增迁移005，先停写备份再 `db init`，不要改001–004或重新导入旧local。**本次仅升级源码并在独立测试库验证，原演示进程/数据库保持原状态，仍加载升级前代码**；受控迁移与重启步骤见 [MIGRATION.md](MIGRATION.md)。实际有限回归及证据见 [APP_S1.md](APP_S1.md)；历史V1整体82.5%、原失败及待复核标签原样保留。

APP-S1 当前无模型验证入口（只指向新建的独立测试库，不停止原演示；创建运行后将打印路径代入配置）：

```bash
apps/retail_demo/.venv/bin/python -m apps.retail_demo.eval_s1.init_run
export APP_EVAL_CONFIG=runtime-data/retail-demo/app_s1/<stamp>/private.json
apps/retail_demo/.venv/bin/python -m apps.retail_demo.eval_s1.setup
apps/retail_demo/.venv/bin/python -m apps.retail_demo.eval_s1.scorer_checks
apps/retail_demo/.venv/bin/python -m apps.retail_demo.eval_s1.offline
apps/retail_demo/.venv/bin/python -m apps.retail_demo.eval_s1.offline_live_checks
apps/retail_demo/.venv/bin/python -m apps.retail_demo.eval_s1.runner preflight
```

当前原始故障记录和快照保存在独立results目录；测试进程通过生命周期清理。只跑无模型检查时最后执行 `docker stop app-s1-<stamp-with-hyphen>` 停止自己的测试容器，保留卷；准确名称见本地私密配置的container字段，不能停止演示库。付费有限回归需要preflight后freeze再runner run，完整命令见 [eval_s1/README.md](eval_s1/README.md)，不自动发起。

## APP-S1 评测收尾（2026-10-07）

零模型离线复核与预算等待见 [APP_S1_COMPLETION.md](APP_S1_COMPLETION.md)。原S1第二轮完整执行6次；原14份完整轨迹13 pass/1 fail，新退款语境评分14 pass。r2_R1预算中断、r2_R6未运行，旧分和报告保留；当前WAITING_BUDGET，未迁移或重启演示实例。

## APP-S1 补齐完成（2026-10-07，授权后）

本轮追加保守USD1、累计USD3，仅r2_R1恢复及r2_R6首次执行，两者旧/新评分均pass。原14结果不重跑，原16逻辑单元：旧评分15/16，新退款语境评分16/16；原冻结13/16及中断fail保留。这是历史回归与跨日恢复，不是同期A/B、最新40题整体或生产准确率。

r2_R1在原会话核实过期未提交后真实聊天重新展示/确认，新3条消息、旧2条不重放；原18事件不变，新增25尾部逐项记录，旧操作仅按已有MCP替代规则设置superseded_by关联新操作。r2_R6用独立app_s1_completion_r6数据库及原夹具/seed43，原恢复数据库保留。新增35次SDK调用都有usage，已知保守USD0.246048528；累计已知1.833227352＋旧5未知预留0.3673098＝USD2.200537152，估算不是账单。

16份目标申请/快照参数/真实确认/数量/Outbox/唯一回执检查通过，正式改参冲突和错误写入/重复发现0；无正常重跑、额外基础设施恢复0。实际Agent/Guideline元数据及生成配置与冻结一致。演示库/进程未迁移或重启，测试进程和PG已停止、卷和证据保留。建议仅受控部署本机演示，需另行确认；最新浏览器完整交互及自由文本人工事实评估未验证。完整报告：`_reviews/20261007_084043_APP_S1_COMPLETION/REPORT.md`。
