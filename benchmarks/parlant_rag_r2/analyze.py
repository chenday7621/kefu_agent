"""Offline answer scoring after service stop; never recompute retrieval or call a judge."""
from common_r2 import *
from metrics_utils import f1,rouge,usage_group,read_obs
from evidence import wrapped_passages
from coverage import checks
import collections,re,random,shutil
ARMS=['CONTROL','R2-EVIDENCE']
def main():
    state=json.loads((RESULT/'smoke_status.json').read_text());assert state['service_stopped']
    selection=json.loads((RESULT/'selection.json').read_text())['targets'];refs=json.loads((OLD_RESULT/'reference_answers_scoring_only.json').read_text())
    # Frozen offline scoring inputs only, inaccessible to runtime run/observe/evidence modules.
    retrieval={r['task_id']:r for r in readl(OLD_RESULT/'R1-B_per_task.jsonl') if r['task_id'] in {t['task_id'] for t in selection}}
    write(RESULT/'reference_answers_scoring_only.json',{t['task_id']:refs[t['task_id']] for t in selection})
    write(RESULT/'retrieval_metrics_reused.json',{'source':str((OLD_RESULT/'offline_metrics.json').relative_to(PROJECT)),'source_sha256':sha(OLD_RESULT/'offline_metrics.json'),'R1-B':json.loads((OLD_RESULT/'offline_metrics.json').read_text())['R1-B'],'recomputed':False})
    payload={(x['task']['task_id'],x['candidate']):x for x in readl(RESULT/'selected_retrieval.jsonl')}
    outcomes={(x['task_id'],x['candidate']):x for x in state['outcomes'] if x['status'].startswith('completed')}
    observations=[r for r in read_obs() if r['attempt_id']!='offline-checks'];price=json.loads((BASE/'pricing.json').read_text());details=[];pairs=[]
    for target in selection:
        tid=target['task_id'];pair={'task_id':tid,'conversation_id':target['conversation_id'],'reference_answers_scoring_only':refs[tid]}
        for arm in ARMS:
            out=outcomes.get((tid,arm),{});answer=out.get('answer','unavailable');attempt=out.get('attempt_id');frozen=payload[(tid,arm)];gold=set(retrieval[tid]['gold_ids_scoring_only'])
            evidence=out.get('actual_evidence',[]);ids={x['document_id'] for x in evidence};urls={x['url'] for x in evidence}
            rows=[r for r in observations if r['attempt_id']==attempt] if attempt else []
            coverage=False;native_unchanged='unavailable';prompt_sha='unavailable';native_sha='unavailable'
            if attempt:
                prompt=RESULT/'attempts'/attempt/'final_generation_prompt.txt';native=RESULT/'attempts'/attempt/'native_generation_prompt.txt'
                assert prompt.exists() and prompt.stat().st_size and native.exists() and native.stat().st_size
                built=prompt.read_text();prompt_sha=sha(prompt);native_sha=sha(native);assert evidence==frozen['top5']
                if arm=='CONTROL':
                    check,rendered=checks(built,evidence,evidence);coverage=rendered==evidence and all(x['full_text_in_prompt'] and x['id_in_prompt'] for x in check);native_unchanged=built==native.read_text();assert native_unchanged
                else:
                    rendered=wrapped_passages(built);coverage=rendered==[{k:p[k] for k in ['document_id','title','url','text']} for p in evidence]
                assert coverage and any(r['kind']=='final_prompt_coverage' and r['all_evidence_covered'] and r['prompt_sha256']==prompt_sha for r in rows)
                draft=[r for r in rows if r['kind']=='model_call_started' and r['schema']=='CannedResponseDraftSchema'];assert len(draft)==1 and draft[0]['prompt_sha256']==prompt_sha
                assert sha(RESULT/'attempts'/attempt/'prompts'/f"{draft[0]['call_id']}.txt")==prompt_sha
            citations=re.findall(r'\bibmcld_\d+-\d+-\d+\b',answer) if answer!='unavailable' else []
            mentions=re.findall(r'https?://[^\s<>\)\]]+',answer) if answer!='unavailable' else []
            detail={'task_id':tid,'conversation_id':target['conversation_id'],'candidate':arm,'attempt_id':attempt,'status':out.get('status','not_started'),'query':frozen['query'],'runtime_input':frozen['task'],'original_top5':frozen['top5'],'actual_top5':evidence,'top5_order_text_unchanged':evidence==frozen['top5'],'answer':answer,'reference_answers_scoring_only':refs[tid],'retrieval_metrics_reused':retrieval[tid]['metrics'],'gold_ids_scoring_only':sorted(gold),'gold_missing_from_top5':sorted(gold-ids),'lexical_f1':max((f1(answer,r) for r in refs[tid]),default=0) if answer!='unavailable' else 'unavailable','rouge_l_f1':max((rouge(answer,r) for r in refs[tid]),default=0) if answer!='unavailable' else 'unavailable','answer_length_chars':len(answer) if answer!='unavailable' else 'unavailable','answer_length_words':len(r0.tokens(answer)) if answer!='unavailable' else 'unavailable','citations':citations,'citation_mentions':len(citations),'unique_citation_ids':len(set(citations)),'citation_ids_from_top5':{c:c in ids for c in set(citations)},'source_url_mentions':len(mentions),'source_urls':mentions,'url_mentions_match_top5':{u:u.rstrip('.,') in urls for u in set(mentions)},'evidence_in_final_prompt':coverage,'independent_original_prompt_reparse':coverage,'control_original_native_prompt_unchanged':native_unchanged,'final_prompt_sha256':prompt_sha,'native_prompt_sha256':native_sha,'seconds':out.get('seconds','unavailable'),'usage':usage_group(rows,price),'human_unsupported_fact_label':'unreviewed','factual_accuracy':'unavailable; no LLM judge or human correctness score'}
            details.append(detail);pair[arm]=detail
        pairs.append(pair)
    jsonl(RESULT/'answer_details.jsonl',details);jsonl(RESULT/'CONTROL_vs_R2_answers.jsonl',pairs)
    metrics={}
    for arm in ARMS:
        rows=[x for x in details if x['candidate']==arm and x['answer']!='unavailable']
        fields=['lexical_f1','rouge_l_f1','answer_length_chars','answer_length_words','citation_mentions','unique_citation_ids','source_url_mentions']
        metrics[arm]={'completed':len(rows),'macro':{k:sum(x[k] for x in rows)/len(rows) if rows else 'unavailable' for k in fields},'prompt_evidence_coverage':sum(x['evidence_in_final_prompt'] for x in rows),'citation_ID_all_from_top5':sum(all(x['citation_ids_from_top5'].values()) for x in rows),'answers_with_citations':sum(bool(x['citations']) for x in rows),'human_unsupported_fact_labels':'unreviewed','not_factual_accuracy':True}
    write(REVIEW/'GENERATION_METRICS.json',metrics)
    formal={x['attempt_id'] for x in outcomes.values()}
    usage={'startup':usage_group([r for r in observations if r['attempt_id']=='startup'],price),'formal':{arm:usage_group([r for r in observations if r['attempt_id'] in {x['attempt_id'] for x in outcomes.values() if x['candidate']==arm}],price) for arm in ARMS},'abnormal':usage_group([r for r in observations if r['attempt_id'] not in formal|{'startup'}],price),'total':usage_group(observations,price),'offline_memory_mock_excluded':True}
    write(REVIEW/'USAGE.json',usage)
    sampling=random.Random(42).sample(range(11),4);write(REVIEW/'HUMAN_SAMPLE_SELECTION.json',{'seed':42,'method':'Random(42).sample(range(11),4), indices in original target order; not based on outcomes','sample_indices_0_based':sampling,'task_ids':[selection[i]['task_id'] for i in sampling]})
    human=[{'task_id':pairs[i]['task_id'],'candidate':arm,'query':pairs[i][arm]['query'],'history':pairs[i][arm]['runtime_input']['input'][:-1],'answer':pairs[i][arm]['answer'],'actual_top5':pairs[i][arm]['actual_top5'],'unsupported_new_fact':'unreviewed','human_reviewer':'unavailable','claim_quote':'','evidence_explanation':''} for i in sampling for arm in ARMS]
    jsonl(RESULT/'human_annotation_sample.jsonl',human)
    write(REVIEW/'SCORING_ISOLATION.json',{'labels_read_only_after_service_stop':True,'reference_qrels_not_in_runtime':True,'retrieval_metrics_copied_not_recomputed':True,'human_labels_not_fabricated':True,'human_samples':8,'same_lexical_functions_as_R0':True})
    print('ANALYZED',len(details),'answers',metrics,flush=True)
if __name__=='__main__':main()
