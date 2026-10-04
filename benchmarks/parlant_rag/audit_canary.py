"""Correct audit classification using immutable prompts actually sent to DeepSeek; no rerun."""
from common import *
from coverage import checks
import shutil

def main():
    path=RESULT/'smoke_status_after_audit_failure.json';state=json.loads(path.read_text());assert len(state['outcomes'])==2
    validations=[];corrected=[]
    for index,out in enumerate(state['outcomes']):
        d=RESULT/'attempts'/out['attempt_id'];rows=readl(d/'observations.jsonl');call=next(x for x in rows if x['kind']=='model_call_started' and x.get('schema')=='CannedResponseDraftSchema');prompt=(d/'prompts'/(call['call_id']+'.txt')).read_text();assert sha(d/'prompts'/(call['call_id']+'.txt'))==call['prompt_sha256']
        provider=next(x for x in rows if x['kind']=='provider_request_started' and x['call_id']==call['call_id']);assert provider['messages_sha256']==digest([{'role':'user','content':prompt}])
        proof,rendered=checks(prompt,out['actual_evidence']);assert rendered==out['actual_evidence'] and len(rendered)==5
        assert out['generated_message_count']==1 and out['target_trigger_count']==1
        validations.append({'attempt_id':out['attempt_id'],'model_prompt_sha256_verified':True,'provider_sent_prompt_sha256_verified':True,'exact_full_five_passages_in_final_prompt':True,'passages':proof,'parse_method':'ast.literal_eval native staged list, then json.loads each event string'})
        record=dict(out);record['original_audit_status']=out['status'];record['status']='completed' if index==0 else 'audit_recovery_extra_answer';record['evidence_coverage_verified']=True;record['coverage_audit']='retrospective exact rendered payload against actual sent prompt';record.pop('error_type');record.pop('error');corrected.append(record)
    write(REVIEW/'CANARY_AUDIT_CORRECTION.json',{'cause':'observer hooked MessageGenerator, default SDK composition used CannedResponseGenerator._build_draft_prompt; missing observation wrongly triggered one recovery although an answer had already returned','formal_choice':'first chronological attempt, not best answer; second extra generation excluded from formal and counted abnormal expense','original_status_sha256':sha(path),'original_outcome_hashes':{x['attempt_id']:sha(RESULT/'attempts'/x['attempt_id']/'outcome.json') for x in state['outcomes']},'validations':validations})
    state['outcomes']=corrected;state['canary_audit_corrected']=True;state['initializations']=1;write(RESULT/'smoke_status.json',state)
    print('both sent prompts contain exact five passages; first original answer is formal; no paid call')
if __name__=='__main__':main()
