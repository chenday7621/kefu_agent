"""Prepare isolation, source/dependency/pricing/protocol freeze; zero model calls."""
import json,os,subprocess,sys,hashlib,time,socket,re,html
from pathlib import Path
from datetime import datetime,timezone
import httpx
from .common import config,activate,save,digest,assert_isolated,database_evidence
from .protocol import SCENARIOS,plan
from .scoring import selftest
from .fixtures import reset
from ..db import connect,initialize
from ..settings import load_settings


def main():
 c=config();r=Path(c['result_dir']);runtime=Path(c['runtime_dir'])
 # Read-only demo baseline, before setting test env.
 demo=load_settings()
 with connect(demo.database_url) as db:
  db.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
  tables=[x['tablename'] for x in db.execute("SELECT tablename FROM pg_tables WHERE schemaname='retail_demo' ORDER BY tablename").fetchall()]
  from psycopg import sql
  save(r/'demo_database_before.json',{t:db.execute(sql.SQL('SELECT * FROM {} ORDER BY 1').format(sql.Identifier(t))).fetchall() for t in tables})
 for port in (55433,8910,8911,8912):
  try:
   with socket.create_connection(('127.0.0.1',port),timeout=.2):raise RuntimeError('Evaluation port occupied; cannot isolate')
  except OSError:pass
 # Pricing evidence fetched before any paid task; legacy alias is not priced on current page.
 u='https://api-docs.deepseek.com/quick_start/pricing'
 response=httpx.get(u,follow_redirects=True,timeout=25);response.raise_for_status()
 (r/'official_pricing.html').write_text(response.text)
 text=html.unescape(re.sub('<[^>]+>',' ',response.text));text=re.sub(r'\s+',' ',text)
 (r/'official_pricing.txt').write_text(text)
 assert all(x in text for x in ('$3.96','$1.32','$0.044'))
 save(r/'pricing.json',{'url':u,'retrieved_utc':datetime.now(timezone.utc).isoformat(),'currency':'USD','official_page_sha256':digest(r/'official_pricing.html'),'requested_legacy_model':'deepseek-chat','legacy_alias_current_official_price':'unavailable; current page lists Flash/Pro, not deepseek-chat','estimate_basis':'conservative reference scenario using maximum published peak Pro rates; not alias-specific billing or guaranteed upper bound on unknown tariff','usd_per_million':{'hit':.044,'miss':1.32,'output':3.96},'budget_cap_usd':5,'reserve_before_call':'UTF-8 input bytes as conservative token allowance + native max_tokens; outstanding/unknown usage keeps reserved allowance','actual_bill':'unavailable'})
 c['fixture_anchor']=datetime.now(timezone.utc).isoformat();save(runtime/'private.json',c);(runtime/'private.json').chmod(0o600)
 subprocess.run(['docker','run','-d','--name',c['container'],'--env-file',str(runtime/'postgres.env'),'-p','127.0.0.1:55433:5432','-v',c['volume']+':/var/lib/postgresql/data','postgres:16-bookworm'],check=True,capture_output=True)
 activate()
 deadline=time.monotonic()+40
 while True:
  try:
   with connect(load_settings().database_url):break
  except Exception:
   if time.monotonic()>deadline:raise
   time.sleep(.3)
 initialize(load_settings().database_url);reset();assert_isolated()
 save(r/'initial_fixture.json',database_evidence())
 save(r/'scenarios.json',SCENARIOS);save(r/'plan.json',plan())
 save(r/'scorer_selftest.json',selftest())
 subprocess.run([sys.executable,'-m','pip','freeze'],stdout=(r/'dependencies.txt').open('w'),check=True)
 model=Path('runtime-data/huggingface/hub/models--sentence-transformers--paraphrase-multilingual-MiniLM-L12-v2/snapshots/e8f8c211226b894fcb81acc59f3b34ba3efd5f42')
 save(r/'embedding_files.json',{str(p.relative_to(model)):digest(p) for p in model.rglob('*') if p.is_file()})
 files=list(Path('apps/retail_demo/eval_v1').glob('*.py'))+list(Path('apps/retail_demo').glob('*.py'))+list(Path('apps/retail_demo/migrations').glob('*.sql'))+[Path('src/parlant/adapters/nlp/deepseek_service.py'),Path('src/parlant/sdk.py'),Path('pyproject.toml')]
 save(r/'protocol_source_hashes.json',{str(p):digest(p) for p in files})
 save(r/'protocol.json',{'name':'APP-EVAL-V1','custom_fixed_identity_single_instance_baseline':True,'not_official_benchmark_or_browser_test_or_causal_gain':True,'scenario_count':20,'natural_language_attempts':40,'fault_samples':18,'max_customer_messages':8,'turn_timeout_seconds':180,'task_timeout_seconds':720,'normal_failure_retry':0,'unscorable_external_interruption_max_recovery':1,'engine_limit_unchanged':5,'temperature':'native hints; each actual request saved','requested_model':'deepseek-chat','no_simulator_or_judge':True,'text_unknown':'needs_review, free prose facts unreviewed','faults_not_combined_with_dialogue_score':True,'effective_metadata':'native deterministic IDs/fields checked at each start; only creation_utc differs because native SDK transient stores rebuild constants, no generated rule text or optimization','selection_and_scorer_frozen_before_paid_call':True,'fixture_anchor':c['fixture_anchor']})
 print('Isolation and protocol freeze prepared; no model calls. Results:',r)

if __name__=='__main__':main()
