"""Read-only integrity and optional HOLDOUT metric/ranking replay; no API."""
from common_holdout import *
import argparse
def main():
    parser=argparse.ArgumentParser();parser.add_argument('--recompute-metrics',action='store_true');parser.add_argument('--rerank',action='store_true');args=parser.parse_args()
    manifests=['PRESERVATION_BEFORE.json','ASSETS_FROZEN.json','RETRIEVAL_FROZEN.json','GENERATION_FROZEN.json','RESUME_FROZEN.json','HASH_MANIFEST.json']
    for name in manifests:
        x=json.loads((REVIEW/name).read_text());files=x.get('files',x)
        for p,h in files.items():assert sha(PROJECT/p)==h,(name,p)
    if args.recompute_metrics:
        from score import summarize
        rels=json.loads((RESULT/'qrels_holdout_scoring_only.json').read_text());summary={}
        for c in ['R0','R1-B']:
            rows=[]
            for r in readl(RESULT/f'{c}_rankings.jsonl'):
                ids=[h['document_id'] for h in r['top10']];gold=rels.get(r['task_id'],{})
                rows.append({'metrics':{**metric(ids,gold,5),**metric(ids,gold,10)}})
            summary[c]=summarize(rows)
        assert summary==json.loads((RESULT/'offline_metrics.json').read_text())
        print(json.dumps(summary,indent=2))
    if args.rerank:
        import numpy as np
        tasks=load_tasks();bm=BM25.load();frozen={c:{r['task_id']:r for r in readl(RESULT/f'{c}_rankings.jsonl')} for c in ['R0','R1-B']}
        original=json.loads((R1/'index/queries.json').read_text());ov=np.load(R1/'index/queries.npy',mmap_mode='r');lookup={q:ov[i] for i,q in enumerate(original)}
        new=json.loads((RESULT/'holdout_query_overlay.json').read_text());nv=np.load(RESULT/'holdout_query_overlay.npy',mmap_mode='r');lookup.update({q:nv[i] for i,q in enumerate(new)})
        matrix=np.load(R1/'index/corpus.npy',mmap_mode='r');ids=json.loads((R1/'index/passage_ids.json').read_text())
        for t in tasks:
            q=t['input'][-1]['text'];a=bm.search(q,10);scores=matrix@lookup[q];top=np.argsort(-scores,kind='stable')[:10]
            assert [x['document_id'] for x in a]==[x['document_id'] for x in frozen['R0'][t['task_id']]['top10']]
            assert [ids[int(i)] for i in top]==[x['document_id'] for x in frozen['R1-B'][t['task_id']]['top10']]
        print('89 HOLDOUT queries: original BM25/BGE Top10 exactly reproduced; no re-encoding')
    print('READ-ONLY HASH VERIFICATION PASSED; no model API')
if __name__=='__main__':main()
