"""No model calls: dual configuration/plan/key isolation and immutable source checks."""
import hashlib
import importlib.util
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from protocol_support import HERE, PLAN, PROTOCOL, PROJECT, scored, unit_root, score_path


def load(path,name):
    spec=importlib.util.spec_from_file_location(name,path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


def main():
    base=load(HERE/'configs/BASE/policy_config.py','base_policy')
    opt=load(HERE/'configs/OPT/policy_config.py','opt_policy')
    oldbase=PROJECT/'benchmarks/parlant_retail_b0_r1/policy_config.py'
    oldopt=PROJECT/'benchmarks/parlant_retail_o1b/policy_config.py'
    assert (HERE/'configs/BASE/policy_config.py').read_bytes()==oldbase.read_bytes()
    assert (HERE/'configs/OPT/policy_config.py').read_bytes()==oldopt.read_bytes()
    assert len(base.ASSOCIATIONS)==5 and len(opt.ASSOCIATIONS)==6
    assert 'operation_target_scope' not in [x['key'] for x in base.ASSOCIATIONS]
    assert len(PLAN)==160 and len({u['unit_id'] for u in PLAN})==160
    assert [u['order'] for u in PLAN]==list(range(1,161))
    for repeat,seed in [(1,42),(2,43)]:
        rows=[u for u in PLAN if u['repeat_id']==repeat];assert len(rows)==80
        first=[rows[i]['group'] for i in range(0,80,2)];assert first.count('BASE')==first.count('OPT')==20
        for i,tid in enumerate(PROTOCOL['task_ids']):
            pair=rows[2*i:2*i+2];assert {x['group'] for x in pair}=={'BASE','OPT'}
            assert all(x['task_id']==tid and x['seed']==seed for x in pair)
    for i in range(0,80,2):assert PLAN[i]['group']!=PLAN[i+80]['group']
    units=[PLAN[0],PLAN[1],PLAN[80]]
    with TemporaryDirectory() as d,patch('protocol_support.sha256',return_value='fakefreeze'):
        root=Path(d);u=units[0];p=score_path(root,u);p.parent.mkdir(parents=True)
        p.write_text(json.dumps({'unit':u,'freeze_sha256':'fakefreeze','protocol':PROTOCOL,'simulation':{'reward_info':{'reward':0}}}))
        assert scored(root,u)
        assert not scored(root,units[1]) and not scored(root,units[2])
        x=json.loads(p.read_text());x['freeze_sha256']='different';p.write_text(json.dumps(x))
        try:scored(root,u)
        except RuntimeError:pass
        else:raise AssertionError('Wrong freeze accepted')
        x['freeze_sha256']='fakefreeze';x['simulation']['reward_info']=None;p.write_text(json.dumps(x))
        try:scored(root,u)
        except RuntimeError:pass
        else:raise AssertionError('Unscored attempt skipped')
    assert len({str(unit_root('/results',u)) for u in PLAN})==4
    assert (HERE/'observe.py').read_bytes()==(PROJECT/'benchmarks/parlant_retail_o1b/observe.py').read_bytes()
    print(json.dumps({'status':'passed','business_config_byte_identical_to_snapshots':True,'base_associations':5,'opt_associations':6,'observer_byte_identical_to_O1B':True,'balanced_160_plan':True,'recovery_group_task_repeat_isolation':True,'scored_zero_skipped':True,'wrong_hash_and_missing_score_rejected':True,'model_selection_ability_tested':False},indent=2))

if __name__=='__main__':main()
