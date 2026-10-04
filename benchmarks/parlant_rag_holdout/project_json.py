"""Skip JSON value spans without decoding excluded gold/rewrite fields."""
import json
def end_value(s, i):
    i0=i
    if s[i]=='"':
        i+=1
        while i<len(s):
            if s[i]=='\\': i+=2
            elif s[i]=='"': return i+1
            else: i+=1
    elif s[i] in '[{':
        closing='}' if s[i]=='{' else ']'; i+=1
        while i<len(s):
            if s[i] in ' \r\n\t,:': i+=1
            elif s[i]==closing: return i+1
            else: i=end_value(s,i)
    else:
        while i<len(s) and s[i] not in ',]} \r\n\t': i+=1
        return i
    raise ValueError('Invalid JSON value at '+str(i0))
def members(s):
    i=0
    while s[i].isspace(): i+=1
    assert s[i]=='{'; i+=1
    while True:
        while s[i].isspace() or s[i]==',': i+=1
        if s[i]=='}': return
        j=end_value(s,i); key=json.loads(s[i:j]); i=j
        while s[i].isspace(): i+=1
        assert s[i]==':'; i+=1
        while s[i].isspace(): i+=1
        j=end_value(s,i)
        yield key,s[i:j]
        i=j
def project(s, allowed):
    return {k:json.loads(v) for k,v in members(s) if k in allowed}
def array_values(s):
    i=0
    while s[i].isspace(): i+=1
    assert s[i]=='['; i+=1
    while True:
        while s[i].isspace() or s[i]==',': i+=1
        if s[i]==']':return
        j=end_value(s,i);yield s[i:j];i=j
def runtime_task(s):
    out=project(s,{'task_id','conversation_id'})
    for k,v in members(s):
        if k=='input':out['input']=[project(m,{'speaker','text'}) for m in array_values(v)]
    return out
