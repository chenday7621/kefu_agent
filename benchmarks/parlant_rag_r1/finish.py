"""Run remaining fixed stages once after encoding, gate paid answers, deliver, stop."""
from common_r1 import *
import subprocess,sys

def step(script,*args):
    log=RESULT/(script.upper().replace('.PY','')+'.log')
    with log.open('a') as f:
        subprocess.run([str(PROJECT/'.venv/bin/python'),str(BASE/script),*args],cwd=PROJECT,env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'},stdout=f,stderr=subprocess.STDOUT,check=True)

def main():
    assert not (REVIEW/'FINISH_STARTED.json').exists(),'one-shot workflow already started'
    assert json.loads((BASE/'index/checkpoint.json').read_text())['committed_rows']==72442
    assert (REVIEW/'INDEX_HASHES.json').exists()
    write(REVIEW/'FINISH_STARTED.json',{'one_shot':True,'paid_generation_depends_on_frozen_gate':True})
    step('validate_index.py')
    step('offline.py','dense');step('offline.py','finalize')
    selection=json.loads((REVIEW/'SELECTION.json').read_text());print('GATE',selection,flush=True)
    if selection['gate_passed']:
        step('offline_runtime_checks.py');step('freeze.py');step('run.py','--execute')
    step('report.py')
    commands=f'''# R1 固定协议复现

当前冻结结果只读；以下离线 cache 存在时 encode.py 复用完成 rows，不重编码。

```bash
# 仅缺少 upstream_metadata 时获取 commit/tree；不 checkout、不下载 corpus blob。
GIT_LFS_SKIP_SMUDGE=1 git clone --filter=blob:none --no-checkout --depth=1 https://github.com/IBM/mt-rag-benchmark.git benchmarks/parlant_rag_r1/upstream_metadata
git -C benchmarks/parlant_rag_r1/upstream_metadata fetch --depth=1 origin 2c618bb98db3c8526433e22d8a2f7320f10a7470
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python benchmarks/parlant_rag_r1/audit_corpus.py
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python benchmarks/parlant_rag_r1/encode.py
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python benchmarks/parlant_rag_r1/offline.py bm25
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python benchmarks/parlant_rag_r1/offline.py dense
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python benchmarks/parlant_rag_r1/offline.py finalize
```

语料获取沿用 R0 prepare.py 锁定 commit/asset/hash；不要在原 R0 目录重跑获取。BGE download_model.py 使用 model_lock.json 中 revision；新下载前应使用锁定 revision，不更新到模型 main。独立输出需复制新路径配置，不覆盖现有正式评测与回答。

门槛通过后执行的顺序为 offline_runtime_checks.py → freeze.py → run.py --execute（每题一次、最多十个新目标、根 .env key）；当前 run.py 明确阻止有 smoke_status 的目录重跑。新付费复现须另建 results/review/runtime、重新冻结文件，生成非确定性，不能覆盖当前回答。报告/包为 report.py → package.py。HOLDOUT、rewrite、reranker、裁判全部未运行。
'''
    (REVIEW/'REPRODUCE_COMMANDS.md').write_text(commands)
    reproduce=PROJECT/'REPRODUCE.md';marker=f'## RAG R1 Cloud ({REVIEW.name})'
    assert marker not in reproduce.read_text()
    with reproduce.open('a') as f:
        f.write(f'\n\n{marker}\n\n本轮为 IBM MTRAG Cloud 自定义 DEV 检索消融（105 有 qrels，11 unavailable）和原固定十题验证，非官方 test 或完整 MTRAG 成绩。原 R0 文件/首次正式回答/划分只读保留。固定 R0、history BM25、BGE current/history、RRF k60，选择 {selection["selected"]}，Recall@5 绝对变化 {selection["absolute_Recall5_gain"]:.6f}，门槛 {"通过" if selection["gate_passed"] else "未通过，未运行新付费回答"}。报告：`{PATHS["review"]}/REPORT.md`；结果：`{PATHS["result"]}`；代码：`benchmarks/parlant_rag_r1`；模型和 index hash、完整消融/轮次组/原十题对照/用量/脱敏包 SHA-256 与复现命令均在该审计目录。未运行 HOLDOUT、收费 rewrite、LLM judge、reranker；生成三条规则/CANNED_FLUID/匹配批量/参数复用 R0 冻结内部元数据。服务执行结束停止，不 commit/push、不自动进入下一轮。\n')
    step('package.py');write(REVIEW/'WORKFLOW_COMPLETE.json',{'complete':True,'selected':selection['selected'],'gate_passed':selection['gate_passed'],'service_stopped':True,'no_further_optimization':True});print('DELIVERY COMPLETE',flush=True)

if __name__=='__main__':main()
