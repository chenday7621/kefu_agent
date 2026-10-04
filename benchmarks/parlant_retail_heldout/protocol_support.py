"""Frozen heldout keys and read-only integrity verification (no task bodies)."""
import hashlib
import json
import os
from pathlib import Path
import subprocess

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[1]
PATHS = json.loads((HERE / 'paths.json').read_text())
REVIEW = PROJECT / PATHS['review']
FREEZE = REVIEW / 'HELDOUT_FROZEN.json'
PROTOCOL = json.loads((HERE / 'protocol.json').read_text())
PLAN = json.loads((HERE / 'execution_plan.json').read_text())


def sha256(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(1024*1024), b''): h.update(block)
    return h.hexdigest()


def unit_root(result_dir, unit):
    return Path(result_dir) / unit['group'] / f"repeat_{unit['repeat_id']}"


def score_path(result_dir, unit):
    return unit_root(result_dir, unit) / f"task_{unit['task_id']}.json"


def scored(result_dir, unit):
    p=score_path(result_dir,unit)
    if not p.exists():return False
    r=json.loads(p.read_text())
    if r.get('unit') != unit or r.get('freeze_sha256') != sha256(FREEZE) or r.get('protocol') != PROTOCOL:
        raise RuntimeError(f'Existing score has mismatched frozen key/hash/protocol: {p}')
    if (r.get('simulation') or {}).get('reward_info') is None:
        raise RuntimeError(f'Incomplete score at completed path: {p}')
    return True


def verify():
    f=json.loads(FREEZE.read_text())
    for p,h in f['code_data_config_sha256'].items():
        if sha256(PROJECT/p)!=h:raise RuntimeError(f'Frozen file changed: {p}')
    for name,rel in [('parlant','.venv/bin/python'),('tau2','benchmarks/tau2-bench/.venv/bin/python')]:
        b=subprocess.check_output(['uv','pip','freeze','--python',str(PROJECT/rel)])
        if hashlib.sha256(b).hexdigest()!=f['package_freeze_sha256'][name]:raise RuntimeError('Dependency mismatch '+name)
    for name,cwd in [('parlant',PROJECT),('benchmark',PROJECT/'benchmarks/tau2-bench')]:
        h=subprocess.check_output(['git','rev-parse','HEAD'],cwd=cwd,text=True).strip()
        if h!=f[name+'_head']:raise RuntimeError('HEAD mismatch '+name)
    for k in ('OMP_NUM_THREADS','MKL_NUM_THREADS'):
        if os.environ.get(k,'2')!='2':raise RuntimeError('Frozen thread configuration requires '+k+'=2')
    assert f['protocol']==PROTOCOL and f['execution_plan']==PLAN
    return sha256(FREEZE)
