"""Create a new evaluation's private config; never overwrite a previous run."""
import json,secrets,subprocess
from pathlib import Path
from datetime import datetime,timezone
from .common import save,digest


def main():
 stamp=datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
 r=Path('results')/('app_s1_confirmed_snapshot_'+stamp);r.mkdir()
 audit=Path('_reviews')/(stamp+'_APP_S1_CONFIRMED_SNAPSHOT');audit.mkdir()
 private=Path('runtime-data/retail-demo/app_s1')/stamp;private.mkdir(parents=True);private.chmod(0o700)
 c={'stamp':stamp,'result_dir':str(r.resolve()),'audit_dir':str(audit.resolve()),'runtime_dir':str(private.resolve()),'container':'app-s1-'+stamp.replace('_','-'),'volume':'app-s1-'+stamp.replace('_','-')+'-pgdata','database':'app_s1','user':'app_s1','password':secrets.token_urlsafe(24),'db_port':55434,'parlant_port':8920,'mcp_port':8921,'tool_port':8922,'budget_usd':2.0}
 save(private/'private.json',c);(private/'private.json').chmod(0o600)
 (private/'postgres.env').write_text('POSTGRES_DB=app_s1\nPOSTGRES_USER=app_s1\nPOSTGRES_PASSWORD='+c['password']+'\n');(private/'postgres.env').chmod(0o600)
 Path('runtime-data/retail-demo/app_s1/latest.txt').write_text(str(private.resolve()))
 files=[p for p in Path('apps/retail_demo').rglob('*') if p.is_file() and not any(s in p.parts for s in ('.venv','__pycache__','eval_s1')) and p.name!='.env']
 save(r/'source_baseline.json',{str(p):digest(p) for p in files})
 (r/'git_before.txt').write_text(subprocess.check_output(['git','status','--short'],text=True)+subprocess.check_output(['git','rev-parse','HEAD'],text=True))
 process=Path('runtime-data/retail-demo/services/processes.json')
 if process.exists():(r/'demo_processes_before.json').write_bytes(process.read_bytes())
 print('New isolated evaluation config:',private/'private.json')

if __name__=='__main__':main()
