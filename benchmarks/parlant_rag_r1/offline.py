"""Fixed DEV-only ablation, using sanitized queries and isolated offline qrels."""
from common_r1 import *
import argparse,collections,time,functools

def summarize(rows):
    annotated=[r for r in rows if r['metrics']['Recall@5']!='unavailable']
    return {'annotated_tasks':len(annotated),'unannotated_tasks':len(rows)-len(annotated),**{key:sum(r['metrics'][key] for r in annotated)/len(annotated) for key in ['Recall@5','Recall@10','nDCG@5','nDCG@10']},'Recall@10_zero_tasks':sum(r['metrics']['Recall@10']==0 for r in annotated)}

def score(candidate,retrieved):
    qrels=json.loads((DATA/'qrels_dev.json').read_text());tasks=load_tasks();rows=[]
    for t in tasks:
        tid=t['task_id'];hits=retrieved[tid];ids=[h['document_id'] for h in hits];rels=qrels.get(tid,{})
        row={'task_id':tid,'conversation_id':t['conversation_id'],'current_turn':(len(t['input'])+1)//2,'prior_history_messages':len(t['input'])-1,'history_length_words':sum(len(r0.tokens(m['text'])) for m in t['input'][:-1]),'query':t['input'][-1]['text'] if candidate in ('R0','R1-B') else history_query(t['input']),'top100':hits,'metrics':{**metric(ids,rels,5),**metric(ids,rels,10)}}
        rows.append(row)
    jsonl(RESULT/f'{candidate}_per_task.jsonl',rows)
    jsonl(RESULT/f'{candidate}_failures.jsonl',[r for r in rows if r['metrics']['Recall@10']==0])
    summary=summarize(rows);write(RESULT/f'{candidate}_metrics.json',summary)
    print(candidate,summary,flush=True);return rows

def bm25():
    bm=BM25.load();tasks=load_tasks()
    for name in ['R0','R1-A']:
        rankings={}
        for i,t in enumerate(tasks):
            query=t['input'][-1]['text'] if name=='R0' else history_query(t['input'])
            rankings[t['task_id']]=[{'document_id':h['document_id'],'score':h['score']} for h in bm.search(query,100)]
        score(name,rankings)
    old=json.loads((R0_RESULT/'retrieval_metrics.json').read_text())
    current=json.loads((RESULT/'R0_metrics.json').read_text())
    # The frozen R0 report stores its means under metrics.
    expected=old.get('macro_metrics',old.get('metrics',old.get('macro',old)))
    for key in ['Recall@5','Recall@10','nDCG@5','nDCG@10']:assert abs(current[key]-expected[key])<1e-12,(key,current,old)
    write(REVIEW/'R0_REPRODUCTION_CHECK.json',{'identical_macro_metrics':True,'bm25_index_sha256':sha(DATA/'bm25.pkl'),'all_queries_current_only':True})

def dense():
    started=time.monotonic()
    import numpy as np
    from encode import encoder,embed
    cache=BASE/'index';assert json.loads((cache/'checkpoint.json').read_text())['committed_rows']==72442
    tok,model=encoder();matrix=np.load(cache/'corpus.npy',mmap_mode='r');ids=json.loads((cache/'passage_ids.json').read_text());tasks=load_tasks()
    queries=sorted({q for t in tasks for q in [t['input'][-1]['text'],history_query(t['input'])]})
    raw_queries=tok([PROTOCOL['query_instruction']+q for q in queries],truncation=False,padding=False)
    original_lengths={q:len(ids) for q,ids in zip(queries,raw_queries['input_ids'])}
    write(REVIEW/'QUERY_ENCODING_AUDIT.json',{'unique_queries':len(queries),'query_max_length':512,'right_truncation':True,'current_question_truncated_tasks':sum(original_lengths[t['input'][-1]['text']]>512 for t in tasks),'history_query_truncated_tasks':sum(original_lengths[history_query(t['input'])]>512 for t in tasks),'tasks':[{'task_id':t['task_id'],'current_query_tokens':original_lengths[t['input'][-1]['text']],'history_query_tokens':original_lengths[history_query(t['input'])],'same_A_C_full_query':True} for t in tasks]})
    records=tok([PROTOCOL['query_instruction']+q for q in queries],max_length=512,truncation=True,padding=False)
    vectors=[]
    for start in range(0,len(queries),16):vectors.extend(embed(tok,model,[{k:v[i] for k,v in records.items()} for i in range(start,min(start+16,len(queries)))]))
    vectors=np.asarray(vectors,dtype='float32');assert np.isfinite(vectors).all() and np.allclose(np.linalg.norm(vectors,axis=1),1,atol=1e-5)
    np.save(cache/'queries.npy',vectors)
    write(cache/'queries.json',queries);lookup={q:i for i,q in enumerate(queries)};ranking={}
    for q,i in lookup.items():
        scores=matrix@vectors[i];top=np.argsort(-scores,kind='stable')[:100]
        ranking[q]=[{'document_id':ids[int(j)],'score':float(scores[j])} for j in top]
    for name in ['R1-B','R1-C']:
        score(name,{t['task_id']:ranking[t['input'][-1]['text'] if name=='R1-B' else history_query(t['input'])] for t in tasks})
    a={r['task_id']:r for r in readl(RESULT/'R1-A_per_task.jsonl')};c={r['task_id']:r for r in readl(RESULT/'R1-C_per_task.jsonl')};fused={}
    for t in tasks:
        values=collections.defaultdict(float)
        for rows in [a,c]:
            for rank,h in enumerate(rows[t['task_id']]['top100'],1):values[h['document_id']]+=1/(60+rank)
        fused[t['task_id']]=[{'document_id':pid,'score':value} for pid,value in sorted(values.items(),key=lambda item:(-item[1],item[0]))[:100]]
    score('R1-D',fused)
    write(REVIEW/'QUERY_CACHE_HASHES.json',{p.name:sha(p) for p in [cache/'queries.npy',cache/'queries.json']})
    write(REVIEW/'OFFLINE_TIMING.json',{'dense_query_encode_rank_B_C_and_RRF_D_seconds':time.monotonic()-started,'bm25_R0_A_seconds':'unavailable','corpus_encode_seconds':json.loads((BASE/'index/checkpoint.json').read_text())['elapsed_this_process'],'paid_API_calls_offline':0})

def finalize():
    summaries={name:json.loads((RESULT/f'{name}_metrics.json').read_text()) for name in PROTOCOL['candidates']}
    def compare(a,b):
        for key in ['Recall@5','nDCG@5','Recall@10']:
            delta=summaries[a][key]-summaries[b][key]
            if abs(delta)>1e-12:return -1 if delta>0 else 1
        return (a>b)-(a<b)
    ordered=sorted(PROTOCOL['candidates'][1:],key=functools.cmp_to_key(compare))
    chosen=ordered[0];base=summaries['R0'];best=summaries[chosen];gain=best['Recall@5']-base['Recall@5'];passed=gain>=.05-1e-12 and best['nDCG@5']>=base['nDCG@5']-1e-12
    write(RESULT/'offline_metrics.json',summaries)
    selection={'selected':chosen,'ranked_candidates':ordered,'gate_passed':passed,'absolute_Recall5_gain':gain,'nDCG5_change':best['nDCG@5']-base['nDCG@5'],'protocol_sha256':sha(BASE/'protocol.json'),'no_tuning':True,'HOLDOUT_run':False}
    write(REVIEW/'SELECTION.json',selection)
    grouped={}
    for name in PROTOCOL['candidates']:
        rows=readl(RESULT/f'{name}_per_task.jsonl');groups={}
        for key,thresholds,labels in [('current_turn',[1,3,6],['1','2-3','4-6','7+']),('prior_history_messages',[0,4,10],['0','1-4','5-10','11+']),('history_length_words',[0,100,300],['0','1-100','101-300','301+'])]:
            by=collections.defaultdict(list)
            for row in rows:by[labels[next((i for i,x in enumerate(thresholds) if row[key]<=x),3)]].append(row)
            groups[key]={label:summarize(by[label]) for label in labels if by[label]}
        grouped[name]=groups
    write(RESULT/'grouped_metrics.json',grouped);print('SELECTION',selection,flush=True)
    targets=json.loads((DATA/'selection.json').read_text())['targets'];tasks={t['task_id']:t for t in load_tasks()};rows={r['task_id']:r for r in readl(RESULT/f'{chosen}_per_task.jsonl')};passages={p['_id']:p for p in readl(DATA/'cloud_passages.jsonl')}
    payload=[]
    for selected in targets:
        tid=selected['task_id'];hits=[]
        for h in rows[tid]['top100'][:10]:
            p=passages[h['document_id']];hits.append({'document_id':p['_id'],'text':p['text'],'title':p['title'],'url':p['url'],'score':h['score']})
        payload.append({'task':tasks[tid],'candidate':chosen,'query':rows[tid]['query'],'top10':hits,'top5':hits[:5]})
    jsonl(RESULT/'selected_ten_retrieval.jsonl',payload)

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('stage',choices=['bm25','dense','finalize']);args=parser.parse_args();globals()[args.stage]()
