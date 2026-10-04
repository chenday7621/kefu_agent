"""Read-only integrity, exact prompt and reused evidence verification. No model or retrieval."""
from common_r2 import *
from evidence import wrapped_passages
from coverage import checks
from metrics_utils import read_obs
import socket
def main():
    for name in ['PRESERVATION_BEFORE.json','ASSETS_FROZEN.json','GENERATION_FROZEN.json','HASH_MANIFEST.json']:
        x=json.loads((REVIEW/name).read_text());files=x.get('files',x)
        for p,h in files.items():assert sha(PROJECT/p)==h,(name,p)
    pairs=readl(RESULT/'CONTROL_vs_R2_answers.jsonl');assert len(pairs)==11
    old={x['task']['task_id']:x for x in readl(OLD_RESULT/'selected_retrieval.jsonl') if x['candidate']=='R1-B'}
    for pair in pairs:
        for arm in ['CONTROL','R2-EVIDENCE']:
            d=pair[arm];expected=old[d['task_id']]['top5'];assert expected==d['original_top5']==d['actual_top5']
            assert d['runtime_input']==old[d['task_id']]['task'] and d['query']==old[d['task_id']]['query']
            path=RESULT/'attempts'/d['attempt_id'];prompt=(path/'final_generation_prompt.txt').read_text()
            assert sha(path/'final_generation_prompt.txt')==d['final_prompt_sha256']
            if arm=='CONTROL':
                assert prompt==(path/'native_generation_prompt.txt').read_text()
                rows,rendered=checks(prompt,expected,expected);assert rendered==expected and all(x['full_text_in_prompt'] for x in rows)
            else:assert wrapped_passages(prompt)==[{k:p[k] for k in ['document_id','title','url','text']} for p in expected]
    rows=[r for r in read_obs() if r['attempt_id']!='offline-checks']
    calls=[r for r in rows if r['kind']=='model_call_started'];assert len(calls)==88
    for r in calls:assert sha(RESULT/'attempts'/r['attempt_id']/'prompts'/f"{r['call_id']}.txt")==r['prompt_sha256']
    assert json.loads((RESULT/'smoke_status.json').read_text())['service_stopped']
    for port in [18903,18921]:
        with socket.socket() as s:s.settimeout(.3);assert s.connect_ex(('127.0.0.1',port))!=0
    print('READ-ONLY VERIFIED: original hashes unchanged, 11 paired identical Top5, 22 actual final prompts, 88 per-call prompts, services stopped; no API/retrieval')
if __name__=='__main__':main()
