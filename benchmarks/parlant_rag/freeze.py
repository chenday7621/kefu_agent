"""Freeze read-only runtime protocol and dependencies after all offline checks."""
import subprocess,sys
from common import *
for name in ['OFFLINE_CHECKS.json','OFFLINE_RETRIEVER_CHECK.json']:
    assert (REVIEW/name).exists(),name
assert (RESULT/'retrieval_metrics.json').exists()
assert not (RESULT/'startup/status.json').exists()
files=[p for p in BASE.rglob('*') if p.is_file() and '__pycache__' not in p.parts]
files+=list((PROJECT/'src').rglob('*.py'))+[PROJECT/'pyproject.toml',PROJECT/'uv.lock']
packages=subprocess.check_output(['uv','pip','freeze','--python',str(PROJECT/'.venv/bin/python')],text=True)
(REVIEW/'DEPENDENCIES.txt').write_text(packages)
freeze={'files':{str(p.relative_to(PROJECT)):sha(p) for p in sorted(files) if p.exists()},'dependencies_sha256':sha(REVIEW/'DEPENDENCIES.txt'),'parlant_git_head':subprocess.check_output(['git','rev-parse','HEAD'],cwd=PROJECT,text=True).strip(),'python':sys.version,'execution':'only fixed 10 DEV rounds, no paid preflight, no holdout, no judge','startup_effective_metadata':'will be saved once after native SDK initialization and reused unchanged for all 10 samples'}
write(REVIEW/'RUN_FROZEN.json',freeze)
print('frozen',len(freeze['files']),'files')
