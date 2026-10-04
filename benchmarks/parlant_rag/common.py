"""R0 frozen lexical retrieval. No model, gold labels or query rewriting."""
from pathlib import Path
import collections, hashlib, heapq, json, math, os, re, pickle, tempfile
BASE = Path(__file__).resolve().parent
PROJECT = BASE.parents[1]
PATHS = json.loads((BASE/'paths.json').read_text())
REVIEW = PROJECT/PATHS['review']
RESULT = PROJECT/PATHS['result']
DATA = BASE/'data'
OFFICIAL = DATA/'official'
COLLECTION = 'mt-rag-ibmcloud-elser-512-100-20240502'

def sha(path):
    with Path(path).open('rb') as f: return hashlib.file_digest(f,'sha256').hexdigest()

def digest(value): return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()

def write(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+'.tmp')
    with tmp.open('w') as f:
        json.dump(value,f,ensure_ascii=False,indent=2,default=str);f.flush();os.fsync(f.fileno())
    tmp.replace(path)

def jsonl(path,rows):
    Path(path).parent.mkdir(parents=True,exist_ok=True)
    with Path(path).open('w') as f:
        for x in rows:f.write(json.dumps(x,ensure_ascii=False)+'\n')
        f.flush();os.fsync(f.fileno())

def readl(path):return [json.loads(x) for x in Path(path).read_text().splitlines()]

def tokens(text):return re.findall(r'\b\w+\b',text.casefold(),flags=re.UNICODE)

def sanitize(task):
    messages=[{'speaker':x['speaker'],'text':x['text']} for x in task['input']]
    assert messages and messages[-1]['speaker']=='user'
    assert all(x['speaker'] in ('user','agent') and isinstance(x['text'],str) for x in messages)
    return {'task_id':task['task_id'],'conversation_id':task['conversation_id'],'input':messages}

class BM25:
    def __init__(self,passages,k1=1.2,b=0.75):
        self.passages=sorted(passages,key=lambda x:x['_id']); self.k1=k1;self.b=b
        self.postings=collections.defaultdict(list);self.lengths=[]
        for i,p in enumerate(self.passages):
            ts=tokens(p['title']+' '+p['text']);self.lengths.append(len(ts))
            for t,n in collections.Counter(ts).items():self.postings[t].append((i,n))
        self.avgdl=sum(self.lengths)/len(self.lengths)
    def search(self,query,k=10):
        scores=collections.defaultdict(float);n=len(self.passages)
        # Each distinct query term contributes once, lexicographically ordered.
        for t in sorted(set(tokens(query))):
            posting=self.postings.get(t,[]);df=len(posting);idf=math.log(1+(n-df+0.5)/(df+0.5))
            for i,tf in posting:
                scores[i]+=idf*tf*(self.k1+1)/(tf+self.k1*(1-self.b+self.b*self.lengths[i]/self.avgdl))
        # Include zero-score passages to make fixed K deterministic for empty queries.
        top=heapq.nsmallest(k,range(n),key=lambda i:(-scores.get(i,0.0),self.passages[i]['_id']))
        return [{'document_id':self.passages[i]['_id'],'text':self.passages[i]['text'],'title':self.passages[i]['title'],'url':self.passages[i].get('url',''),'score':scores.get(i,0.0)} for i in top]
    def save(self,path):
        with Path(path).open('wb') as f:pickle.dump(self,f,protocol=5)
    @staticmethod
    def load():
        with (DATA/'bm25.pkl').open('rb') as f:return pickle.load(f)

def metric(ids,rels,k):
    relevant={x for x,v in rels.items() if v>0}
    if not relevant:return {'Recall@'+str(k):'unavailable','nDCG@'+str(k):'unavailable'}
    dcg=sum(rels.get(x,0)/math.log2(i+2) for i,x in enumerate(ids[:k]))
    ideal=sum(v/math.log2(i+2) for i,v in enumerate(sorted(rels.values(),reverse=True)[:k]))
    return {'Recall@'+str(k):len(set(ids[:k])&relevant)/len(relevant),'nDCG@'+str(k):dcg/ideal}
