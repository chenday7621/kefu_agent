"""Freeze once after successful finite offline checks, before any paid call."""
from pathlib import Path
import json,difflib
from datetime import datetime,timezone
from .common import config,save,digest

def main():
 r=Path(config()['result_dir']);assert json.loads((r/'offline/report.json').read_text())['passed']
 assert json.loads((r/'offline_live_checks/report.json').read_text())['passed']
 assert not list((r/'calls').glob('*.json')) if (r/'calls').exists() else True
 files=list(Path('apps/retail_demo/eval_s1').glob('*.py'))+list(Path('apps/retail_demo/eval_v1').glob('*.py'))+list(Path('apps/retail_demo').glob('*.py'))+list(Path('apps/retail_demo/migrations').glob('*.sql'))+[Path('src/parlant/adapters/nlp/deepseek_service.py'),Path('src/parlant/sdk.py'),Path('pyproject.toml')]
 assert not (r/'PAID_FREEZE.json').exists(),'Freeze already saved'
 hashes={str(p):digest(p) for p in files};save(r/'protocol_source_hashes.json',hashes)
 before=json.loads((r/'source_before.json').read_text());changed={p:{'before':h,'after':digest(p)} for p,h in before.items() if digest(p)!=h}
 assert not any('/eval_v1/' in p or '/migrations/' in p for p in changed),'Frozen V1 or applied migration changed'
 lines=[]
 for p in changed:
  orig=r/'source_before'/p
  lines.extend(difflib.unified_diff(orig.read_text().splitlines(True),Path(p).read_text().splitlines(True),fromfile='before/'+p,tofile='after/'+p))
 new=Path('apps/retail_demo/migrations/005_confirmed_snapshot.sql')
 lines.extend(difflib.unified_diff([],new.read_text().splitlines(True),fromfile='/dev/null',tofile=str(new)))
 (r/'APP_S1_MINIMAL.diff').write_text(''.join(lines))
 for p in files:
  target=r/'frozen_source'/p;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(p.read_bytes())
 save(r/'PAID_FREEZE.json',{'utc':datetime.now(timezone.utc).isoformat(),'source_hashes_sha256':digest(r/'protocol_source_hashes.json'),'old_scorer_sha256':digest('apps/retail_demo/eval_v1/scoring.py'),'observation_adapter_sha256':digest('apps/retail_demo/eval_s1/scoring.py'),'changed_target_sources':changed,'new_migration':{str(new):digest(new)},'scenario_count':8,'attempt_count':16,'plan_sha256':digest(r/'plan.json'),'scenario_sha256':digest(r/'scenarios.json'),'effective_metadata_sha256':digest(r/'effective_metadata.json'),'budget_usd':2,'prepaid_calls':0,'criteria_unchanged':True})
 print('APP-S1 frozen; scorer unchanged; 16 fixed tasks.')

if __name__=='__main__':main()
