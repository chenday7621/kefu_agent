"""R1 paths and read-only reuse of R0 BM25/metrics; never write into R0."""
import sys
sys.dont_write_bytecode=True
from pathlib import Path
import json,hashlib,os
BASE=Path(__file__).resolve().parent
PROJECT=BASE.parents[1]
PATHS=json.loads((BASE/'paths.json').read_text())
REVIEW=PROJECT/PATHS['review'];RESULT=PROJECT/PATHS['result'];R0=PROJECT/PATHS['r0_base'];DATA=R0/'data';R0_REVIEW=PROJECT/PATHS['r0_review'];R0_RESULT=PROJECT/PATHS['r0_result']
sys.path.append(str(R0))
import common as r0
sha=r0.sha;digest=r0.digest;write=r0.write;jsonl=r0.jsonl;readl=r0.readl;BM25=r0.BM25;metric=r0.metric
PROTOCOL=json.loads((BASE/'protocol.json').read_text())

def history_query(messages):
    assert messages[-1]['speaker']=='user'
    if len(messages)==1:return messages[-1]['text']
    assert len(messages)>=3 and messages[-3]['speaker']=='user' and messages[-2]['speaker']=='agent'
    return '\n'.join(m['text'] for m in messages[-3:])

def load_tasks():
    tasks=readl(DATA/'runtime_dev.jsonl');split=json.loads((DATA/'split.json').read_text());assert len(tasks)==116
    for t in tasks:
        assert set(t)=={'task_id','conversation_id','input'} and t['conversation_id'] in split['DEV']
        assert all(set(m)=={'speaker','text'} for m in t['input'])
    return tasks
