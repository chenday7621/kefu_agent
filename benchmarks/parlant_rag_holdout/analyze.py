"""Post-generation scoring only. Reference text cannot flow back to runtime."""
from common_holdout import *
from project_json import project,members,array_values
from metrics_utils import f1,rouge,usage_group,read_obs
import re,collections
def main():
    state=json.loads((RESULT/'smoke_status.json').read_text());assert state['service_stopped']
    selected=json.loads((RESULT/'selection.json').read_text())['targets'];want={t['task_id'] for t in selected}
    refs={}
    for line in (DATA/'official/mtrag-human/generation_tasks/reference.jsonl').open():
        route=project(line,{'task_id'})
        if route.get('task_id') not in want:continue
        for key,value in members(line):
            if key=='targets':refs[route['task_id']]=[project(x,{'text'})['text'] for x in array_values(value)]
    assert set(refs)==want
    write(RESULT/'reference_answers_scoring_only.json',refs)
    payload={(x['task']['task_id'],x['candidate']):x for x in readl(RESULT/'selected_retrieval.jsonl')}
    retrieval={c:{x['task_id']:x for x in readl(RESULT/f'{c}_per_task.jsonl')} for c in ['R0','R1-B']}
    outcomes={(x['task_id'],x['candidate']):x for x in state['outcomes'] if x['status'].startswith('completed')}
    rows=read_obs();rows=[r for r in rows if r['attempt_id']!='offline-checks'];price=json.loads((BASE/'pricing.json').read_text());details=[];pairs=[]
    for s in selected:
        tid=s['task_id'];pair={'task_id':tid,'conversation_id':s['conversation_id'],'reference_answers':refs[tid]}
        for c in ['R0','R1-B']:
            out=outcomes.get((tid,c),{});answer=out.get('answer','unavailable');attempt=out.get('attempt_id');inp=payload[(tid,c)]
            evidence=out.get('actual_evidence',[]);ids={x['document_id'] for x in evidence};citations=re.findall(r'\bibmcld_\d+-\d+-\d+\b',answer) if answer!='unavailable' else []
            observed=[r for r in rows if r['attempt_id']==attempt] if attempt else []
            metrics=retrieval[c][tid]['metrics'];gold=set(retrieval[c][tid]['gold_ids_scoring_only']);missing=sorted(gold-ids)
            detail={'task_id':tid,'conversation_id':s['conversation_id'],'candidate':c,'status':out.get('status','not_started'),'attempt_id':attempt,'runtime_input':inp['task'],'query':inp['query'],'top10':inp['top10'],'actual_top5':evidence,'retrieval_metrics':metrics,'gold_ids_scoring_only':sorted(gold),'gold_missing_from_top5':missing,'answer':answer,'reference_answers_scoring_only':refs[tid],'citations':citations,'citation_ids_in_actual_top5':{x:x in ids for x in set(citations)},'lexical_f1':max((f1(answer,r) for r in refs[tid]),default=0) if answer!='unavailable' else 'unavailable','rouge_l_f1':max((rouge(answer,r) for r in refs[tid]),default=0) if answer!='unavailable' else 'unavailable','evidence_in_final_prompt':out.get('evidence_coverage_verified','unavailable'),'prompt_coverage_observations':[r for r in observed if r['kind']=='final_prompt_coverage'],'seconds':out.get('seconds','unavailable'),'usage':usage_group(observed,price),'retrieval_failure_A':'unavailable' if not gold else 'all_gold_missing' if not gold&ids else 'partial_gold_missing' if missing else 'none_by_qrels','factual_correctness':'unavailable; no LLM judge or quantitative human correctness score'}
            prompt_path=RESULT/'attempts'/attempt/'final_generation_prompt.txt' if attempt else None
            detail['original_final_prompt_text_status']='available' if prompt_path and prompt_path.exists() and prompt_path.stat().st_size else 'unavailable: original file empty/missing after first-process interruption'
            detail['independent_original_prompt_reparse_available']=detail['original_final_prompt_text_status']=='available'
            details.append(detail);pair[c]=detail
        pairs.append(pair)
    jsonl(RESULT/'answer_details.jsonl',details);jsonl(RESULT/'R0_vs_R1B_answers.jsonl',pairs)
    auxiliary={}
    for c in ['R0','R1-B']:
        complete=[x for x in details if x['candidate']==c and x['answer']!='unavailable']
        auxiliary[c]={'completed':len(complete),**{k:sum(x[k] for x in complete)/len(complete) if complete else 'unavailable' for k in ['lexical_f1','rouge_l_f1']},'final_prompt_coverage_verified':sum(x['evidence_in_final_prompt'] is True for x in complete),'all_citation_ids_from_actual_top5':sum(all(x['citation_ids_in_actual_top5'].values()) for x in complete),'answers_with_citations':sum(bool(x['citations']) for x in complete),'all_gold_missing_top5':sum(x['retrieval_failure_A']=='all_gold_missing' for x in complete),'partial_gold_missing_top5':sum(x['retrieval_failure_A']=='partial_gold_missing' for x in complete),'qrels_unavailable':sum(x['retrieval_failure_A']=='unavailable' for x in complete)}
    write(REVIEW/'AUXILIARY_COMPARISON.json',{'groups':auxiliary,'metrics_are_auxiliary_not_factual_accuracy':True})
    formal={x['attempt_id'] for x in outcomes.values()};startup=[r for r in rows if r['attempt_id']=='startup'];abnormal=[r for r in rows if r['attempt_id'] not in formal|{'startup'}]
    usage={'startup':usage_group(startup,price),'abnormal':usage_group(abnormal,price),'formal':{c:usage_group([r for r in rows if r['attempt_id'] in {x['attempt_id'] for x in outcomes.values() if x['candidate']==c}],price) for c in ['R0','R1-B']},'total':usage_group(rows,price),'offline_mock_requests_excluded':True}
    write(REVIEW/'USAGE.json',usage)
    write(REVIEW/'ANSWER_SCORING_ISOLATION.json',{'references_read_after_service_stop':True,'reference_fields_decoded':['task_id','targets.text'],'answerability_gold_context_rewrite_not_decoded':True,'reference_answers_runtime_input':False,'reference_sha256':sha(RESULT/'reference_answers_scoring_only.json')})
    print('ANALYZED',len(details),'answers',auxiliary,flush=True)
if __name__=='__main__':main()
