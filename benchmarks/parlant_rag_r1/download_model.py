"""Lock official BGE revision and fetch only tokenizer/config/weights/model card."""
from common_r1 import *
import urllib.request,concurrent.futures,time
from huggingface_hub import HfApi,hf_hub_download

def main():
    api=HfApi();existing=json.loads((BASE/'model_lock.json').read_text()) if (BASE/'model_lock.json').exists() else None
    info=api.model_info(PROTOCOL['dense_model'],revision=existing['revision'] if existing else None);revision=info.sha
    names={x.rfilename for x in info.siblings}
    chosen=[x for x in ['README.md','config.json','tokenizer.json','tokenizer_config.json','special_tokens_map.json','vocab.txt','model.safetensors','1_Pooling/config.json','sentence_bert_config.json','modules.json'] if x in names]
    assert 'model.safetensors' in chosen
    model=BASE/'model';model.mkdir(exist_ok=True)
    write(REVIEW/'MODEL_SOURCE_LOCK.json',{'repo':PROTOCOL['dense_model'],'revision':revision,'license':'MIT per official model card','files':chosen,'model_card_url':'https://huggingface.co/BAAI/bge-base-en-v1.5','official_card_read':True})
    def download(name):
        path=hf_hub_download(PROTOCOL['dense_model'],name,revision=revision,local_dir=model,cache_dir=BASE/'hf_download_cache')
        return name,sha(path),Path(path).stat().st_size
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
        entries=list(ex.map(download,chosen))
    lock={'repo':PROTOCOL['dense_model'],'revision':revision,'files':{n:{'sha256':h,'bytes':s} for n,h,s in entries},'instruction':PROTOCOL['query_instruction'],'pooling':'CLS','normalize':'L2','device':'CPU','precision':'float32','model_card':'MIT'}
    if existing:assert lock==existing,'Frozen model must not change'
    write(BASE/'model_lock.json',lock);write(REVIEW/'MODEL_HASHES.json',lock);print('MODEL LOCKED',revision,'files',len(entries),flush=True)
if __name__=='__main__':main()
