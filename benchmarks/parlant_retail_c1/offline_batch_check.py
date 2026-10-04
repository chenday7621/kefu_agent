"""Exercise the installed matcher/classifier and mapping with fake model responses.
No API requests. Does not measure model matching accuracy.
"""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import inspect
import json
from pathlib import Path
from types import SimpleNamespace
from typing import get_type_hints, get_args
from unittest.mock import MagicMock, AsyncMock, patch
from lagom import Container, Singleton
import tiktoken

from batch_extension import (C1BatchPolicy, ObservedControlPolicy, ObservedGenericStrategy,
    ELIGIBLE, container_callbacks, install_batch_observation)
from observe import Observer
from parlant.core.engines.alpha.optimization_policy import BasicOptimizationPolicy, OptimizationPolicy
from parlant.core.engines.alpha.guideline_matching.generic.generic_guideline_matching_strategy import GenericGuidelineMatchingStrategy
from parlant.core.engines.alpha.guideline_matching.generic_guideline_matching_strategy_resolver import GenericGuidelineMatchingStrategyResolver
from parlant.core.engines.alpha.guideline_matching.guideline_matcher import GuidelineMatcher, GuidelineMatchingStrategyResolver
from parlant.core.engines.alpha.guideline_matching.guideline_matching_context import GuidelineMatchingContext
from parlant.core.engines.alpha.guideline_matching.generic.guideline_low_criticality_batch import GenericLowCriticalityGuidelineMatchingBatch
from parlant.core.guidelines import Guideline, GuidelineId, GuidelineContent
from parlant.core.common import Criticality


class FakeMeter:
    def create_duration_histogram(self, **kwargs):
        @asynccontextmanager
        async def measure(**kwargs):
            yield
        return SimpleNamespace(measure=measure)


def context(applied=()):
    return GuidelineMatchingContext(
        agent=SimpleNamespace(id="offline-agent", name="Retail support", description="offline test"),
        session=SimpleNamespace(id="offline-session", agent_states=[SimpleNamespace(applied_guideline_ids=list(applied))] if applied else []),
        customer=SimpleNamespace(id="offline-customer", name="Offline customer"),
        context_variables=[], interaction_history=[], terms=[], capabilities=[], staged_events=[], active_journeys=[], journey_paths={})


def rule(i, category="actionable", action="offline action"):
    return Guideline(id=GuidelineId(str(i)), creation_utc=datetime.now(timezone.utc),
        content=GuidelineContent("offline condition " + str(i), None if category=="observational" else action), enabled=True,
        tags=[], metadata={"customer_dependent_action_data":{"is_customer_dependent":True}} if category=="customer_dependent" else {},
        criticality=Criticality.LOW if category=="low" else Criticality.MEDIUM)


def build_container():
    c=Container()
    for name, typ in get_type_hints(GenericGuidelineMatchingStrategy.__init__).items():
        if name=="return" or typ is OptimizationPolicy: continue
        value=MagicMock()
        if name=="logger": value=MagicMock()
        elif name=="meter": value=FakeMeter()
        elif name=="entity_queries": value=SimpleNamespace(guideline_and_journeys_it_depends_on={})
        elif name=="relationship_store": value=SimpleNamespace(list_relationships=AsyncMock(return_value=[]))
        c[typ]=value
    for name,typ in get_type_hints(GuidelineMatcher.__init__).items():
        if name not in ("return","logger","meter","strategy_resolver"): c[typ]=MagicMock()
    c[GuidelineMatchingStrategyResolver]=Singleton(GenericGuidelineMatchingStrategyResolver)
    c[GuidelineMatcher]=Singleton(GuidelineMatcher)
    return c


