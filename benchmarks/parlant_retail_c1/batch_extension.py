"""C1: dependency-injected batch-size policy plus read-only batch telemetry.

The original classifier, candidate ordering, batch constructors, prompts,
processing, result mapping, relationship resolution and retries are delegated.
"""
from collections import Counter
from contextvars import ContextVar
import math
from uuid import uuid4

from lagom import Singleton
from parlant.core.engines.alpha.optimization_policy import BasicOptimizationPolicy, OptimizationPolicy
from parlant.core.engines.alpha.guideline_matching.generic.generic_guideline_matching_strategy import GenericGuidelineMatchingStrategy
from parlant.core.engines.alpha.guideline_matching.guideline_matcher import GuidelineMatchingStrategyResolver, GuidelineMatcher
from parlant.core.engines.alpha.guideline_matching.generic.guideline_actionable_batch import GenericActionableGuidelineMatchingBatch
from parlant.core.engines.alpha.guideline_matching.generic.observational_batch import GenericObservationalGuidelineMatchingBatch
from parlant.core.engines.alpha.guideline_matching.generic.guideline_previously_applied_actionable_batch import GenericPreviouslyAppliedActionableGuidelineMatchingBatch
from parlant.core.engines.alpha.guideline_matching.generic.guideline_previously_applied_actionable_customer_dependent_batch import GenericPreviouslyAppliedActionableCustomerDependentGuidelineMatchingBatch

ELIGIBLE = (
    GenericActionableGuidelineMatchingBatch,
    GenericObservationalGuidelineMatchingBatch,
    GenericPreviouslyAppliedActionableGuidelineMatchingBatch,
    GenericPreviouslyAppliedActionableCustomerDependentGuidelineMatchingBatch,
)
MATCH_BATCH_CONTEXT = ContextVar("c1_observed_matching_batch", default=None)


class ObservedControlPolicy(BasicOptimizationPolicy):
    observer = None

    def get_guideline_matching_batch_size(self, guideline_count, hints={}):
        size = super().get_guideline_matching_batch_size(guideline_count, hints)
        if self.observer:
            self.observer.record("matching_batch_size", category=getattr(hints.get("type"), "__name__", str(hints.get("type"))), candidate_count=guideline_count, default_size=size, selected_size=size, policy=type(self).__name__)
        return size


class C1BatchPolicy(ObservedControlPolicy):
    def get_guideline_matching_batch_size(self, guideline_count, hints={}):
        baseline = BasicOptimizationPolicy.get_guideline_matching_batch_size(self, guideline_count, hints)
        size = max(baseline, min(2, guideline_count)) if hints.get("type") in ELIGIBLE else baseline
        if self.observer:
            self.observer.record("matching_batch_size", category=getattr(hints.get("type"), "__name__", str(hints.get("type"))), candidate_count=guideline_count, default_size=baseline, selected_size=size, policy=type(self).__name__)
        return size


class ObservedGenericStrategy(GenericGuidelineMatchingStrategy):
    observer = None

    async def create_matching_batches(self, guidelines, context):
        round_id = uuid4().hex
        batches = await super().create_matching_batches(guidelines, context)
        if self.observer:
            groups = {}
            for batch in batches:
                name = type(batch).__name__
                info = groups.setdefault(name, {"batch_sizes": [], "rule_ids": [], "batch_ids": []})
                mapping = {str(k): str(g.id) for k, g in getattr(batch, "_guidelines", {}).items()}
                batch_id = uuid4().hex
                info["batch_sizes"].append(batch.size)
                info["rule_ids"].extend(mapping.values())
                info["batch_ids"].append(batch_id)
                batch._c1_observation = {"round_id": round_id, "batch_id": batch_id, "category": name, "local_to_rule_id": mapping, "batch_size": batch.size}
            self.observer.record("matching_round", round_id=round_id, input_rule_ids=[str(g.id) for g in guidelines], categories=groups)
        return batches


def container_callbacks(group, observer):
    """Called separately in isolated worker processes, before dependencies resolve."""
    policy = C1BatchPolicy() if group == "C1" else ObservedControlPolicy()
    policy.observer = observer

    async def configure(container):
        container[OptimizationPolicy] = policy
        container[GenericGuidelineMatchingStrategy] = Singleton(ObservedGenericStrategy)
        return container

    async def initialize(container):
        strategy = container[GenericGuidelineMatchingStrategy]
        strategy.observer = observer
        resolver = container[GuidelineMatchingStrategyResolver]
        matcher = container[GuidelineMatcher]
        assert strategy._optimization_policy is policy
        assert resolver._generic_strategy is strategy
        assert matcher.strategy_resolver is resolver
        observer.record("batch_injection_verified", group=group, policy=type(policy).__name__, strategy=type(strategy).__name__, matcher=type(matcher).__name__, resolver=type(resolver).__name__, eligible_categories=[x.__name__ for x in ELIGIBLE], control_default_equal=group == "CONTROL")

    return configure, initialize


def install_batch_observation(observer):
    """Observe process return/error; no alternate inference, validation or repair."""
    for cls in ELIGIBLE:
        original = cls.process

        async def tracked(self, _original=original):
            info = getattr(self, "_c1_observation", {"category": type(self).__name__, "batch_size": self.size})
            token = MATCH_BATCH_CONTEXT.set(info)
            observer.record("matching_batch_started", **info)
            try:
                result = await _original(self)
                observer.record("matching_batch_finished", **info, returned_rule_ids=[str(m.guideline.id) for m in result.matches], success=True)
                return result
            except BaseException as exc:
                observer.record("matching_batch_finished", **info, success=False, error_type=type(exc).__name__)
                raise
            finally:
                MATCH_BATCH_CONTEXT.reset(token)

        cls.process = tracked
