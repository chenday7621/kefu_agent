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
- [ ] GitHub 发布到用户指定的既有仓库 `chenday7621/kefu_agent` 的 `main`；发布后核对远程 SHA。
- [x] 没有执行 benchmark、DeepSeek 请求、模型推理、依赖安装或 Agent 修改。

## Git

- 本地分支：`release/agent-optimization`。
- 提交说明：`Finalize Parlant agent optimization project`。
- 发布内容 commit：`53f13acb6e55283d81150cb140f115571cce3e72`。
- GitHub 发布地址：[chenday7621/kefu_agent](https://github.com/chenday7621/kefu_agent)，目标分支 `main`。
- origin：`https://github.com/chenday7621/kefu_agent.git`；原 Parlant 来源保留为 upstream：`https://github.com/emcie-co/parlant.git`。
- 提交作者：若本地未配置身份，使用自动整理身份 `Codex <codex@localhost>`，不冒用上游作者。

## 文件范围与复现限制

公开保留上游 `src/`、许可证与项目配置，以及实验实现、展示 `configs/`、`docs/` 和最小 `requirements.txt`。`results/`、`_reviews/`、官方外部数据及权重不进入提交；既有 `REPRODUCE.md` 保留在本地，公开入口见 `docs/reproduce.md`。原实验的 Git/hash 校验和快照依赖未修改，因此 fresh clone 不能在没有本地资产与冻结文件时一键重放历史成绩。

内容 commit 的 SHA 通过后续仅修改本清单的文档提交记录，避免将自身 SHA 写入自身内容造成自引用。当前 HEAD 可用 `git rev-parse HEAD` 查看。

本地检查明细存于忽略的 `runtime-data/release-preparation/`，不作为公开审计包上传。

## 检查范围

本地展示提交包含 926 个公开文件：没有密钥、个人路径、完整 benchmark 数据、结果包、模型权重或本地缓存。展示分支及上游基点祖先已扫描；本地 `refs/codex/` 工具快照不属于发布分支，保留本地、不作为发布对象。只检查有限的凭据模式及本地密钥精确匹配，不将此描述为完整安全审计。

本次新增展示文件的 whitespace/link/JSON 检查通过。三个既有冻结 `metrics_utils.py` 文件末尾空行提示保留，不为格式整理修改实验代码。实验服务监听端口与运行进程检查均为空；没有启动服务。

## 发布认证

GitHub token 只读取根目录忽略的 `.env` 中的 `GIT_TOKEN`，通过本地临时 askpass 交给 Git，不嵌入 remote URL 或 Git 配置，不启用 credential 存储。只推送指定 `main` 分支，不推送标签或本地工具快照。
