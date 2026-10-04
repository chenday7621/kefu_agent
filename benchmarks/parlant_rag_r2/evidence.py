"""Only replace native STAGED_EVENTS in the CANNED_FLUID draft generation builder."""
import dataclasses,json,re
RULES='''Generation rules:
1. Answer only based on retrieved evidence.
2. If evidence is insufficient, explicitly state uncertainty.
3. Do not invent details absent from evidence.
4. Prefer concise answer and mention source when useful.'''
def wrapper(task,passages):
    assert len(passages)==5
    prefix='User conversation history:\n'+json.dumps(task['input'][:-1],ensure_ascii=False,indent=2)+'\n\nCurrent question:\n'+task['input'][-1]['text']+'\n\nRetrieved evidence:\n'
    blocks=[]
    for i,p in enumerate(passages,1):
        assert f'[/Evidence {i}]' not in p['text']
        blocks.append(f"[Evidence {i}]\nSource ID: {p['document_id']}\nTitle: {p['title']}\nSource URL: {p['url']}\nContent:\n{p['text']}\n[/Evidence {i}]")
    return prefix+'\n\n'.join(blocks)+'\n\n'+RULES
def wrapped_passages(prompt):
    rows=[]
    for i in range(1,6):
        start=f'[Evidence {i}]\n';end=f'\n[/Evidence {i}]'
        assert prompt.count(start)==1 and prompt.count(end)==1
        block=prompt.split(start,1)[1].split(end,1)[0]
        head,text=block.split('\nContent:\n',1)
        lines=head.split('\n');assert len(lines)==3
        rows.append({'document_id':lines[0].removeprefix('Source ID: '),'title':lines[1].removeprefix('Title: '),'url':lines[2].removeprefix('Source URL: '),'text':text})
    return rows
def replace(builder,task,passages):
    from parlant.core.engines.alpha.prompt_builder import BuiltInSection,PromptSection
    key=BuiltInSection.STAGED_EVENTS;old=builder.sections.copy();assert key in old
    content=wrapper(task,passages)
    builder.sections[key]=PromptSection(template='{evidence_wrapper}',props={'evidence_wrapper':content},status=old[key].status)
    assert list(builder.sections)==list(old)
    assert all(builder.sections[k] is v for k,v in old.items() if k!=key)
    built=builder.build();rendered=wrapped_passages(built)
    assert rendered==[{k:p[k] for k in ['document_id','title','url','text']} for p in passages]
    assert content in built
    return {'only_changed_section':'BuiltInSection.STAGED_EVENTS','all_other_section_objects_unchanged':True,'history_and_current_question_repeated_verbatim_in_wrapper':True,'native_interaction_history_section_unchanged':True,'wrapper_sha256':__import__('hashlib').sha256(content.encode()).hexdigest(),'rendered':rendered}
