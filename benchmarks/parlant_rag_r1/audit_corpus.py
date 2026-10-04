"""Read-only provenance verification. Never reads official rewrite or heldout answers."""
from common_r1 import *
import zipfile,re,subprocess,collections
def main():
    frozen=json.loads((R0_REVIEW/'DATA_HASHES.json').read_text())
    for name,value in frozen.items():assert sha(PROJECT/name)==value,name
    zip_path=DATA/'official/corpora/passage_level/cloud.jsonl.zip'
    content=zip_path.read_bytes();assert content[:2]==b'PK' and not content.startswith(b'version https://git-lfs')
    blob=hashlib.sha1(b'blob '+str(len(content)).encode()+b'\0'+content).hexdigest()
    git=BASE/'upstream_metadata'
    commit=subprocess.check_output(['git','-C',str(git),'rev-parse','2c618bb98db3c8526433e22d8a2f7320f10a7470'],text=True).strip()
    tree=subprocess.check_output(['git','-C',str(git),'ls-tree',commit,'corpora/passage_level/cloud.jsonl.zip'],text=True).strip()
    assert commit=='2c618bb98db3c8526433e22d8a2f7320f10a7470' and tree.split()[2]==blob
    with zipfile.ZipFile(zip_path) as z:
        assert z.testzip() is None and z.namelist()==['cloud.jsonl']
        official=[json.loads(x) for x in z.read('cloud.jsonl').splitlines()]
    local=readl(DATA/'cloud_passages.jsonl');assert official==local and len(local)==72442
    ids={p['_id'] for p in local};assert len(ids)==72442
    assert all(re.fullmatch(r'ibmcld_\d+-\d+-\d+',x) for x in ids)
    assert all(p['url'].startswith('https://cloud.ibm.com/') for p in local)
    qrel_rows=[x.split() for x in (DATA/'official/mtrag-human/retrieval_tasks/cloud/qrels/dev.tsv').read_text().splitlines()[1:]]
    assert len(qrel_rows)==494 and all(r[1] in ids for r in qrel_rows)
    qrels=json.loads((DATA/'qrels_dev.json').read_text());tasks=load_tasks()
    assert len(qrels)==105 and sum(t['task_id'] not in qrels for t in tasks)==11
    for t in tasks:
        if t['task_id'] in qrels:
            assert qrels[t['task_id']]=={r[1]:int(r[2]) for r in qrel_rows if r[0]==t['task_id']}
    selection=json.loads((DATA/'selection.json').read_text());assert len(selection['targets'])==10
    row={'asset_path':'corpora/passage_level/cloud.jsonl.zip','upstream_commit':commit,'upstream_git_tree':tree,'local_git_blob_sha1':blob,'git_blob_exact_match':True,'zip_sha256':sha(zip_path),'zip_bytes':len(content),'zip_member':'cloud.jsonl','zip_crc_valid':True,'lfs_pointer':False,'lfs_attributes':'only legacy corpora/cloud.jsonl, fiqa.jsonl, govt.jsonl, clapnq.jsonl; passage_level ZIP not matched','all_r0_data_hashes_unchanged':True,'extracted_records_exactly_equal_r0':True,'passages':len(ids),'parent_document_ids':len({x.rsplit('-',2)[0] for x in ids}),'id_pattern':'ibmcld_[digits]-[start]-[end]','qrels_rows':494,'qrels_queries':len({r[0] for r in qrel_rows}),'qrels_full_id_exact_matches':494,'qrels_grades':dict(collections.Counter(r[2] for r in qrel_rows)),'DEV_annotated':105,'DEV_unannotated':11,'README_cloud_passages':61022,'README_cloud_documents':57638,'README_government_passages':72422,'README_government_documents':8578,'count_difference_explanation':'The frozen official Cloud ZIP itself contains 72442 IBM Cloud-prefixed passages and IBM Cloud URLs; exact official git blob and all 494 qrels offsets match. README counts conflict with the published asset and Cloud conversation collection metadata. Upstream documentation/asset inconsistency; underlying editorial/versioning cause unavailable. Similar Government counts are not evidence that this is the Government asset. No asset redownload or R0 alteration. Parent IDs counted separately only; offsets are preserved in retrieval/scoring.','selection_sha256':sha(DATA/'selection.json'),'split_sha256':sha(DATA/'split.json'),'license':'Apache-2.0 (official repository LICENSE); BGE MIT separately','HOLDOUT_evaluated':False}
    write(REVIEW/'CORPUS_SOURCE_AUDIT.json',row);print('CORPUS VERIFIED',len(ids),blob)
if __name__=='__main__':main()
