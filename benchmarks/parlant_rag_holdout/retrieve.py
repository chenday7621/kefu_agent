"""Label-free R0/R1-B rankings; extend queries only in a separate frozen overlay."""
from common_holdout import *
import time, numpy as np
def main():
    assert (REVIEW/'ASSETS_FROZEN.json').exists()
    assert not (REVIEW/'RETRIEVAL_FROZEN.json').exists(),'Never overwrite frozen retrieval'
    started=time.monotonic();tasks=load_tasks();bm=BM25.load();rankings=[]
    for t in tasks:
        hits=bm.search(t['input'][-1]['text'],10)
        rankings.append({'task_id':t['task_id'],'query':t['input'][-1]['text'],'top10':hits})
    jsonl(RESULT/'R0_rankings.jsonl',rankings)
    sys.path.append(str(R1))
    from encode import encoder,embed
    original_queries=json.loads((R1/'index/queries.json').read_text())
    original_vectors=np.load(R1/'index/queries.npy',mmap_mode='r')
    lookup={q:i for i,q in enumerate(original_queries)}
    queries=sorted({t['input'][-1]['text'] for t in tasks});misses=[q for q in queries if q not in lookup]
    tok,model=encoder();vector_lookup={q:original_vectors[lookup[q]] for q in queries if q in lookup}
    raw=tok([PROTOCOL['query_instruction']+q for q in misses],truncation=False,padding=False)
    records=tok([PROTOCOL['query_instruction']+q for q in misses],max_length=512,truncation=True,padding=False)
    vectors=[]
    for start in range(0,len(misses),16):
        vectors.extend(embed(tok,model,[{k:v[i] for k,v in records.items()} for i in range(start,min(start+16,len(misses)))]))
    new=np.asarray(vectors,dtype='float32').reshape(-1,768)
    assert np.isfinite(new).all() and np.allclose(np.linalg.norm(new,axis=1),1,atol=1e-5)
    np.save(RESULT/'holdout_query_overlay.npy',new);write(RESULT/'holdout_query_overlay.json',misses)
    vector_lookup.update(dict(zip(misses,new)))
    matrix=np.load(R1/'index/corpus.npy',mmap_mode='r');ids=json.loads((R1/'index/passage_ids.json').read_text())
    assert matrix.shape==(72442,768) and ids==sorted(ids) and len(set(ids))==72442
    passages={p['_id']:p for p in readl(DATA/'cloud_passages.jsonl')}
    assert set(passages)==set(ids)
    ranked={}
    for q in queries:
        scores=matrix@vector_lookup[q];top=np.argsort(-scores,kind='stable')[:10];hits=[]
        for i in top:
            pp=passages[ids[int(i)]]
            hits.append({'document_id':pp['_id'],'text':pp['text'],'title':pp['title'],'url':pp['url'],'score':float(scores[i])})
        ranked[q]=hits
    dense=[{'task_id':t['task_id'],'query':t['input'][-1]['text'],'top10':ranked[t['input'][-1]['text']]} for t in tasks]
    jsonl(RESULT/'R1-B_rankings.jsonl',dense)
    selected=json.loads((RESULT/'selection.json').read_text())['targets'];taskmap={t['task_id']:t for t in tasks};payload=[]
    for candidate,rows in [('R0',rankings),('R1-B',dense)]:
        rows={r['task_id']:r for r in rows}
        for s in selected:
            t=taskmap[s['task_id']];r=rows[s['task_id']]
            payload.append({'task':t,'candidate':candidate,'query':r['query'],'top10':r['top10'],'top5':r['top10'][:5]})
    jsonl(RESULT/'selected_retrieval.jsonl',payload)
    files=[RESULT/name for name in ['runtime_holdout.jsonl','selection.json','R0_rankings.jsonl','R1-B_rankings.jsonl','selected_retrieval.jsonl','holdout_query_overlay.npy','holdout_query_overlay.json']]
    write(REVIEW/'RETRIEVAL_FROZEN.json',{'files':{str(p.relative_to(PROJECT)):sha(p) for p in files},'queries':len(queries),'original_cache_hits':len(queries)-len(misses),'overlay_misses_encoded_once':len(misses),'query_tokens':dict(zip(misses,map(len,raw['input_ids']))),'truncated_queries':sum(len(x)>512 for x in raw['input_ids']),'corpus_reencoded':0,'qrels_read':False,'reference_answers_read':False,'seconds':time.monotonic()-started})
    print('LABEL-FREE RETRIEVAL FROZEN',len(tasks),len(misses),round(time.monotonic()-started,2),flush=True)
if __name__=='__main__':main()
