"""Read-only integrity audit, stop only owned test container, redacted evidence bundle."""
import json,os,subprocess,hashlib,tarfile,shutil
from pathlib import Path
from datetime import datetime,timezone
from psycopg import sql
from dotenv import dotenv_values
from ..settings import load_settings,ROOT
from ..db import connect
from ..business import plain
from .common import config,activate,save,digest,database_evidence


def main():
 c=config();r=Path(c['result_dir']);audit=Path(c['audit_dir'])
 assert c['container'].startswith('app-eval-v1-') and c['database']=='app_eval_v1'
 # Read original env before activating test env, never connect using default for reset.
 original=load_settings();baseline=json.loads((r/'demo_database_before.json').read_text())
 with connect(original.database_url) as db:
  db.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
  after=plain({t:db.execute(sql.SQL('SELECT * FROM {} ORDER BY 1').format(sql.Identifier(t))).fetchall() for t in baseline})
 save(r/'demo_database_after.json',after)
 changed_demo=[t for t in baseline if baseline[t]!=after[t]]
 source=json.loads((r/'source_baseline.json').read_text());changed_source=[p for p,h in source.items() if digest(p)!=h]
 old=json.loads((r/'historical_report_hashes.json').read_text());changed_old=[p for p,h in old.items() if digest(p)!=h]
 original_proc=json.loads((r/'demo_processes_before.json').read_text());proc_current=json.loads(Path('runtime-data/retail-demo/services/processes.json').read_text())
 original_alive=all(Path(f'/proc/{x["pid"]}/cmdline').exists() and x['module'].encode() in Path(f'/proc/{x["pid"]}/cmdline').read_bytes() for x in original_proc)
 activate();state=database_evidence();save(r/'final_test_database.json',state)
 with connect(load_settings().database_url) as db:
  hooks=db.execute("SELECT tgname FROM pg_trigger WHERE tgname LIKE 'eval_fault_%'").fetchall()
  functions=db.execute("SELECT proname FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='retail_demo' AND proname LIKE 'eval_fault_%'").fetchall()
  sequences=db.execute("SELECT sequencename FROM pg_sequences WHERE schemaname='retail_demo' AND sequencename LIKE 'eval_fault_%'").fetchall()
 processes=[]
 for p in Path(c['runtime_dir']).glob('*.process.json'):
  x=json.loads(p.read_text());cmd=Path(f'/proc/{x["pid"]}/cmdline')
  processes.append({'module':x['module'],'pid':x['pid'],'still_running':cmd.exists() and x['module'].encode() in cmd.read_bytes()})
 markers=list((r/'faults').glob('*/drop_mcp'))+list((r/'faults').glob('*/after_commit'))
 model_paths=Path('runtime-data/huggingface/hub/models--sentence-transformers--paraphrase-multilingual-MiniLM-L12-v2/snapshots/e8f8c211226b894fcb81acc59f3b34ba3efd5f42')
 model_changed=[p for p,h in json.loads((r/'embedding_files.json').read_text()).items() if digest(model_paths/p)!=h]
 scorer_digest=digest('apps/retail_demo/eval_v1/scoring.py');frozen=json.loads((r/'protocol_source_hashes.json').read_text())
 assertions={'original_demo_db_unchanged':not changed_demo,'target_source_and_old_docs_unchanged':not changed_source,'old_raw_reports_unchanged':not changed_old,'original_process_records_unchanged':proc_current==original_proc,'original_processes_alive':original_alive,'test_processes_stopped':not any(x['still_running'] for x in processes),'fault_hooks_cleared':not hooks and not functions and not sequences and not markers,'embedding_assets_unchanged':not model_changed,'scorer_source_unchanged_since_before_paid_call':scorer_digest==frozen['apps/retail_demo/eval_v1/scoring.py']}
 assert all(assertions.values()),assertions
 stopped=subprocess.run(['docker','stop',c['container']],check=True,capture_output=True,text=True)
 container_state=subprocess.check_output(['docker','inspect','--format','{{.State.Status}}',c['container']],text=True).strip();assert container_state=='exited'
 final={'assertions':assertions,'changed_demo_tables':changed_demo,'changed_target_files':changed_source,'old_report_files_checked':len(old),'test_processes':processes,'test_container':c['container'],'test_container_state':container_state,'test_volume_retained':c['volume'],'original_worker_enabled':after['outbox_control'][0]['enabled'],'original_outbox_statuses':{status:sum(x['status']==status for x in after['return_outbox']) for status in ('pending','failed','delivered')},'original_demo_counts':{t:len(after[t]) for t in ('orders','return_requests','return_operations','parlant_sessions','parlant_events','return_outbox')},'git_head':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),'git_status':subprocess.check_output(['git','status','--short'],text=True).strip(),'initial_preflight_default_runtime_log_appends':'preserved; SDK env-order failure before any paid call, no original DB/frozen source change','completed_utc':datetime.now(timezone.utc).isoformat()}
 save(r/'final_isolation.json',final);save(audit/'FINAL_ISOLATION.json',final)
 # Package public metadata and all test evidence, not old demo DB, weights, private configs or pricing HTML.
 staging=audit/'upload_stage';staging.mkdir(exist_ok=True)
 secrets=[]
 for path in (ROOT/'.env',ROOT/'apps/retail_demo/.env'):
  for k,v in dotenv_values(path).items():
   if v and len(v)>10 and any(n in k.upper() for n in ('KEY','TOKEN','PASSWORD','DATABASE_URL')):secrets.append(v)
 secrets.append(c['password'])
 capabilities=set()
 for p in (r/'tasks').glob('*/final_database.json'):
  capabilities.update(x['id'] for x in json.loads(p.read_text())['tool_session_contexts'])
 for p in (r/'faults').glob('*/final_database.json'):
  capabilities.update(x['id'] for x in json.loads(p.read_text())['tool_session_contexts'])
 replacements={v:'<REDACTED_SECRET>' for v in secrets}
 replacements.update({v:'redacted-capability-'+hashlib.sha256(v.encode()).hexdigest()[:16] for v in capabilities})
 replacements[str(ROOT)]='<PROJECT_ROOT>'
 selected=[]
 for p in sorted(r.rglob('*')):
  if not p.is_file():continue
  rel=p.relative_to(r)
  if p.name in ('demo_database_before.json','demo_database_after.json','official_pricing.html','official_pricing.txt'):continue
  if '__pycache__' in rel.parts or '.venv' in rel.parts or p.suffix in ('.bin','.safetensors','.pyc','.pem','.key') or p.name=='.env':continue
  if rel.parts[0]=='parlant' and p.name not in ('api_calls.jsonl','model_calls.jsonl','parlant.log'):continue
  selected.append((p,Path('results')/r.name/rel))
 for p in sorted(Path('apps/retail_demo/eval_v1').rglob('*')):
  if p.is_file() and p.suffix in ('.py','.md'):selected.append((p,p))
 basis={'source':'https://api-docs.deepseek.com/quick_start/pricing','retrieved_utc':json.loads((r/'pricing.json').read_text())['retrieved_utc'],'currency':'USD','unit':'per million tokens','Flash_off_peak':{'hit':.003,'miss':.15,'output':.6},'Flash_peak':{'hit':.006,'miss':.3,'output':1.2},'Pro_peak_guard_reference':{'hit':.044,'miss':1.32,'output':3.96},'legacy_alias_price':'unavailable; Flash estimate inferred from actual returned model','full_page_local_sha256':digest(r/'official_pricing.html')}
 save(staging/'OFFICIAL_PRICE_BASIS.json',basis)
 for p,rel in selected:
  dest=staging/rel;dest.parent.mkdir(parents=True,exist_ok=True)
  text=p.read_text(errors='strict')
  for value,replacement in replacements.items():text=text.replace(value,replacement)
  assert not any(v in text for v in secrets)
  dest.write_text(text)
 for p in (audit/'REPORT.md',audit/'FINAL_ISOLATION.json'):
  text=p.read_text()
  for value,replacement in replacements.items():text=text.replace(value,replacement)
  (staging/p.name).write_text(text)
 manifest={str(p.relative_to(staging)):digest(p) for p in sorted(staging.rglob('*')) if p.is_file()}
 save(staging/'HASH_MANIFEST.json',manifest)
 bundle=audit/('APP_EVAL_V1_'+c['stamp']+'_UPLOAD.tar.gz')
 with tarfile.open(bundle,'w:gz') as tar:tar.add(staging,arcname='APP_EVAL_V1')
 (audit/'UPLOAD_SHA256.txt').write_text(digest(bundle)+'  '+bundle.name+'\n')
 save(audit/'PACKAGE_CHECK.json',{'files':len(manifest),'sha256':digest(bundle),'bytes':bundle.stat().st_size,'secret_exact_value_scan':'clean','root_personal_path_replaced':True,'MCP_capabilities_redacted':len(capabilities),'private_configs_and_original_demo_db_not_packaged':True,'model_weights_not_packaged':True,'official_price_basis_table_packaged_instead_of_full_HTML':True})
 print(json.dumps({'assertions':assertions,'original_worker_enabled':final['original_worker_enabled'],'test_container':'exited','bundle':str(bundle),'sha256':digest(bundle)},ensure_ascii=False,indent=2))

if __name__=='__main__':main()
