# Parlant × τ³-bench Retail V0.1

This integration uses the existing Parlant engine as the evaluated agent. The
official τ-bench orchestrator supplies its simulated user, Retail database and
tools, and scorer. `bridge.py` exposes the official tool schemas to Parlant;
each tool call is sent as a ticket to the orchestrator, executed once by the
official environment, and returned to the waiting Parlant tool. The agent sees
the public policy, tool schemas and conversation only.

The seven public Retail policy sections are copied verbatim into Parlant
guidelines. `business_config.py` adds source-labelled policy clarifications
and tool-use hints. These hints do not execute a workflow. Startup checks that
every official Retail tool has a guideline association. `observe.py` records
Parlant model calls, preparation iterations, tool call identity and response
field coverage without making model calls. A fresh Parlant configuration store
is created for each V0.1 run. Official task data, tools and scoring stay intact.

`selection.json` freezes the first five train IDs before task inspection:
`0, 1, 2, 3, 4`. The formal run uses 60 steps, 900 seconds per task, 180
seconds per tool wait, at most three orchestrator errors, and zero task reruns.
The seed does not make external model responses deterministic. An earlier V0
debugging run is preserved under the original results directory.

From the project root, with a mode-600 ignored `.env` containing
`DEEPSEEK_API_KEY`:

```bash
benchmarks/tau2-bench/.venv/bin/python -m unittest discover \
  -s benchmarks/parlant_retail -p 'test_v01_synthetic.py' -v
benchmarks/tau2-bench/.venv/bin/python benchmarks/parlant_retail/run.py \
  --phase all --result-dir results/NEW_V01_RUN
python3 benchmarks/parlant_retail/audit.py results/NEW_V01_RUN
```

Freeze the V0.1 code and configuration in the review directory before the
formal run. The runner refuses to overwrite existing task results and stops
on an environment or adapter error. `audit.py` inspects saved results without
API calls. See the review report for exact versions, results and limitations.
