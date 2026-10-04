import asyncio,dataclasses,json,hashlib
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from common import *

async def main():
    import run
    from parlant.adapters.db.transient import TransientDocumentDatabase
    from parlant.core.agents import AgentStore,AgentDocumentStore
    from parlant.core.guidelines import GuidelineStore,GuidelineDocumentStore
    from parlant.core.engines.alpha.perceived_performance_policy import PerceivedPerformancePolicyProvider
    gen=NS(generate=lambda s:hashlib.sha256(str(s).encode()).hexdigest()[:10])
    async with AgentDocumentStore(gen,TransientDocumentDatabase()) as agents, GuidelineDocumentStore(gen,TransientDocumentDatabase()) as rules:
        server=NS(container={AgentStore:agents,GuidelineStore:rules,PerceivedPerformancePolicyProvider:PerceivedPerformancePolicyProvider(run.Quiet())},_guideline_evaluations={},get_agent=AsyncMock())
        await run.restore_effective(server)
        old=json.loads((RESULT/'startup/effective_configuration.json').read_text())
        assert json.loads(json.dumps([dataclasses.asdict(x) for x in await agents.list_agents()],default=str))==old['agent']
        assert json.loads(json.dumps([dataclasses.asdict(x) for x in await rules.list_guidelines()],default=str))==old['guidelines']
        assert not server._guideline_evaluations
        write(REVIEW/'OFFLINE_RESTORE_CHECK.json',{'agent_exact':True,'guidelines_exact':True,'new_evaluation_scheduled':0,'paid_calls':0});print('exact frozen snapshot restore passed')
if __name__=='__main__':asyncio.run(main())
