"""Download only locked official assets, freeze split BEFORE scoring; no HOLDOUT answer analysis."""
import collections,csv,json,random,re,subprocess,zipfile,urllib.request
from common import *

def main():
    commit=json.loads((BASE/'upstream_commit.json').read_text())['sha']
    assets=['README.md','LICENSE','.gitattributes','corpora/README.md','mtrag-human/README.md','mtrag-human/generation_tasks/README.md','mtrag-human/retrieval_tasks/README.md','scripts/evaluation/README.md','scripts/evaluation/run_retrieval_eval.py','scripts/evaluation/run_algorithmic.py','mtrag-human/conversations/conversations.json','mtrag-human/generation_tasks/reference.jsonl','mtrag-human/retrieval_tasks/cloud/cloud_lastturn.jsonl','mtrag-human/retrieval_tasks/cloud/qrels/dev.tsv','corpora/passage_level/cloud.jsonl.zip']
    for name in assets:
        p=OFFICIAL/name
        if not p.exists():
            p.parent.mkdir(parents=True,exist_ok=True)
            p.write_bytes(urllib.request.urlopen('https://raw.githubusercontent.com/IBM/mt-rag-benchmark/'+commit+'/'+name,timeout=120).read())
        assert not p.read_bytes()[:100].startswith(b'version https://git-lfs.github.com/spec/v1'),name
    tasks=readl(OFFICIAL/'mtrag-human/generation_tasks/reference.jsonl')
    cloud=[x for x in tasks if x['Collection']==COLLECTION]
    ids=sorted({x['conversation_id'] for x in cloud});rng=random.Random(42);shuffled=ids.copy();rng.shuffle(shuffled);n=int(len(ids)*0.6)
    split={'kind':'custom conversation-level DEV/HOLDOUT, not official test','seed':42,'method':'sort conversation_id; Python Random(42).shuffle; floor(0.6*N) DEV; remainder HOLDOUT','sorted_conversation_ids':ids,'DEV':shuffled[:n],'HOLDOUT':shuffled[n:]}
    if (DATA/'split.json').exists():assert json.loads((DATA/'split.json').read_text())==split
    else:write(DATA/'split.json',split)
    assert not set(split['DEV'])&set(split['HOLDOUT'])
    dev=sorted((x for x in cloud if x['conversation_id'] in split['DEV']),key=lambda x:x['task_id'])
    safe=[sanitize(x) for x in dev];jsonl(DATA/'runtime_dev.jsonl',safe)
    # Sampling uses only task IDs and conversation IDs; no answerability or retrieval score.
    candidates=[{'task_id':x['task_id'],'conversation_id':x['conversation_id']} for x in safe]
    rng=random.Random(42);rng.shuffle(candidates);selected=[];seen=set()
    for x in candidates:
        if x['conversation_id'] not in seen:selected.append(x);seen.add(x['conversation_id'])
        if len(selected)==10:break
    selection={'seed':42,'method':'sorted DEV tasks, Random(42).shuffle, first 10 distinct conversations','targets':selected,'canary_task_id':selected[0]['task_id']}
    if (DATA/'selection.json').exists():assert json.loads((DATA/'selection.json').read_text())==selection
    else:write(DATA/'selection.json',selection)
    # Gold stored independently, only DEV. Official contexts are never runtime input.
    jsonl(DATA/'scoring_dev.jsonl',[{'task_id':x['task_id'],'targets':[{'speaker':t['speaker'],'text':t['text']} for t in x['targets']],'reference_context_ids':[c['document_id'] for c in x['contexts']]} for x in dev])
    qrows=list(csv.DictReader((OFFICIAL/'mtrag-human/retrieval_tasks/cloud/qrels/dev.tsv').open(),delimiter='\t'));qrels={}
    for x in qrows:qrels.setdefault(x['query-id'],{})[x['corpus-id']]=int(x['score'])
    z=zipfile.ZipFile(OFFICIAL/'corpora/passage_level/cloud.jsonl.zip');assert z.namelist()==['cloud.jsonl'];assert z.testzip() is None
    passages=[json.loads(x) for x in z.open('cloud.jsonl')]
    pids={x['_id'] for x in passages};assert len(pids)==len(passages);assert all(x['_id']==x['id'] for x in passages)
    missing=[x for x in qrows if x['corpus-id'] not in pids];assert not missing
    taskids={x['task_id'] for x in cloud};assert set(qrels)<=taskids
    queries=readl(OFFICIAL/'mtrag-human/retrieval_tasks/cloud/cloud_lastturn.jsonl');qt={x['_id']:x['text'] for x in queries}
    assert set(qt)==set(qrels)
    query_mismatch=[x['task_id'] for x in dev if x['task_id'] in qt and qt[x['task_id']]!='|user|: '+x['input'][-1]['text']]
    assert not query_mismatch
    for x in passages:assert re.fullmatch(r'ibmcld_\d+-\d+-\d+',x['_id'])
    docs={x['_id'].rsplit('-',2)[0] for x in passages}
    jsonl(DATA/'cloud_passages.jsonl',passages)
    write(DATA/'qrels_dev.json',{x['task_id']:qrels[x['task_id']] for x in dev if x['task_id'] in qrels})
    conversations=json.loads((OFFICIAL/'mtrag-human/conversations/conversations.json').read_text())
    audit={'upstream_commit':commit,'license':'Apache-2.0 (repository LICENSE); corpus source IBM Cloud technical documentation','human_all_conversations':len(conversations),'human_all_tasks':len(tasks),'cloud_conversations':len(ids),'cloud_conversations_source_count':sum(x['domain']==COLLECTION for x in conversations),'cloud_tasks':len(cloud),'cloud_passages':len(passages),'cloud_document_ids':len(docs),'document_count_method':'count unique prefix before two numeric passage offsets; never applied to qrels matching','official_readme_cloud_documents':57638,'official_readme_cloud_passages':61022,'readme_count_mismatch':True,'qrels_rows':len(qrows),'qrels_queries':len(qrels),'qrels_grades':sorted({int(x['score']) for x in qrows}),'qrels_missing_passages':missing,'qrels_missing_tasks':sorted(set(qrels)-taskids),'DEV_conversations':n,'HOLDOUT_conversations':len(ids)-n,'DEV_tasks':len(dev),'HOLDOUT_tasks':len(cloud)-len(dev),'DEV_qrels_tasks':sum(x['task_id'] in qrels for x in dev),'DEV_unannotated_task_ids':[x['task_id'] for x in dev if x['task_id'] not in qrels],'query_text_mismatch':query_mismatch,'LFS_pointer_check':'all selected assets checked, ZIP CRC checked','HOLDOUT_answers_analyzed':False}
    write(REVIEW/'DATA_AUDIT.json',audit)
    config={'name':'RAG R0 Cloud','query':'current user text only','retrieval_top_k':10,'answer_top_k':5,'bm25':{'k1':1.2,'b':0.75,'idf':'log(1+(N-df+0.5)/(df+0.5))','tokenizer':'Unicode casefold + re.findall(r"\\b\\w+\\b"); no stemming/stopwords','indexed_fields':'title + space + exact official text','query_tf':'distinct terms once, sorted','tie_break':'score descending, full passage_id ascending','chunking':'none, complete official passage_level corpus','zero_score':'include to fixed K'},'model':'deepseek-chat','embedding':'sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2 offline CPU','batching':'unmodified BasicOptimizationPolicy','seed':42,'per_sample_seconds':180,'infrastructure_recovery_max':1,'startup_initializations':1,'llm_judge':False,'holdout_run':False,'rules':[{'condition':'The user asks about IBM Cloud information or technical documentation','action':'Answer factual questions using the retrieved IBM Cloud passages. Use only facts supported by these passages; use the existing conversation only to understand the question.'},{'condition':'The response makes a factual claim supported by a retrieved passage','action':'Give the source by citing the exact full passage document_id in square brackets, and include its source URL when helpful. Never invent a source ID.'},{'condition':'The retrieved passages do not provide enough evidence, or the user question is ambiguous','action':'Clearly explain the evidence limitation or ask a concise clarification. Do not infer missing technical facts.'}]}
    write(BASE/'config.json',config)
    write(REVIEW/'DATA_HASHES.json',{str(p.relative_to(PROJECT)):sha(p) for p in sorted(DATA.rglob('*')) if p.is_file()})
    for name in ['split.json','selection.json']:write(REVIEW/name,json.loads((DATA/name).read_text()))
    print(json.dumps(audit,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
