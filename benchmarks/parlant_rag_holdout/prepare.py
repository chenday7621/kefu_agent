"""Freeze original assets and random sample selection before retrieving/scoring."""
from common_holdout import *
from project_json import project,runtime_task
import random, subprocess, shutil
def main():
    assert not (RESULT/'selection.json').exists(), 'Already prepared; never overwrite'
    roots=[R0,R1,R0_RESULT,PROJECT/'results/mtrag_cloud_r1_20261004_021350',PROJECT/'_reviews/20261004_012144_RAG_R0_CLOUD',PROJECT/'_reviews/20261004_021350_RAG_R1_RETRIEVAL',PROJECT/'src']
    preservation={str(p.relative_to(PROJECT)):sha(p) for root in roots for p in sorted(root.rglob('*')) if p.is_file()}
    for p in [PROJECT/'pyproject.toml',PROJECT/'uv.lock']:preservation[str(p.relative_to(PROJECT))]=sha(p)
    if (REVIEW/'PRESERVATION_BEFORE.json').exists():assert preservation==json.loads((REVIEW/'PRESERVATION_BEFORE.json').read_text())
    else:write(REVIEW/'PRESERVATION_BEFORE.json',preservation)
    shutil.copyfile(PROJECT/'REPRODUCE.md',REVIEW/'REPRODUCE_BEFORE.md')
    deps=subprocess.run([str(PROJECT/'.venv/bin/python'),'-m','pip','freeze'],capture_output=True,text=True,check=True).stdout
    (REVIEW/'DEPENDENCIES_BEFORE.txt').write_text(deps)
    frozen=json.loads((PROJECT/'_reviews/20261004_021350_RAG_R1_RETRIEVAL/GENERATION_FROZEN.json').read_text())['files']
    # Only integrity checks; no DEV score/answer parsing.
    for name,value in frozen.items():assert sha(PROJECT/name)==value,name
    keys=[DATA/'split.json',DATA/'cloud_passages.jsonl',DATA/'bm25.pkl',R0/'config.json',R0_RESULT/'startup/effective_configuration.json',R1/'model_lock.json',R1/'protocol.json']
    keys+=list((R1/'model').rglob('*'))+list((R1/'index').rglob('*'))
    write(REVIEW/'ASSETS_FROZEN.json',{'files':{str(p.relative_to(PROJECT)):sha(p) for p in sorted(keys) if p.is_file()},'original_R1_freeze_verified':True,'runtime_fields':['speaker','text'],'new_query_cache':'separate HOLDOUT overlay, original query cache read-only','selected_candidate':'R1-B','no_DEV_evaluation_or_answer_analysis':True})
    split=json.loads((DATA/'split.json').read_text());holdout=set(split['HOLDOUT']);tasks=[]
    for line in (DATA/'official/mtrag-human/generation_tasks/reference.jsonl').open():
        route=project(line,{'conversation_id'})
        if route.get('conversation_id') not in holdout:continue
        x=runtime_task(line)
        assert set(x)=={'task_id','conversation_id','input'}
        assert all(set(m)=={'speaker','text'} and m['speaker'] in ['user','agent'] for m in x['input'])
        tasks.append(x)
    tasks.sort(key=lambda t:t['task_id']);assert len(tasks)==89 and len({t['task_id'] for t in tasks})==89
    assert {t['conversation_id'] for t in tasks}==holdout
    jsonl(RESULT/'runtime_holdout.jsonl',tasks)
    shuffled=tasks.copy();random.Random(42).shuffle(shuffled);seen=set();selected=[]
    for t in shuffled:
        if t['conversation_id'] in seen:continue
        seen.add(t['conversation_id']);selected.append({'task_id':t['task_id'],'conversation_id':t['conversation_id']})
    assert len(selected)==11
    selection={'seed':42,'method':'sort task_id; Random(42).shuffle; first target of each conversation','targets':selected,'planned_targets':11,'planned_answers':22,'user_approved':'11 targets, R0/R1-B once each, all 11 conversations','selected_before_retrieval_and_labels':True,'paired_order':'R0 first for even zero-based sample index, R1-B first for odd'}
    write(RESULT/'selection.json',selection)
    write(REVIEW/'INPUT_ISOLATION.json',{'task_count':89,'conversations':11,'top_level_decoded_fields':['conversation_id','task_id','input'],'excluded_values_skipped_without_json_decoding':['targets','enrichments','contexts','answerability','feedback','rewrite'],'official_rewrite_content_read':False,'runtime_sha256':sha(RESULT/'runtime_holdout.jsonl'),'selection_sha256':sha(RESULT/'selection.json'),'qrels_read':False,'reference_answers_read':False})
    print('FROZEN: 89 HOLDOUT tasks, 11 random targets, 22 paired answers',flush=True)
if __name__=='__main__':main()
