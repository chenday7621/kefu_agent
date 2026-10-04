"""Read-only result audit, auxiliary lexical scores and sanitized upload bundle. No model calls."""
import collections,csv,datetime,re,shutil,subprocess,tarfile
from common_r2 import *

def norm(text):return re.sub(r'\b(a|an|the)\b',' ',re.sub(r'[^\w\s]',' ',text.casefold())).split()
def f1(a,b):
    a=norm(a);b=norm(b)
    if not a or not b:return float(a==b)
    overlap=sum((collections.Counter(a)&collections.Counter(b)).values())
    return 2*overlap/(len(a)+len(b))
def rouge(a,b):
    a=norm(a);b=norm(b)
    if not a or not b:return float(a==b)
    row=[0]*(len(b)+1)
    for x in a:
        nxt=[0]
        for i,y in enumerate(b):nxt.append(row[i]+1 if x==y else max(row[i+1],nxt[-1]))
        row=nxt
    return 2*row[-1]/(len(a)+len(b))

def read_obs():return [x for p in sorted(RESULT.rglob('observations.jsonl')) for x in readl(p)]

def usage_group(rows,price):
    calls=[x for x in rows if x['kind']=='model_call_started'];http=[x for x in rows if x['kind']=='physical_http_started'];responses=[x for x in rows if x['kind']=='provider_response_received']
    usages=[x['raw_usage'] for x in responses if isinstance(x['raw_usage'],dict)]
    fields=['prompt_tokens','completion_tokens','prompt_cache_hit_tokens','prompt_cache_miss_tokens']
    known={k:sum(u[k] for u in usages if isinstance(u.get(k),int)) for k in fields}
    missing={k:sum(not isinstance(u.get(k),int) for u in usages) for k in fields}
    terminal={x.get('call_id') for x in responses};unknown=[x['call_id'] for x in calls if x['call_id'] not in terminal]
    unknown_request_usage=max(0,len(http)-len(responses),sum(x['kind']=='provider_request_started' for x in rows)-len(responses))
    totals={k:known[k] if not missing[k] and not unknown and not unknown_request_usage else 'unavailable' for k in fields}
    cost=0.0;cost_missing=0
    for x in responses:
        u=x['raw_usage'];model=x['response_model'];date=datetime.datetime.fromisoformat(x['utc']);peak=date.weekday()<5 and (1<=date.hour<4 or 6<=date.hour<10)
        rates=price['per_million_tokens'].get(model,{}).get('peak' if peak else 'off_peak')
        if not rates or not isinstance(u,dict) or any(not isinstance(u.get(k),int) for k in fields):cost_missing+=1;continue
        assert u['prompt_cache_hit_tokens']+u['prompt_cache_miss_tokens']==u['prompt_tokens']
        cost+=(u['prompt_cache_hit_tokens']*rates['cache_hit_input']+u['prompt_cache_miss_tokens']*rates['cache_miss_input']+u['completion_tokens']*rates['output'])/1e6
    return {'schematic_calls':len(calls),'physical_http_requests':len(http) if http or not calls else 'unavailable','provider_create_calls':sum(x['kind']=='provider_request_started' for x in rows),'physical_http_observation':'no transport hook records despite provider responses; do not treat absence as 0; SDK retries unavailable' if calls and not http else 'observed','provider_responses':len(responses),'known_token_totals':known,'token_totals':totals,'unaccounted_request_usage_count':unknown_request_usage,'missing_response_calls':unknown,'missing_fields':missing,'known_cost_estimate_usd':cost,'cost_estimate_usd':cost if not cost_missing and not unknown and not unknown_request_usage and not any(missing.values()) else 'unavailable','cost_missing_responses':cost_missing,'invoice':'unavailable','response_models':dict(collections.Counter(x['response_model'] for x in responses))}

