"""Read-only hooks adapted from O1-B: context ownership, fsync journals, raw provider usage."""
import asyncio,contextvars,hashlib,inspect,json,os,time,uuid
from common_r2 import RESULT,write,digest,durable_text
SCOPE=contextvars.ContextVar('rag_scope',default=('startup','startup'))
CALL=contextvars.ContextVar('rag_call',default=None)
ROWS=[]
EVIDENCE={}
TARGETS={}
FATAL=[]
API_ERRORS=[]

def scope_dir():
    task,attempt=SCOPE.get()
    return RESULT/'startup' if task=='startup' else RESULT/'attempts'/attempt

def record(kind,**fields):
    task,attempt=SCOPE.get();row={'kind':kind,'utc':__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),'task_id':task,'attempt_id':attempt,**fields};ROWS.append(row)
    path=scope_dir()/'observations.jsonl';path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('a') as f:f.write(json.dumps(row,ensure_ascii=False,default=str)+'\n');f.flush();os.fsync(f.fileno())
    return row

def install():
    from parlant.adapters.nlp.deepseek_service import DeepSeekSchematicGenerator
    from parlant.core.engines.alpha.message_generator import MessageGenerator
    from openai.resources.chat.completions import AsyncCompletions
    import httpx2 as httpx
    original_http=httpx.AsyncClient.send
    async def send(self,request,*a,**kw):
        if request.url.host!='api.deepseek.com':return await original_http(self,request,*a,**kw)
        if FATAL:raise RuntimeError('Provider stop rule active')
        rid=uuid.uuid4().hex;start=time.monotonic();record('physical_http_started',http_id=rid,call_id=CALL.get(),method=request.method,path=request.url.path)
        try:
            response=await original_http(self,request,*a,**kw)
            record('physical_http_finished',http_id=rid,call_id=CALL.get(),status=response.status_code,seconds=time.monotonic()-start)
            if response.status_code in (401,402,403):FATAL.append({'status':response.status_code,'reason':'provider authentication/balance failure'})
            if response.status_code>=400:API_ERRORS.append(response.status_code)
            else:API_ERRORS.clear()
            if len(API_ERRORS)>=3:FATAL.append({'reason':'three consecutive provider HTTP errors','statuses':list(API_ERRORS)})
            return response
        except BaseException as exc:
            record('physical_http_finished',http_id=rid,call_id=CALL.get(),status='unavailable',error_type=type(exc).__name__,seconds=time.monotonic()-start)
            API_ERRORS.append('unavailable')
            if len(API_ERRORS)>=3:FATAL.append({'reason':'three consecutive transport/provider errors','statuses':list(API_ERRORS)})
            raise
    httpx.AsyncClient.send=send
    original_create=AsyncCompletions.create
    async def create(self,*a,**kw):
        assert kw['model']=='deepseek-chat'
        record('provider_request_started',call_id=CALL.get(),request_model=kw['model'],parameters={k:v for k,v in kw.items() if k!='messages'},messages_sha256=digest(kw['messages']))
        response=await original_create(self,*a,**kw)
        record('provider_response_received',call_id=CALL.get(),request_model=kw['model'],response_model=response.model,raw_usage=response.usage.model_dump() if response.usage else 'unavailable')
        return response
    AsyncCompletions.create=create
    original_generate=DeepSeekSchematicGenerator._do_generate
    async def generate(self,prompt,hints={}):
        call=uuid.uuid4().hex;token=CALL.set(call);built=prompt.build() if hasattr(prompt,'build') else str(prompt);path=scope_dir()/'prompts'/f'{call}.txt';path.parent.mkdir(parents=True,exist_ok=True);durable_text(path,built)
        start=time.monotonic();record('model_call_started',call_id=call,schema=self.schema.__name__,request_model=self.model_name,prompt_sha256=hashlib.sha256(built.encode()).hexdigest(),hints=dict(hints))
        try:
            result=await original_generate(self,prompt,hints)
            record('model_call_finished',call_id=call,schema=self.schema.__name__,success=True,seconds=time.monotonic()-start)
            return result
        except BaseException as exc:
            record('model_call_finished',call_id=call,schema=self.schema.__name__,success=False,error_type=type(exc).__name__,seconds=time.monotonic()-start);raise
        finally:CALL.reset(token)
    DeepSeekSchematicGenerator._do_generate=generate
    from parlant.core.engines.alpha.canned_response_generator import CannedResponseGenerator
    from coverage import checks
    def install_builder(cls,name):
        original=getattr(cls,name);signature=inspect.signature(original)
        def build(self,*a,**kw):
            args=signature.bind(self,*a,**kw).arguments
            builder=original(self,*a,**kw)
            native=builder.build();session=str(args['session'].id);expected=EVIDENCE.get(session,[]);target=TARGETS[session];staged=[]
            for e in args['staged_tool_events']:
                for c in e.data.get('tool_calls',[]):
                    if c['tool_id'].endswith(':cloud_bm25_r0'):staged.extend(c['result']['data']['passages'])
            assert expected==target['top5'] and staged==expected and len(expected)==5
            from parlant.core.sessions import EventKind,EventSource
            messages=[{'speaker':'user' if e.source==EventSource.CUSTOMER else 'agent','text':e.data['message']} for e in args['interaction_history'] if e.kind==EventKind.MESSAGE]
            assert messages==target['task']['input']
            change={'all_other_sections_unchanged':True,'changed_sections':[]}
            if target['candidate']=='R2-EVIDENCE':
                assert name=='_build_draft_prompt', 'R2 modifies only CANNED_FLUID final draft builder'
                from evidence import replace,wrapped_passages
                change=replace(builder,target['task'],expected)
                change['changed_sections']=['BuiltInSection.STAGED_EVENTS']
                built=builder.build();rendered=wrapped_passages(built)
                rows=[{'document_id':p['document_id'],'exact_staged_payload':p in staged,'id_in_prompt':p['document_id'] in built,'full_text_in_prompt':any(x['document_id']==p['document_id'] and x['text']==p['text'] for x in rendered)} for p in expected]
                assert rendered==[{k:p[k] for k in ['document_id','title','url','text']} for p in expected]
            else:
                assert target['candidate']=='CONTROL'
                built=builder.build();assert built==native
                rows,rendered=checks(built,expected,staged)
                assert rendered==expected
            durable_text(scope_dir()/'native_generation_prompt.txt',native)
            durable_text(scope_dir()/'final_generation_prompt.txt',built)
            import difflib
            durable_text(scope_dir()/'generation_prompt.diff',''.join(difflib.unified_diff(native.splitlines(True),built.splitlines(True),fromfile='native',tofile=target['candidate'])))
            record('generation_evidence_format',session_id=session,candidate=target['candidate'],native_prompt_sha256=hashlib.sha256(native.encode()).hexdigest(),final_prompt_sha256=hashlib.sha256(built.encode()).hexdigest(),native_history_matches_frozen_input=True,change=change)
            record('final_prompt_coverage',session_id=session,prompt_sha256=hashlib.sha256(built.encode()).hexdigest(),builder=cls.__name__+'.'+name,candidate=target['candidate'],expected_passages=len(expected),staged_passages=len(staged),rendered_passages=len(rendered),passages=rows,all_evidence_covered=staged==expected and len(rendered)==5 and all(c['id_in_prompt'] and c['full_text_in_prompt'] for c in rows))
            return builder
        setattr(cls,name,build)
    install_builder(MessageGenerator,'_build_prompt')
    install_builder(CannedResponseGenerator,'_build_draft_prompt')
