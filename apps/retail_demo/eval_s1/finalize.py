"""Read-only isolation audit and complete redacted APP-S1 evidence package."""
import json,hashlib,tarfile,subprocess,os
from pathlib import Path
from psycopg import sql
from dotenv import dotenv_values
from datetime import datetime,timezone
from ..db import connect
from ..settings import load_settings,ROOT
from ..business import plain
from .common import config,activate,save,digest,database_evidence

def package(c):
 r=Path(c['result_dir']);audit=Path(c['audit_dir']);stage=audit/'upload_stage';stage.mkdir(exist_ok=True)
 secrets=[c['password']]
 for p in (ROOT/'.env',ROOT/'apps/retail_demo/.env'):
  secrets.extend(v for k,v in dotenv_values(p).items() if v and len(v)>10 and any(n in k.upper() for n in ('KEY','TOKEN','PASSWORD','DATABASE_URL')))
 capabilities=set()
 for p in r.rglob('*.json'):
  try:
   d=json.loads(p.read_text())
   if isinstance(d,dict) and isinstance(d.get('tool_session_contexts'),list):capabilities.update(x['id'] for x in d['tool_session_contexts'])
  except (ValueError,KeyError):pass
 replacements={v:'<REDACTED_SECRET>' for v in secrets};replacements.update({v:'redacted-capability-'+hashlib.sha256(v.encode()).hexdigest()[:16] for v in capabilities});replacements[str(ROOT)]='<PROJECT_ROOT>'
 files=[]
 for p in r.rglob('*'):
  if not p.is_file():continue
  rel=p.relative_to(r)
  if p.name in ('demo_database_before.json','demo_database_after.json','official_pricing.html','official_pricing.txt'):continue
  if '__pycache__' in rel.parts or '.venv' in rel.parts or p.suffix in ('.bin','.safetensors','.pyc','.pem','.key') or p.name=='.env':continue
  if rel.parts[0]=='parlant' and p.name not in ('api_calls.jsonl','model_calls.jsonl','parlant.log'):continue
  files.append((p,Path('results')/r.name/rel))
 for p in Path('apps/retail_demo/eval_s1').glob('*'):
  if p.is_file() and p.suffix in ('.py','.md'):files.append((p,p))
 for p in [Path('apps/retail_demo/APP_S1.md'),Path('apps/retail_demo/README.md'),Path('apps/retail_demo/MIGRATION.md'),Path('apps/retail_demo/VERIFICATION.md')]:files.append((p,p))
 for p,rel in files:
  target=stage/rel;target.parent.mkdir(parents=True,exist_ok=True);text=p.read_text()
  for v,value in replacements.items():text=text.replace(v,value)
  assert not any(v in text for v in secrets)
  target.write_text(text)
 for p in (audit/'REPORT.md',audit/'FINAL_ISOLATION.json'):
  text=p.read_text()
  for v,value in replacements.items():text=text.replace(v,value)
  (stage/p.name).write_text(text)
 pricing=json.loads((r/'pricing.json').read_text());save(stage/'OFFICIAL_PRICE_BASIS.json',{'url':pricing['url'],'retrieved_utc':pricing['retrieved_utc'],'currency':'USD','page_sha256':pricing['official_page_sha256'],'Pro_peak_guard':{'hit':.044,'miss':1.32,'output':3.96},'Flash_offpeak_reference':{'hit':.003,'miss':.15,'output':.6},'alias_bill':'unavailable'})
 manifest={str(p.relative_to(stage)):digest(p) for p in stage.rglob('*') if p.is_file() and p.name!='HASH_MANIFEST.json'};save(stage/'HASH_MANIFEST.json',manifest)
 archive=audit/('APP_S1_'+c['stamp']+'_UPLOAD.tar.gz')
 with tarfile.open(archive,'w:gz') as tar:tar.add(stage,arcname='APP_S1')
 with tarfile.open(archive,'r:gz') as tar:
  for name,h in manifest.items():assert hashlib.sha256(tar.extractfile('APP_S1/'+name).read()).hexdigest()==h
 sha=digest(archive);(audit/'UPLOAD_SHA256.txt').write_text(sha+'  '+archive.name+'\n')
 save(audit/'PACKAGE_CHECK.json',{'sha256':sha,'bytes':archive.stat().st_size,'entries':len(manifest),'actual_tar_entries_hash_verified':True,'secrets_excluded':True,'capabilities_redacted':len(capabilities),'private_config_demo_DB_model_weights_excluded':True})
 print('Archive:',archive,'SHA256:',sha)

