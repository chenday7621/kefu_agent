"""One fixed CPU float32 BGE corpus cache; no labels or model API."""
from common_r1 import *
os.environ['TOKENIZERS_PARALLELISM']='false'
os.environ['HF_HUB_OFFLINE']='1'
os.environ['TRANSFORMERS_OFFLINE']='1'
import time,pickle,numpy as np,torch
from transformers import AutoTokenizer,AutoModel

def encoder():
    torch.set_num_threads(PROTOCOL['embedding_execution']['torch_threads'])
    torch.set_num_interop_threads(1)
    tok=AutoTokenizer.from_pretrained(BASE/'model',local_files_only=True)
    model=AutoModel.from_pretrained(BASE/'model',local_files_only=True,attn_implementation='sdpa').eval()
    assert next(model.parameters()).dtype==torch.float32
    return tok,model

def embed(tok,model,records):
    batch=tok.pad(records,padding=True,return_tensors='pt')
    with torch.inference_mode():
        value=model(**batch).last_hidden_state[:,0]
        return torch.nn.functional.normalize(value,p=2,dim=1).numpy().astype('float32')

def main():
    cache=BASE/'index';cache.mkdir(exist_ok=True)
    passages=sorted(readl(DATA/'cloud_passages.jsonl'),key=lambda p:p['_id'])
    tok,model=encoder();n=len(passages);assert n==72442
    lock={'corpus_sha256':sha(DATA/'cloud_passages.jsonl'),'model_lock_sha256':sha(BASE/'model_lock.json'),'protocol_sha256':sha(BASE/'protocol.json'),'shape':[n,768],'dtype':'float32'}
    if (cache/'lock.json').exists():assert json.loads((cache/'lock.json').read_text())==lock
    else:write(cache/'lock.json',lock)
    tokens_path=cache/'tokens.pkl'
    if tokens_path.exists():
        with tokens_path.open('rb') as f:records=pickle.load(f)
    else:
        records=[];untruncated=[]
        for start in range(0,n,512):
            texts=[p['title']+' '+p['text'] for p in passages[start:start+512]]
            raw=tok(texts,truncation=False,padding=False)
            untruncated.extend(len(x) for x in raw['input_ids'])
            cut=tok(texts,truncation=True,max_length=512,padding=False)
            records.extend({k:v[i] for k,v in cut.items()} for i in range(len(texts)))
        with tokens_path.open('wb') as f:pickle.dump(records,f,protocol=5)
        write(cache/'tokenization.json',{'passages':n,'truncated_passages':sum(x>512 for x in untruncated),'max_original_tokens':max(untruncated),'input_fields':'title + space + exact text','no_instruction':True})
    order=sorted(range(n),key=lambda i:(len(records[i]['input_ids']),passages[i]['_id']))
    write(cache/'passage_ids.json',[p['_id'] for p in passages])
    checkpoint=cache/'checkpoint.json';done=json.loads(checkpoint.read_text())['committed_rows'] if checkpoint.exists() else 0
    matrix=np.lib.format.open_memmap(cache/'corpus.npy',mode='r+' if (cache/'corpus.npy').exists() else 'w+',dtype='float32',shape=(n,768))
    start=time.monotonic();begin=done;batchsize=16
    while done<n:
        indices=order[done:done+batchsize];vectors=embed(tok,model,[records[i] for i in indices])
        assert np.isfinite(vectors).all() and np.allclose(np.linalg.norm(vectors,axis=1),1,atol=1e-5)
        matrix[indices]=vectors;matrix.flush();done+=len(indices)
        with (cache/'batches.jsonl').open('a') as f:
            f.write(json.dumps({'rows':indices,'end_committed':done,'sha256':hashlib.sha256(vectors.tobytes()).hexdigest(),'seconds':time.monotonic()-start})+'\n');f.flush();os.fsync(f.fileno())
        write(checkpoint,{'committed_rows':done,'total':n,'elapsed_this_process':time.monotonic()-start})
        if done%160==0 or done==n:
            seconds=time.monotonic()-start;rate=(done-begin)/seconds
            print(f'ENCODE {done}/{n} {rate:.2f} passages/s ETA {(n-done)/max(rate,1e-9)/60:.1f} min',flush=True)
    write(cache/'hashes.json',{p.name:sha(p) for p in [cache/'corpus.npy',cache/'passage_ids.json',cache/'tokens.pkl',cache/'lock.json']})
    journal=readl(cache/'batches.jsonl');all_rows=[i for row in journal for i in row['rows']]
    assert len(all_rows)==n and len(set(all_rows))==n and set(all_rows)==set(range(n))
    assert np.isfinite(matrix).all() and np.allclose(np.linalg.norm(matrix,axis=1),1,atol=1e-5)
    write(cache/'integrity.json',{'committed_rows':n,'unique_committed_rows':len(set(all_rows)),'batches':len(journal),'finite_embeddings':True,'L2_norm_1':True,'no_passage_reencoded':True,'seconds_encoding':time.monotonic()-start})
    write(REVIEW/'INDEX_HASHES.json',{'model':json.loads((BASE/'model_lock.json').read_text()),'index':json.loads((cache/'hashes.json').read_text()),'complete':True,'passages_encoded':n,'versions':{'torch':torch.__version__,'numpy':np.__version__}})
    print('CORPUS COMPLETE',flush=True)

if __name__=='__main__':main()
