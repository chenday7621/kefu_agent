"""Decode the actual native rendered staged-event section, preserving all passage offsets/text."""
import ast,json,re

def prompt_passages(prompt):
    match=re.search(r'Prioritize their data over any other sources and use their details to complete your task: ###\s*(.*?)\s*###',prompt,re.S)
    if not match:return []
    # Native PromptBuilder renders a Python list of JSON event strings.
    events=[json.loads(x) for x in ast.literal_eval(match.group(1))]
    return [p for e in events for c in e['data'].get('tool_calls',[]) if c['tool_id'].endswith(':cloud_bm25_r0') for p in c['result']['passages']]

def checks(prompt,expected,staged=None):
    rendered=prompt_passages(prompt)
    return [{'document_id':p['document_id'],'exact_staged_payload':p in staged if staged is not None else 'unavailable','id_in_prompt':p['document_id'] in prompt,'full_text_in_prompt':any(x['document_id']==p['document_id'] and x['text']==p['text'] for x in rendered)} for p in expected],rendered
