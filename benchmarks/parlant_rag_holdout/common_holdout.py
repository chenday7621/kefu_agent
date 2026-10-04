"""HOLDOUT-only paths. Original R0/R1 modules are imported read-only."""
import sys
sys.dont_write_bytecode = True
import json, os, hashlib
from pathlib import Path
BASE = Path(__file__).resolve().parent
PROJECT = BASE.parents[1]
PATHS = json.loads((BASE/'paths.json').read_text())
REVIEW = PROJECT/PATHS['review']
RESULT = PROJECT/PATHS['result']
R0 = PROJECT/'benchmarks/parlant_rag'
R1 = PROJECT/'benchmarks/parlant_rag_r1'
DATA = R0/'data'
R0_RESULT = PROJECT/'results/mtrag_cloud_r0_20261004_012144'
sys.path.append(str(R0))
import common as r0
sha=r0.sha; digest=r0.digest; write=r0.write; jsonl=r0.jsonl; readl=r0.readl; BM25=r0.BM25; metric=r0.metric
PROTOCOL=json.loads((R1/'protocol.json').read_text())
def load_tasks():
    tasks=readl(RESULT/'runtime_holdout.jsonl')
    split=json.loads((DATA/'split.json').read_text())
    assert len(tasks)==89
    for t in tasks:
        assert set(t)=={'task_id','conversation_id','input'}
        assert t['conversation_id'] in split['HOLDOUT']
        assert all(set(m)=={'speaker','text'} for m in t['input'])
        assert t['input'][-1]['speaker']=='user'
    return tasks
