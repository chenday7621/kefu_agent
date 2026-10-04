"""Freeze original sources and copy only the fixed selected R1-B evidence payloads."""
from common_r2 import *
import subprocess,shutil,difflib
def main():
    assert not (REVIEW/'ASSETS_FROZEN.json').exists()
    roots=[R0,R1,HOLDOUT,R0_RESULT,PROJECT/'results/mtrag_cloud_r1_20261004_021350',OLD_RESULT,PROJECT/'_reviews/20261004_012144_RAG_R0_CLOUD',PROJECT/'_reviews/20261004_021350_RAG_R1_RETRIEVAL',OLD_REVIEW,PROJECT/'src']
    before={str(p.relative_to(PROJECT)):sha(p) for root in roots for p in sorted(root.rglob('*')) if p.is_file()}
    for p in [PROJECT/'pyproject.toml',PROJECT/'uv.lock']:before[str(p.relative_to(PROJECT))]=sha(p)
    write(REVIEW/'PRESERVATION_BEFORE.json',before)
    shutil.copyfile(PROJECT/'REPRODUCE.md',REVIEW/'REPRODUCE_BEFORE.md')
    durable_text(REVIEW/'DEPENDENCIES_BEFORE.txt',subprocess.check_output([str(PROJECT/'.venv/bin/python'),'-m','pip','freeze'],text=True))
    for name in ['ASSETS_FROZEN.json','RETRIEVAL_FROZEN.json','GENERATION_FROZEN.json','RESUME_FROZEN.json']:
        for p,h in json.loads((OLD_REVIEW/name).read_text())['files'].items():assert sha(PROJECT/p)==h,p
    selection=json.loads((OLD_RESULT/'selection.json').read_text());targets=selection['targets']
    assert len(targets)==11 and len({x['conversation_id'] for x in targets})==11
    shutil.copyfile(OLD_RESULT/'selection.json',RESULT/'holdout_selection_original.json')
    newselection={'targets':targets,'planned':22,'arms':['CONTROL','R2-EVIDENCE'],'order':'CONTROL first for odd ordinal targets; R2 first for even','seed_from_original':42,'fresh_CONTROL_authorized_by_user':True,'original_R1B_answers_never_overwritten':True,'max_attempts_per_arm_target':1,'stop_on_infrastructure_failure_no_extra_attempt':True}
    write(RESULT/'selection.json',newselection)
    payload={x['task']['task_id']:x for x in readl(OLD_RESULT/'selected_retrieval.jsonl') if x['candidate']=='R1-B'}
    assert set(payload)=={x['task_id'] for x in targets};runtime=[];new=[];proof=[]
    for t in targets:
        x=payload[t['task_id']];assert x['query']==x['task']['input'][-1]['text'] and x['top5']==x['top10'][:5]
        assert len(x['top5'])==5
        runtime.append(x['task']);proof.append({'task_id':t['task_id'],'Top5_payload_sha256':digest(x['top5']),'query_sha256':digest(x['query']),'input_sha256':digest(x['task']['input'])})
        for arm in newselection['arms']:new.append({**x,'candidate':arm,'retrieval_candidate':'R1-B'})
    jsonl(RESULT/'runtime_targets.jsonl',runtime);jsonl(RESULT/'selected_retrieval.jsonl',new);write(REVIEW/'TOP5_IDENTICAL.json',proof)
    assets=json.loads((OLD_REVIEW/'ASSETS_FROZEN.json').read_text())['files']
    assets.update({str(p.relative_to(PROJECT)):sha(p) for p in [OLD_RESULT/'holdout_query_overlay.npy',OLD_RESULT/'holdout_query_overlay.json',OLD_RESULT/'selection.json',OLD_RESULT/'selected_retrieval.jsonl']})
    assets.update({str(p.relative_to(PROJECT)):sha(p) for p in [RESULT/'selection.json',RESULT/'holdout_selection_original.json',RESULT/'runtime_targets.jsonl',RESULT/'selected_retrieval.jsonl']})
    write(REVIEW/'ASSETS_FROZEN.json',{'files':assets,'model_corpus_index_cache_unchanged':True,'retrieval_not_run_or_recomputed':True,'new_encoding':0,'no_DEV_or_full_MTRAG_test':True})
    configs={'CONTROL':{'generation_evidence':'native Parlant STAGED_EVENTS Python/JSON tool event','retrieval':'frozen R1-B Top5','rules':'original three guidelines','model':'deepseek-chat','composition':'CANNED_FLUID','policy':'BasicOptimizationPolicy'},'R2-EVIDENCE':{'generation_evidence':'labeled Evidence1..5 + verbatim official history/current question + requested four generation instructions; replace STAGED_EVENTS only','retrieval':'frozen R1-B Top5','rules':'original three guidelines','model':'deepseek-chat','composition':'CANNED_FLUID','policy':'BasicOptimizationPolicy'}}
    write(REVIEW/'ARM_CONFIGS.json',configs)
    durable_text(REVIEW/'CONTROL_R2_CONFIG.diff',''.join(difflib.unified_diff(json.dumps(configs['CONTROL'],indent=2).splitlines(True),json.dumps(configs['R2-EVIDENCE'],indent=2).splitlines(True),fromfile='CONTROL',tofile='R2-EVIDENCE')))
    write(REVIEW/'PROTOCOL.json',{'only_generation_stage_evidence_section_modified':True,'query_order_text_Top5_unchanged':True,'history_unchanged':True,'wrapper_duplicates_history_to_follow_requested_layout':True,'four_requested_instructions_are_prompt_only_not_new_guidelines':True,'references_qrels_answerability_absent_from_runtime':True,'no_query_rewrite_reranker_hybrid_retrieval_change':True,'max_new_generation_attempts':22,'per_attempt_seconds':180,'same_native_configuration':True,'no_LLM_judge':True,'no_response_rewrite_or_fact_correction':True,'human_review':'manual human labels unavailable unless supplied; agent qualitative inspection explicitly identified, not a human factual judge'})
    print('FROZEN 11 original targets, identical BGE Top5 for both arms',flush=True)
if __name__=='__main__':main()
