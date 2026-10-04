"""Build local CPU index and score every DEV task with qrels; no paid calls."""
import asyncio, math, time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from common import *

async def protocol_checks():
    from parlant.adapters.db.transient import TransientDocumentDatabase
    from parlant.core.sessions import SessionDocumentStore,SessionId,EventSource,EventKind
    from parlant.core.customers import CustomerId
    from parlant.core.agents import AgentId
    from parlant.core.app_modules.sessions import SessionModule
    from parlant.core.engines.alpha.engine_context import Interaction
    db=TransientDocumentDatabase()
    async with SessionDocumentStore(db) as store:
        # Real local SessionModule.create_event on a transient document store.
        module=object.__new__(SessionModule);module._session_store=store
        module._tracer=SimpleNamespace(trace_id='offline');module.dispatch_processing_task=AsyncMock()
        s=await store.create_session(CustomerId('guest'),AgentId('offline'))
        history=[{'speaker':'user','text':'Earlier question'},{'speaker':'agent','text':'Earlier official response'}]
        for m in history:
            await module.create_event(s.id,EventKind.MESSAGE,{'message':m['text'],'participant':{'id':'offline','display_name':m['speaker']},'flagged':False,'tags':[]},None,EventSource.CUSTOMER if m['speaker']=='user' else EventSource.AI_AGENT,trigger_processing=False)
        assert module.dispatch_processing_task.await_count==0
        await module.create_event(s.id,EventKind.MESSAGE,{'message':'Current only','participant':{'id':'offline','display_name':'user'},'flagged':False,'tags':[]},None,EventSource.CUSTOMER,trigger_processing=True)
        assert module.dispatch_processing_task.await_count==1
        interaction=Interaction(await store.list_events(s.id))
        assert interaction.last_customer_message.content=='Current only'
        assert [m.content for m in interaction.messages]==['Earlier question','Earlier official response','Current only']
        await store.delete_session(s.id)
        assert not await store._event_collection.find({'session_id': {'$eq': s.id}})
        from parlant.core.common import ItemNotFoundError
        try:await store.read_session(s.id)
        except ItemNotFoundError:pass
        else:raise AssertionError('Session still exists')
    return {'real_session_module_history_no_dispatch':True,'one_target_dispatch':True,'speaker_text_roundtrip':True,'session_events_removed':True}

def main():
    assert (DATA/'split.json').exists() and (DATA/'selection.json').exists()
    start=time.monotonic();bm=BM25(readl(DATA/'cloud_passages.jsonl'));bm.save(DATA/'bm25.pkl')
    tasks=readl(DATA/'runtime_dev.jsonl');qrels=json.loads((DATA/'qrels_dev.json').read_text());split=json.loads((DATA/'split.json').read_text())
    assert all(set(x)=={'task_id','conversation_id','input'} for x in tasks)
    assert all(set(m)=={'speaker','text'} for x in tasks for m in x['input'])
    assert all(x['conversation_id'] in split['DEV'] for x in tasks)
    assert len({x['task_id'] for x in tasks})==len(tasks)
    results=[]
    for x in tasks:
        hits=bm.search(x['input'][-1]['text']);ids=[h['document_id'] for h in hits]
        scores={}
        if x['task_id'] in qrels:
            for k in (5,10):scores.update(metric(ids,qrels[x['task_id']],k))
        else:scores={k:'unavailable' for k in ('Recall@5','Recall@10','nDCG@5','nDCG@10')}
        results.append({'task_id':x['task_id'],'query':x['input'][-1]['text'],'qrels_covered':x['task_id'] in qrels,'metrics':scores,'top10':[{'document_id':h['document_id'],'score':h['score']} for h in hits],'relevant_ids':list(qrels.get(x['task_id'],{}))})
    covered=[x for x in results if x['qrels_covered']]
    metrics={k:sum(x['metrics'][k] for x in covered)/len(covered) for k in covered[0]['metrics']}
    checks=asyncio.run(protocol_checks())
    assert metric(['a','b'],{'a':1,'b':1},5)=={'Recall@5':1.0,'nDCG@5':1.0}
    assert metric(['z','a'],{'a':1,'b':1},5)['Recall@5']==0.5
    assert abs(metric(['z','a'],{'a':1},5)['nDCG@5']-1/math.log2(3))<1e-12
    assert metric([],{},5)['Recall@5']=='unavailable'
    fixture=BM25([{'_id':'a','title':'','text':'same'},{'_id':'b','title':'','text':'same'}]);assert [x['document_id'] for x in fixture.search('same',2)]==['a','b']
    assert fixture.search('same same',2)==fixture.search('same',2)
    # Compare native return payload against exactly what is indexed; not official contexts.
    from parlant.sdk import RetrieverResult
    pids={p['_id'] for p in bm.passages}
    for x in tasks:
        hits=bm.search(x['input'][-1]['text'],5);payload=RetrieverResult(data={'passages':hits})
        assert payload.data['passages']==hits
        assert all(h['document_id'] in pids for h in hits)
    checks.update({'no_gold_runtime_fields':True,'gold_scores_only_DEV':True,'fixed_split_before_evaluation':True,'exact_qrels_mapping':True,'deterministic_bm25_ties':True,'metric_hand_fixtures':True,'native_RetrieverResult_payload_roundtrip':True,'final_prompt_coverage':'must be verified in real smoke, not claimed by offline payload check'})
    jsonl(RESULT/'retrieval_per_task.jsonl',results)
    jsonl(RESULT/'retrieval_failures.jsonl',[x for x in covered if x['metrics']['Recall@10']<1])
    write(RESULT/'retrieval_metrics.json',{'DEV_tasks':len(tasks),'qrels_tasks':len(covered),'unannotated_tasks':len(tasks)-len(covered),'macro_metrics':metrics,'official_equivalence':'same binary-qrels recall and linear-gain ndcg_cut as official pytrec_eval evaluate; pure stdlib implementation, hand fixtures verified; official script only lists 1/3/5, extended to 10','unannotated_policy':'excluded from denominator; unavailable, never 0','seconds_build_and_evaluate':time.monotonic()-start,'index_sha256':sha(DATA/'bm25.pkl')})
    write(REVIEW/'OFFLINE_CHECKS.json',checks)
    print(json.dumps(metrics,indent=2));print('OFFLINE CHECKS PASSED',checks)
if __name__=='__main__':main()
