"""Read-only full cache integrity; no encoder/model/API."""
from common_r1 import *
import numpy as np
cache=BASE/'index';checkpoint=json.loads((cache/'checkpoint.json').read_text());assert checkpoint['committed_rows']==72442
matrix=np.load(cache/'corpus.npy',mmap_mode='r');assert matrix.shape==(72442,768) and matrix.dtype==np.float32
ids=json.loads((cache/'passage_ids.json').read_text());assert len(set(ids))==72442 and ids==sorted(ids)
assert set(ids)=={p['_id'] for p in readl(DATA/'cloud_passages.jsonl')}
assert np.isfinite(matrix).all() and np.allclose(np.linalg.norm(matrix,axis=1),1,atol=1e-5)
journal=readl(cache/'batches.jsonl');rows=[i for r in journal for i in r['rows']]
assert len(rows)==72442 and len(set(rows))==72442 and set(rows)==set(range(72442))
for r in journal:assert hashlib.sha256(np.asarray(matrix[r['rows']]).tobytes()).hexdigest()==r['sha256']
for name,value in json.loads((cache/'hashes.json').read_text()).items():assert sha(cache/name)==value
write(REVIEW/'INDEX_INTEGRITY.json',{'committed_rows':72442,'unique_committed_rows':72442,'batch_hashes_checked':len(journal),'finite':True,'L2_norm_1':True,'duplicate_encodings':0,'elapsed_encoding_seconds':checkpoint['elapsed_this_process'],'model_lock_sha256':sha(BASE/'model_lock.json'),'corpus_sha256':sha(DATA/'cloud_passages.jsonl')})
print('INDEX INTEGRITY VERIFIED',flush=True)
