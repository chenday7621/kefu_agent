# Post-training Phase 1-A/B and frozen Phase 1-C DEV protocol

This is an opt-in APP experiment extension. The production APP entry point and
all historical Retail/O1-B runtimes are unchanged. The APP extension's default
backend remains disabled. An explicitly invoked single-generation local smoke
backend now exists; it never dispatches a tool.

The storage root is `/mnt/nvme3/chenyi/posttraining`. Activate with:

```bash
source /home/chenyi/miniconda3/etc/profile.d/conda.sh
conda activate /mnt/nvme3/chenyi/posttraining/conda-envs/qwen-policy
source /mnt/nvme3/chenyi/posttraining/runtime/activate.sh
cd /home/chenyi/kefu_agent
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 CUDA_VISIBLE_DEVICES="" \
  python -m experiments.posttraining.offline_checks --review "$POSTTRAIN_ROOT/runtime/offline-checks"
```

`policy.SchemaRouter(frozen, qwen)` routes by **class identity**, exclusively
`SingleToolBatchSchema`. `CannedResponseDraftSchema` and every other schema,
embeddings, moderation and streaming delegate to the provided frozen service.
Wrap the APP's existing `nlp_factory` with this router in a future independent
launcher. `integration.install_container(container, qwen, trajectory)` is its
`configure_container` callback. It installs lazy batcher/caller factories and
preparation hooks; it does not initialize NLP providers or start any service.
Do not enable this extension in the historical benchmark launchers.
For an independently authorized APP experiment, `app_extension` is an existing
SDK module extension point (`PARLANT_SDK_MODULE` plus the explicit
`POSTTRAIN_APP_POLICY=phase1a-interface` flag). It binds exactly
`SchematicGenerator[SingleToolBatchSchema]` before NLP initialization; Parlant's
`try_define` preserves this binding. All other generator bindings still use the
original frozen provider. The module retains a disabled Qwen backend and is not
activated by the environment setup script or offline checks.

The true resolved candidate is bound by `ObservedSingleToolBatch.process`.
`QwenPolicyProvider` uses the official tokenizer `apply_chat_template`, passing
only visible parameters of that candidate as `tools`. It preserves the native
Parlant prompt and adds the native Pydantic output schema as instructions.
Thinking is explicitly disabled. Parse with the same `SingleToolBatchSchema`
model; additionally reject hidden, trusted and unknown argument names.
Return the native schema into the unchanged evaluation gate. Qwen cannot invoke
MCP, bind a customer, mint a capability or change confirmation/snapshot logic.

Exact causal links are carried by `PolicyGenerationResult.policy_step_id`,
then `AttributedEvaluations.policy_step_id` (including empty NO_CALL lists).
Native evaluator output `ToolCall.id` is registered against that exact ID.
`ObservedToolCaller` delegates to the original caller and logs dispatch and
the original `ToolCallResult.id`. Next-iteration hooks link explicitly to the
previous round's policy IDs and result IDs. The candidate decisions are separate
even when they run concurrently. There is no "last LLM call" attribution.

Each JSONL line is a full snapshot, appended with an increasing per-step
`record_sequence`. Materialize each step by selecting its latest sequence.
Multiple calls, parents and successor candidates use arrays; singleton fields
are null for multiple IDs. Actual arguments are recorded at the original
ToolCaller dispatch boundary; native MCP argument conversion and backend token
injection still occur downstream. These are not raw HTTP wire payloads.
Native MCP request IDs and emitted event IDs are not claimed in this phase.

`finish_episode` is an explicit experiment controller API. A preparation ending
or an APP reply is not assumed to be a terminal episode/reward. Unknown terminal
status and reward remain null. Fixtures are named `APP/fixture/...` and are not
training data. System, user, history, tool facts and schemas are input-only;
future token spans/logprobs are unavailable, and unconsumed reasoning/name
echoes must be masked. Phase 0's split policy continues to apply.

Dependencies are intentionally limited to the repository, its PostgreSQL driver,
CUDA PyTorch, tokenizer/Transformers/Accelerate/PEFT/Datasets and offline check
tools. TRL/vLLM/SGLang are deferred. See the Phase 1-A review for exact locks and
the Phase 1-B plan; do not run inference or an APP service in Phase 1-A.

Dependency replay, after creating/activating the independent prefix and cache
paths above (installation only):

```bash
python -m pip install --no-cache-dir torch==2.8.0 --index-url https://download.pytorch.org/whl/cu126
python -m pip install --no-cache-dir -c experiments/posttraining/app_constraints.txt \
  -e . -r experiments/posttraining/requirements.txt
```

Critical APP dependency versions follow the repository's uv.lock. Torch/Transformers
use the experiment's smaller CUDA 12.6 / Transformers 4.x stack, satisfying the
repository's declared minimums and official Qwen3 support. Complete installed
versions are also saved in the review's environment artifact and /mnt logs.

`local_qwen.LocalQwenSmokeBackend` requires bf16 on exactly one visible GPU,
eval/inference_mode, offline local files, a frozen rendered prompt, and an explicit
single-call budget. It passes `use_model_defaults=False, do_sample=False` to
Transformers and asserts the class decoder's actual config. `smoke_phase1b`
supports `--require-call` and `--no-call`; it blocks networking and native tool
dispatch and stops after the native gate. The historical sampled CALL is not
greedy evidence. The final corrected CALL and prior corrected NO_CALL are separate
audit artifacts outside version control.

`dev_protocol` generates/verifies the 30 new DEV definitions on CPU, and `scorer`
consumes explicit controller event bindings plus normalized DB snapshots without
an LLM judge. Generated tasks, manifests, traces and scores belong on NAS and
are not committed. `dev_protocol_lock.json` versions their hashes. See
`SCORER_SPEC.md` and `PHASE1C_RUN_PLAN.md` for the frozen evidence contract and
the next-run budget. The current smoke CLI is not a 30-episode APP baseline
controller; that controller requires a separately authorized integration step.

CPU-only checks:

```bash
source experiments/posttraining/phase1b_env.sh
python -m unittest experiments.posttraining.test_dev_protocol -v
python -m experiments.posttraining.dev_protocol verify --bundle <frozen-NAS-bundle>
```
