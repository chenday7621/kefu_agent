from common_r2 import *
import datetime
def main():
    assert json.loads((REVIEW/'OFFLINE_RUNTIME_CHECKS.json').read_text())['passed']
    assert not (REVIEW/'GENERATION_FROZEN.json').exists()
    files=[p for p in BASE.rglob('*') if p.is_file() and '__pycache__' not in p.parts]
    files+=[RESULT/p for p in ['selection.json','holdout_selection_original.json','runtime_targets.jsonl','selected_retrieval.jsonl']]
    files += [REVIEW/p for p in ['ARM_CONFIGS.json','CONTROL_R2_CONFIG.diff','PROTOCOL.json','TOP5_IDENTICAL.json','OFFLINE_RUNTIME_CHECKS.json']]
    write(REVIEW/'GENERATION_FROZEN.json',{'files':{str(p.relative_to(PROJECT)):sha(p) for p in files},'freeze_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'user_confirmed_fresh_CONTROL_and_R2_11_each':True,'max_attempts':22,'one_attempt_per_arm_target':True,'only_native_draft_STAGED_EVENTS_replaced_for_R2':True,'original_R1B_retrieval_and_results_read_only':True,'same_retrieval_no_recalculation':True,'no_qrels_reference_answer_answerability_in_prompt':True,'no_new_rule_evaluation':True,'no_paid_preflight_or_judge':True})
    print('R2 GENERATION FROZEN: 22 attempts, no extra retries')
if __name__=='__main__':main()
