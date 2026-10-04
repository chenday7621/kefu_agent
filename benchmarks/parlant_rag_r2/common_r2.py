"""R2 generation only; original retrieval artifacts and modules remain read-only."""
import sys
sys.dont_write_bytecode=True
import json,os,hashlib
from pathlib import Path
BASE=Path(__file__).resolve().parent
PROJECT=BASE.parents[1]
PATHS=json.loads((BASE/'paths.json').read_text())
REVIEW=PROJECT/PATHS['review']; RESULT=PROJECT/PATHS['result']
R0=PROJECT/'benchmarks/parlant_rag';R1=PROJECT/'benchmarks/parlant_rag_r1';DATA=R0/'data'
HOLDOUT=PROJECT/'benchmarks/parlant_rag_holdout'
OLD_RESULT=PROJECT/'results/mtrag_cloud_r1_holdout_20261004_130837'
OLD_REVIEW=PROJECT/'_reviews/20261004_130837_RAG_R1B_HOLDOUT'
R0_RESULT=PROJECT/'results/mtrag_cloud_r0_20261004_012144'
sys.path.append(str(R0))
import common as r0
sha=r0.sha;digest=r0.digest;write=r0.write;jsonl=r0.jsonl;readl=r0.readl
def durable_text(path,text):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+'.tmp')
    with tmp.open('w') as f:f.write(text);f.flush();os.fsync(f.fileno())
    os.replace(tmp,path)
    fd=os.open(path.parent,os.O_RDONLY)
    try:os.fsync(fd)
    finally:os.close(fd)
def load_tasks():
    tasks=readl(RESULT/'runtime_targets.jsonl');assert len(tasks)==11
    holdout=set(json.loads((DATA/'split.json').read_text())['HOLDOUT'])
    assert len({t['conversation_id'] for t in tasks})==11
    for t in tasks:
        assert set(t)=={'task_id','conversation_id','input'} and t['conversation_id'] in holdout
        assert all(set(m)=={'speaker','text'} for m in t['input']) and t['input'][-1]['speaker']=='user'
    return tasks