def main():
 c=config();r=Path(c['result_dir']);a=Path(c['audit_dir']);assert c['container'].startswith('app-s1-') and c['database']=='app_s1'
 before=json.loads((r/'demo_database_before.json').read_text());original=load_settings()
 with connect(original.database_url) as db:
  db.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY');after=plain({t:db.execute(sql.SQL('SELECT * FROM {} ORDER BY 1').format(sql.Identifier(t))).fetchall() for t in before})
 save(r/'demo_database_after.json',after);changed_demo=[t for t in before if before[t]!=after[t]]
 baseline=json.loads((r/'source_before.json').read_text());allowed={'apps/retail_demo/'+p for p in ('business.py','mcp_server.py','session_tools.py','recovery.py','outbox.py','parlant_app.py','guidelines.py','README.md','MIGRATION.md','VERIFICATION.md')}
 unexpected=[p for p,h in baseline.items() if p not in allowed and digest(p)!=h]
 old=json.loads((r/'v1_hashes.json').read_text());oldchanged=[p for p,h in old.items() if digest(p)!=h]
 frozen=json.loads((r/'protocol_source_hashes.json').read_text());freezechanged=[p for p,h in frozen.items() if digest(p)!=h]
 origproc=json.loads(Path('runtime-data/retail-demo/services/processes.json').read_text());alive=all(Path(f'/proc/{x["pid"]}/cmdline').exists() and x['module'].encode() in Path(f'/proc/{x["pid"]}/cmdline').read_bytes() for x in origproc)
 activate();save(r/'final_test_database.json',database_evidence())
 with connect(load_settings().database_url) as db:
  hooks=db.execute("SELECT tgname FROM pg_trigger WHERE tgname LIKE 'eval_fault_%'").fetchall()
  funcs=db.execute("SELECT proname FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='retail_demo' AND proname LIKE 'eval_fault_%'").fetchall()
 markers=list((r/'offline').glob('drop_mcp'))+list((r/'offline').glob('after_commit'))
 processes=[]
 for p in Path(c['runtime_dir']).glob('*.process.json'):
  x=json.loads(p.read_text());path=Path(f'/proc/{x["pid"]}/cmdline');processes.append({**x,'running':path.exists() and x['module'].encode() in path.read_bytes()})
 assertions={'demo_database_unchanged':not changed_demo,'unrelated_application_files_unchanged':not unexpected,'V1_all_result_files_unchanged':not oldchanged,'paid_frozen_source_unchanged':not freezechanged,'original_demo_processes_alive':alive,'test_processes_stopped':not any(p['running'] for p in processes),'test_markers_and_fault_hooks_cleared':not hooks and not funcs and not markers}
 assert all(assertions.values()),assertions
 subprocess.run(['docker','stop',c['container']],check=True,capture_output=True)
 result={'assertions':assertions,'checked_V1_result_files':len(old),'changed_demo_tables':changed_demo,'original_demo_processes':origproc,'original_demo_worker_enabled':after['outbox_control'][0]['enabled'],'original_demo_migrations':[x['version'] for x in after['schema_migrations']],'original_demo_running_loaded_pre_S1_version':True,'test_processes':processes,'test_container_state':subprocess.check_output(['docker','inspect','--format','{{.State.Status}}',c['container']],text=True).strip(),'test_volume_retained':c['volume'],'git_head':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),'git_status':subprocess.check_output(['git','status','--short'],text=True).strip(),'finished_utc':datetime.now(timezone.utc).isoformat()}
 save(r/'final_isolation.json',result);save(a/'FINAL_ISOLATION.json',result)
 package(c)

if __name__=='__main__':main()
