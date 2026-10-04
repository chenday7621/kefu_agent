"""No APIs: train sampling, paired keys, snapshot config and score recovery."""
import json
import random
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from protocol_support import HERE, PROJECT, PLAN, PROTOCOL, score_path, scored

def main():
    source=json.loads((PROJECT/'_reviews/20261002_202338_DAY1_B0_DEV30/selection.json').read_text())
    assert PROTOCOL['task_ids']==random.Random(42).sample(source['task_ids'],10)
    assert PROTOCOL['split']=='train' and len(PLAN)==20
    for group in ('CONTROL','C1'):
        assert (HERE/'configs'/group/'policy_config.py').read_bytes()==(PROJECT/'benchmarks/parlant_retail_o1b/policy_config.py').read_bytes()
    assert [u['order'] for u in PLAN]==list(range(1,21)) and len({u['unit_id'] for u in PLAN})==20
    for i,tid in enumerate(PROTOCOL['task_ids']):
        a,b=PLAN[2*i:2*i+2]
        assert {a['group'],b['group']}=={'C1','CONTROL'} and a['task_id']==b['task_id']==tid and a['seed']==b['seed']==42
        assert a['group']==('C1' if i%2==0 else 'CONTROL')
    with TemporaryDirectory() as d,patch('protocol_support.sha256',return_value='fake'):
        root=Path(d);unit=PLAN[0];p=score_path(root,unit);p.parent.mkdir()
        p.write_text(json.dumps({'unit':unit,'freeze_sha256':'fake','protocol':PROTOCOL,'simulation':{'reward_info':{'reward':0}}}))
        assert scored(root,unit) and not scored(root,PLAN[1])
        x=json.loads(p.read_text());x['freeze_sha256']='wrong';p.write_text(json.dumps(x))
        try:scored(root,unit)
        except RuntimeError:pass
        else:raise AssertionError('Wrong hash accepted')
        x['freeze_sha256']='fake';x['simulation']['reward_info']=None;p.write_text(json.dumps(x))
        try:scored(root,unit)
        except RuntimeError:pass
        else:raise AssertionError('Unscored execution skipped')
    assert (HERE/'observe.py').read_bytes()==(PROJECT/'benchmarks/parlant_retail_heldout/observe.py').read_bytes()
    print(json.dumps({'status':'PASS','fixed_train_sample':True,'balanced_20_plan':True,'both_business_configs_O1B_byte_identical':True,'recovery_group_task_keys_isolated':True,'score_zero_skipped':True,'incomplete_or_wrong_hash_rejected':True,'observer_preserved':True,'paid_model_calls':0},indent=2))

if __name__=='__main__':main()
