"""Freeze selected retrieval, unchanged generation settings and integrity."""
from common_r1 import *
import subprocess
selection=json.loads((REVIEW/'SELECTION.json').read_text());assert selection['gate_passed']
assert json.loads((REVIEW/'OFFLINE_RUNTIME_CHECKS.json').read_text())['passed']
files=[p for p in BASE.rglob('*') if p.is_file() and p.parts[-1]!='tokens.pkl' and 'upstream_metadata' not in p.parts and 'hf_download_cache' not in p.parts and '.cache' not in p.parts and '__pycache__' not in p.parts]
files+=[p for p in (PROJECT/'src').rglob('*.py')]+[PROJECT/'pyproject.toml',PROJECT/'uv.lock',R0/'config.json',DATA/'split.json',DATA/'selection.json',DATA/'runtime_dev.jsonl',DATA/'bm25.pkl',DATA/'cloud_passages.jsonl',RESULT/'selected_ten_retrieval.jsonl',R0_RESULT/'startup/effective_configuration.json']
packages=subprocess.check_output(['uv','pip','freeze','--python',str(PROJECT/'.venv/bin/python')],text=True)
assert packages==(REVIEW/'DEPENDENCIES_BEFORE.txt').read_text()
write(REVIEW/'GENERATION_FROZEN.json',{'files':{str(p.relative_to(PROJECT)):sha(p) for p in files},'selection':selection,'configuration':'exact R0 effective snapshot; no reevaluation; same internal agent/tool IDs in separate store','retrieval':'cached exact selected top5; runtime query and full history revalidated before each native return','original_ten_selection_sha256':sha(DATA/'selection.json'),'dependencies_unchanged':True,'max_new_target_answers':10,'paid_query_rewrite':False,'judge':False,'HOLDOUT':False})
print('GENERATION FROZEN',selection['selected'])