async def main():
    observer=Observer()
    containers={};strategies={}
    for group in ("CONTROL","C1"):
        configure, initialize=container_callbacks(group,observer)
        c=await configure(build_container())
        await initialize(c)
        containers[group]=c; strategies[group]=c[GenericGuidelineMatchingStrategy]
    assert containers["CONTROL"][OptimizationPolicy] is not containers["C1"][OptimizationPolicy]
    assert type(containers["CONTROL"][OptimizationPolicy]) is ObservedControlPolicy
    assert type(containers["C1"][OptimizationPolicy]) is C1BatchPolicy
    default=BasicOptimizationPolicy(); control=containers["CONTROL"][OptimizationPolicy];c1=containers["C1"][OptimizationPolicy]
    boundary_rows=[]
    for cls in [*ELIGIBLE,GenericLowCriticalityGuidelineMatchingBatch]:
        for n in (0,1,2,3,9,10,11,20,21,30,31,60):
            hints={"type":cls}; assert control.get_guideline_matching_batch_size(n,hints)==default.get_guideline_matching_batch_size(n,hints)
            if cls not in ELIGIBLE: assert c1.get_guideline_matching_batch_size(n,hints)==default.get_guideline_matching_batch_size(n,hints)
            else: assert c1.get_guideline_matching_batch_size(n,hints)==max(default.get_guideline_matching_batch_size(n,hints),min(2,n))
    for name in ("use_embedding_cache","get_message_generation_retry_temperatures","get_guideline_matching_batch_retry_temperatures","get_response_analysis_batch_retry_temperatures","get_tool_calling_batch_retry_temperatures","get_guideline_proposition_retry_temperatures"):
        assert getattr(c1,name)()==getattr(default,name)()==getattr(control,name)()
    for category in ("actionable","observational","previous","customer_dependent","low"):
        for n in (0,1,2,3,9,10,11,20,21,30,31,60):
            gs=[rule(i,category) for i in range(n)]
            ctx=context([g.id for g in gs] if category in ("previous","customer_dependent") else [])
            out={g:await s.create_matching_batches(gs,ctx) for g,s in strategies.items()}
            flatten=lambda batches:[str(g.id) for b in batches for g in b._guidelines.values()]
            assert flatten(out["CONTROL"])==flatten(out["C1"])==[str(g.id) for g in gs]
            assert len(set(flatten(out["C1"])))==n
            assert all(b._optimization_policy is containers["C1"][OptimizationPolicy] for b in out["C1"])
            boundary_rows.append({"category":category,"count":n,"CONTROL_sizes":[b.size for b in out['CONTROL']],"C1_sizes":[b.size for b in out['C1']]})
    mixed=[rule(i,c) for i,c in enumerate(["actionable","previous","customer_dependent","low","observational"]*3)]
    applied=[g.id for g in mixed if g.content.action and int(g.id)%5 in (1,2)]
    def categorized(bs):
        d={}
        for b in bs:d.setdefault(type(b).__name__,[]).extend(g.id for g in b._guidelines.values())
        return d
    assert categorized(await strategies['CONTROL'].create_matching_batches(mixed,context(applied)))==categorized(await strategies['C1'].create_matching_batches(mixed,context(applied)))
    dup=[rule(1),rule(2),rule(1)]
    assert [g.id for b in await strategies['C1'].create_matching_batches(dup,context()) for g in b._guidelines.values()]==["1","2"]
    # Real original processing and schema validation; deliberately fabricated all-true responses.
    install_batch_observation(observer)
    mapping_checks=[]; risks=[]; tokenizer=tiktoken.encoding_for_model("gpt-4o")
    for cat in ("actionable","observational","previous","customer_dependent"):
        gs=[rule(i,cat) for i in range(3)]
        batches=await strategies['C1'].create_matching_batches(gs,context([g.id for g in gs] if cat in ('previous','customer_dependent') else []))
        for batch in batches:
            import importlib
            mod=importlib.import_module(type(batch).__module__)
            schema=next(v for k,v in vars(mod).items() if k.endswith('MatchesSchema') and inspect.isclass(v))
            check_type=get_args(schema.model_fields['checks'].annotation)[0]
            checks=[]
            for local,g in reversed(list(batch._guidelines.items())):
                values={k:(local if k=='guideline_id' else (True if f.annotation is bool or 'bool' in str(f.annotation) else 'offline synthetic')) for k,f in check_type.model_fields.items() if f.is_required()}
                checks.append(values)
            parsed=schema.model_validate({'checks':checks})
            async def generate(prompt,hints={},_parsed=parsed):return SimpleNamespace(content=_parsed,info=SimpleNamespace())
            batch._schematic_generator=SimpleNamespace(generate=generate)
            output=await batch.process()
            assert [m.guideline.id for m in output.matches]==[g.id for g in reversed(list(batch._guidelines.values()))]
            mapping_checks.append({'category':cat,'batch_size':batch.size,'reverse_response_id_mapping':'PASS'})
            prompt=batch._build_prompt(shots=await batch.shots()).build()
            risks.append({'category':cat,'batch_size':batch.size,'empty_history_estimated_prompt_tokens':len(tokenizer.encode(prompt)),'synthetic_response_tokens':len(tokenizer.encode(parsed.model_dump_json())),'actual_output_cap':8192,'matching_accuracy_tested':False})
    # Saved long-policy actions with real prompt templates, no model calls.
    import sys
    sys.path.insert(0,str(Path(__file__).parent/'configs/CONTROL'))
    from bridge import policy_sections, CONDITIONS
    policy=(Path(__file__).parents[1]/'tau2-bench/data/tau2/domains/retail/policy.md').read_text()
    long_rules=[rule(i,action=s['text']) for i,s in enumerate(policy_sections(policy)) if s['heading'] in CONDITIONS]
    for b in await strategies['C1'].create_matching_batches(long_rules,context()):
        p=b._build_prompt(shots=await b.shots()).build();risks.append({'category':'official_policy_pair','batch_size':b.size,'empty_history_estimated_prompt_tokens':len(tokenizer.encode(p)),'history_and_tool_payload_not_bounded':True})
    print(json.dumps({'status':'PASS','real_container_injection_and_matcher_resolution':True,'category_order_and_coverage_identical':True,'no_accidental_duplicates':True,'boundaries':boundary_rows,'mapping':mapping_checks,'length_risk_checks':risks,'context_limit_proven_safe':False,'output_length_proven_safe':False,'risk_note':'Two checks increase per-request output; rationale and tool/history lengths are unbounded. Existing 8192 output cap and all prompts preserved; model accuracy and worst-case lengths unverified.','model_api_requests':0,'matching_accuracy_tested':False},indent=2))


if __name__=='__main__':asyncio.run(main())
