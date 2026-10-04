from common_holdout import *
import re,zipfile,subprocess
def main():
    source=json.loads((PROJECT/'_reviews/20261004_021350_RAG_R1_RETRIEVAL/CORPUS_SOURCE_AUDIT.json').read_text())
    for key in ['DEV_annotated','DEV_unannotated','selection_sha256','HOLDOUT_evaluated']:source.pop(key,None)
    p=DATA/'official/corpora/passage_level/cloud.jsonl.zip';content=p.read_bytes()
    assert sha(p)==source['zip_sha256'] and content[:2]==b'PK'
    blob=hashlib.sha1(b'blob '+str(len(content)).encode()+b'\0'+content).hexdigest()
    assert blob==source['local_git_blob_sha1']
    tree=subprocess.check_output(['git','-C',str(R1/'upstream_metadata'),'ls-tree',source['upstream_commit'],source['asset_path']],text=True).strip()
    assert tree==source['upstream_git_tree']
    with zipfile.ZipFile(p) as z:
        assert z.testzip() is None
        original=[json.loads(x) for x in z.read('cloud.jsonl').splitlines()]
    ps=readl(DATA/'cloud_passages.jsonl');assert original==ps
    assert len(ps)==72442 and len({p['_id'] for p in ps})==72442
    assert all(re.fullmatch(r'ibmcld_\d+-\d+-\d+',p['_id']) and p['url'].startswith('https://cloud.ibm.com/') for p in ps)
    source.update({'this_run_asset_rehashed':True,'ZIP_records_equal_to_frozen_corpus':True,'serialization':'R0 rewrites JSONL whitespace; decoded records exactly equal, file hash remains frozen','HOLDOUT_only_qrels_map_verified_in':'SCORING_ISOLATION.json','no_download_or_asset_change':True})
    write(REVIEW/'CORPUS_SOURCE_AUDIT.json',source)
    write(REVIEW/'MODEL_SOURCE_LOCK.json',json.loads((R1/'model_lock.json').read_text()))
    print('ASSET PROVENANCE RECHECKED')
if __name__=='__main__':main()
