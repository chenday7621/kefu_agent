# Final Release Checklist

- [x] 根目录 README 已整理为开源展示，保留上游归属与 Apache-2.0 许可。
- [x] Transaction / RAG 文档及复现入口已整理；案例来自既有开发轨迹。
- [x] 展示默认为 Retail **O1-B**、RAG **R1-B**；C1 / R2 仅作消融记录。
- [x] Retail 37.50% → 58.75%（+21.25pp）；MTRAG Cloud 自定义 HOLDOUT Recall@5 22.31% → 36.55%（+14.24pp）。
- [x] R2 未显示整体生成收益，人工标签保留 unreviewed，不称事实准确率。
- [x] 完整数据、结果、审计、权重、缓存及本地个人路径打包助手已加入忽略规则，磁盘原件保留。
- [x] 暂存文件与发布分支祖先的凭据/禁止资产扫描完成；本地三个凭据值（含 GIT_TOKEN）精确匹配检查均无命中。
- [x] 9,127 个原有源码、实验代码、数据和结果 SHA-256 全部保持不变。
- [x] 本地内容提交完成；后续仅补记本清单中的提交号。
- [x] GitHub 已发布到用户指定的既有仓库 `chenday7621/kefu_agent` 的 `main`，远程 SHA 已核对。
- [x] 没有执行 benchmark、DeepSeek 请求、模型推理、依赖安装或 Agent 修改。

## Git

- 发布分支：`main`，跟踪 `origin/main`；原本地历史分支 `release/agent-optimization` 保留。
- 提交说明：`Finalize Parlant agent optimization project`。
- GitHub 首次发布快照 commit：`020cf4cada6736b656f3182f45c3024d35f28146`。
- 原本地整理 commit：`53f13acb6e55283d81150cb140f115571cce3e72`（仅留本地历史分支）。
- GitHub 发布地址：[chenday7621/kefu_agent](https://github.com/chenday7621/kefu_agent)，目标分支 `main`。
- origin：`https://github.com/chenday7621/kefu_agent.git`；原 Parlant 来源保留为 upstream：`https://github.com/emcie-co/parlant.git`。
- 提交作者：若本地未配置身份，使用自动整理身份 `Codex <codex@localhost>`，不冒用上游作者。

## 文件范围与复现限制

公开保留上游 `src/`、许可证与项目配置，以及实验实现、展示 `configs/`、`docs/` 和最小 `requirements.txt`。`results/`、`_reviews/`、官方外部数据及权重不进入提交；既有 `REPRODUCE.md` 保留在本地，公开入口见 `docs/reproduce.md`。原实验的 Git/hash 校验和快照依赖未修改，因此 fresh clone 不能在没有本地资产与冻结文件时一键重放历史成绩。

首次发布快照的 SHA 通过后续仅修改本清单的文档提交记录，避免将自身 SHA 写入自身内容造成自引用。当前 HEAD 可用 `git rev-parse HEAD` 查看，并与 `git ls-remote origin refs/heads/main` 核对。

本地检查明细存于忽略的 `runtime-data/release-preparation/`，不作为公开审计包上传。

## 检查范围

本地展示提交的公开文件集合没有密钥、个人路径、完整 benchmark 数据、结果包、模型权重或本地缓存。展示分支及上游基点祖先已扫描；本地 `refs/codex/` 工具快照不属于发布分支，保留本地、不作为发布对象。只检查有限的凭据模式及本地密钥精确匹配，不将此描述为完整安全审计。

本次新增展示文件的 whitespace/link/JSON 检查通过。三个既有冻结 `metrics_utils.py` 文件末尾空行提示保留，不为格式整理修改实验代码。实验服务监听端口与运行进程检查均为空；没有启动服务。

## 发布认证

GitHub token 只读取根目录忽略的 `.env` 中的 `GIT_TOKEN`，通过本地临时 askpass 交给 Git，不嵌入 remote URL 或 Git 配置，不启用 credential 存储。只推送指定 `main` 分支，不推送标签或本地工具快照。

## 发布历史

原仓库是上游 tag 的浅克隆，首次带上游历史的 push 因缺少对象被 GitHub 拒收，未写入远程 main。随后使用与已检查内容完全相同的文件树建立独立发布根提交，正常推送到空仓库 main；没有 force push，也没有删除原本地历史、源码或实验资产。

## 发布 CI

首轮 Verify and Test 的三个上游测试步骤因 `error: no justfile found` 失败，上传本身已成功。自动 CI 改为 Python 3.12 离线发布校验，覆盖语法、配置、文档链接与私有资产排除；不执行模型测试或 benchmark，不需要模型密钥。该校验不能冒充完整上游 Parlant 测试通过。冻结 Agent/benchmark 代码保持不变。
