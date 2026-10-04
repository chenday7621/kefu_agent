from common_holdout import *
def main():
    assert json.loads((REVIEW/'OFFLINE_RUNTIME_CHECKS.json').read_text())['passed']
    assert not (REVIEW/'GENERATION_FROZEN.json').exists()
    files=[p for p in BASE.rglob('*') if p.is_file() and '__pycache__' not in p.parts]
    files.extend(RESULT/p for p in ['selection.json','runtime_holdout.jsonl','selected_retrieval.jsonl'])
    write(REVIEW/'GENERATION_FROZEN.json',{'files':{str(p.relative_to(PROJECT)):sha(p) for p in files},'target_count':11,'max_new_answers':22,'one_per_conversation':True,'R0_and_R1B_current_question_only':True,'paired_order':'R0 then R1-B on odd ordinal samples; reversed on even','same_effective_agent_guidelines_and_metadata':True,'same_basic_policy':True,'new_rule_evaluations':0,'max_seconds_per_attempt':180,'max_infrastructure_recoveries':1,'no_retry_after_completed_answer':True,'only_retriever_payload_changes':True,'reference_answers_not_yet_read':True,'provider':'DeepSeek official, root .env only','judge':False,'Agent_Plan':False,'no_DEV':True,'no_tuning_after_HOLDOUT':True})
    print('22-answer generation frozen',flush=True)
if __name__=='__main__':main()
