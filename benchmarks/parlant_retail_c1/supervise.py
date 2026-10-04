"""Sequential C1 paired plan; isolated CONTROL/C1 workers."""
import argparse
from datetime import datetime, timezone
from collections import Counter
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from uuid import uuid4
from protocol_support import PROJECT, HERE, PROTOCOL, PLAN, FREEZE, verify, scored, unit_root, score_path, sha256


def append(path,row):
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('a') as f:
        f.write(json.dumps({'time_utc':datetime.now(timezone.utc).isoformat(),**row})+'\n');f.flush();os.fsync(f.fileno())


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--result-dir',type=Path,required=True)
    args=parser.parse_args();root=args.result_dir.resolve()
    if not root.is_relative_to(PROJECT/'results') or not root.name.startswith('tau3_retail_c1_pilot10_'):raise RuntimeError('Independent C1 directory required')
    freeze_sha=verify();root.mkdir(parents=True,exist_ok=True)
    # Prevent concurrent supervisors and duplicate paid execution. Linux lock releases on crash.
    import fcntl
    lock=(root/'supervision.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    journal=root/'supervision/events.jsonl';workers={}
    prior=[json.loads(l) for l in journal.read_text().splitlines()] if journal.exists() else []
    native_count=sum(x.get('native_crash',False) for x in prior)
    schema_failure_categories=Counter(cat for x in prior if x.get("event")=="unit_scored" for cat in x.get("schema_failure_categories",[]))
    if native_count>=2:raise RuntimeError('Repeated native crash: manual versioned repair required')
    # Reject stray scored results, including results from a different repeat/group.
    expected={score_path(root,u) for u in PLAN}
    for p in root.glob('*/task_*.json'):
        if p not in expected:raise RuntimeError('Unexpected completed result '+str(p))
    for u in PLAN:scored(root,u)
    provenance=root/'provenance.json'
    if provenance.exists():
        if json.loads(provenance.read_text())['freeze_sha256']!=freeze_sha:raise RuntimeError('Result directory belongs to another freeze')
    else:provenance.write_text(json.dumps({'freeze_sha256':freeze_sha,'protocol':PROTOCOL,'plan':PLAN},indent=2)+'\n')
    def launch(group):
        invocation=f'{group}_{datetime.now().strftime("%Y%m%d_%H%M%S")}_{uuid4().hex}'
        control=root/'supervision'/invocation;control.mkdir(parents=True)
        out=(control/'stdout.log').open('x');err=(control/'stderr.log').open('x')
        env=os.environ.copy();env.update({'OMP_NUM_THREADS':'2','MKL_NUM_THREADS':'2','PYTHONFAULTHANDLER':'1','PYTHONUNBUFFERED':'1'})
        cmd=[sys.executable,'-u',str(HERE/'run.py'),'--group',group,'--result-dir',str(root),'--control-dir',str(control)]
        p=subprocess.Popen(cmd,stdin=subprocess.PIPE,stdout=out,stderr=err,text=True,env=env,start_new_session=True)
        worker={'p':p,'control':control,'out':out,'err':err,'group':group};workers[group]=worker
        append(journal,{'event':'worker_started','group':group,'pid':p.pid,'invocation':invocation,'command':cmd,'freeze_sha256':freeze_sha})
        deadline=time.monotonic()+660
        while not (control/'ready.json').exists():
            if p.poll() is not None:raise RuntimeError(f'{group} worker failed during startup')
            if time.monotonic()>deadline:raise RuntimeError(f'{group} startup deadline')
            time.sleep(.5)
        print(f'{group} ready: actual frozen rules checked',flush=True)
        return worker
    def stop(group):
        nonlocal native_count
        w=workers.pop(group,None)
        if w is None:return
        p=w['p']
        if p.poll() is None:
            try:p.stdin.write('{"stop":true}\n');p.stdin.flush()
            except (BrokenPipeError,OSError):pass
            try:p.wait(timeout=45)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid,signal.SIGTERM)
                try:p.wait(timeout=10)
                except subprocess.TimeoutExpired:os.killpg(p.pid,signal.SIGKILL);p.wait()
        w['out'].close();w['err'].close()
        stderr=(w['control']/'stderr.log').read_text(errors='replace')
        native=p.returncode in (-6,134,-11,139) or any(x in stderr for x in ('realloc(): invalid old size','Fatal Python error: Aborted','Fatal Python error: Segmentation fault'))
        native_count+=int(native)
        append(journal,{'event':'worker_exited','group':group,'pid':p.pid,'returncode':p.returncode,'signal':-p.returncode if p.returncode<0 else None,'native_crash':native,'stderr':str((w['control']/'stderr.log').relative_to(root))})
    try:
        for unit in PLAN:
            if scored(root,unit):continue
            while not scored(root,unit):
                attempt_root=unit_root(root,unit)/'attempts'/f"task_{unit['task_id']}"
                attempts=list(attempt_root.glob('*/'))
                if len(attempts)>=2:
                    append(journal,{'event':'recovery_limit_reached','unit':unit,'attempts':len(attempts)})
                    return 2
                group=unit['group'];w=workers.get(group) or launch(group)
                if w['p'].poll() is not None:stop(group);w=launch(group)
                append(journal,{'event':'unit_dispatched','unit':unit,'worker_pid':w['p'].pid,'prior_attempts':len(attempts)})
                w['p'].stdin.write(json.dumps({'unit':unit})+'\n');w['p'].stdin.flush()
                deadline=time.monotonic()+PROTOCOL['timeout_seconds_per_task']+360
                ack=w['control']/(unit['unit_id']+'.ack.json')
                while not ack.exists() and w['p'].poll() is None and time.monotonic()<deadline:time.sleep(.5)
                if ack.exists():
                    assert scored(root,unit)
                    row=json.loads(score_path(root,unit).read_text());reward=row['simulation']['reward_info']['reward']
                    ack_row=json.loads(ack.read_text()); schema_failure_categories.update(ack_row.get("schema_failure_categories",[]))
                    append(journal,{'event':'unit_scored','unit':unit,'reward':reward,'result_sha256':sha256(score_path(root,unit)),'schema_failure_count':ack_row.get('schema_failure_count',0),'schema_failure_categories':ack_row.get('schema_failure_categories',[])})
                    if ack_row.get('pause_persistent_schema') or max(schema_failure_categories.values(),default=0)>=2:
                        append(journal,{'event':'paused_persistent_schema','unit':unit});return 5
                    print(f"[{unit['order']}/20] {unit['unit_id']} reward={reward} seconds={row['elapsed_seconds']:.1f}",flush=True)
                    break
                # Scored units never rerun, even if a worker dies before its acknowledgement.
                stop(group)
                append(journal,{'event':'unit_infrastructure_interruption','unit':unit,'scored':scored(root,unit),'native_crashes_seen':native_count})
                if native_count>=2:return 3
                error_paths=sorted(attempt_root.glob('*/error.json'))
                stderr_text=(w['control']/'stderr.log').read_text(errors='replace')
                errors=[json.loads(p.read_text()).get('error','') for p in error_paths]
                if any(x in (' '.join(errors)+' '+stderr_text) for x in ('Insufficient Balance','insufficient balance','InsufficientBalance','Error code: 402')):
                    append(journal,{'event':'paused_balance','unit':unit});return 6
                if scored(root,unit):break
                if len(list(attempt_root.glob('*/')))==len(attempts):
                    # No task attempt entered: cannot loop an adapter/startup fault indefinitely.
                    return 4
        append(journal,{'event':'all_planned_units_scored','count':len(PLAN)})
        return 0
    finally:
        for group in list(workers):stop(group)


if __name__=='__main__':sys.exit(main())
