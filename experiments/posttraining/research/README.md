# Qwen B0 / first LoRA research preparation

These modules prepare an isolated research path. They are **not evidence of a completed model experiment**. The initial run is blocked before paid/model execution by the missing reference-provider credential and the unapproved independent TRAIN/SFT-VALID family split. The existing 30-task Oracle acceptance is preserved and must not be repeated.

Implemented preparation:

- One resident Qwen policy backend with a serial queue and explicit request IDs; native official chat template remains in the existing policy adapter.
- Original reference DeepSeek generation for every non-policy schema, with cross-process reservations before each request. Total USD10; at least USD2 is protected for post-SFT evaluation. Unknown outcomes keep their reservation. SDK retries are disabled; native physical API invocations each receive a separate ledger ID.
- Original neural multilingual MiniLM embedder with a locked local snapshot and persistent native cache. Oracle action/reply/embedding branches are absent from the formal entrance.
- A scripted user actor that receives only the opening, current user-known fields and frozen followups. It confirms only the latest actually displayed phrase matching current user intent.
- Explicit parser-failure handling: raw output and parse failure remain recorded; the unchanged native gate consumes empty evaluable actions without dispatch. No successful parsed proposal is invented and no second generation is allowed.
- Exact journal compatibility, independent sample-admission checks, completion-only labels and a collator that preserves real EOS when EOS is also the padding ID.
- A guarded one-epoch PEFT training entrance. It refuses undersized/unverified/DEV data and requires separate sanity evidence. **The scenario generator/expert, collection orchestration, real binding validation and training sanity artifacts still require completion after split/provider blockers are resolved.**

## Commands

Use the existing isolated Python prefix and set cache/storage environment before import. Provide `POSTTRAIN_MODEL_PATH` and the actual private run config; generated evidence stays outside Git.

```bash
python -m unittest experiments.posttraining.research.test_offline
python -m experiments.posttraining.research.runner --plan "$POSTTRAIN_RUN_CONFIG" --status
python -m experiments.posttraining.research.runner --plan "$POSTTRAIN_RUN_CONFIG" --resume
python -m experiments.posttraining.research.train --config "$POSTTRAIN_TRAIN_CONFIG" --validate-only
```

`run.example.json` is a configurable example, not a ready-to-spend run. The research runner currently provides persisted preflight/status and a guarded B0 batch entrance; it reports missing later-stage artifacts explicitly. It does not invent a dataset, sanity run, checkpoint or evaluation. A formal `B0_FREEZE.json` must contain the reviewed controller config, tested code commit, model/provider/embedding locks, owned cluster marker, source hashes and budget authorization before a model can run.

## Resume prerequisites

1. Configure only `DEEPSEEK_API_KEY` locally; never send or commit a credential. Git authentication and model-service keys remain separate.
2. Approve/register a defensible family-disjoint TRAIN/SFT-VALID pair or explicitly amend the split policy. No amendment is inferred from a dollar-budget approval.
3. Complete the remaining integration/data/sanity orchestration and targeted model-free checks, commit the tested B0 version, then run the first formal task as the counted canary.

The protocol stays at 30 candidate decisions, 12 user turns and 18 native tool calls per DEV-V2 episode. Failed model tasks remain failures; completed tasks are never replayed for score selection. Original APP/core/business migrations and scorer are unchanged.

The model remains official Qwen3-8B revision `b968826d9c46dd6066d109eabc6255188de91218`; bf16, no quantization, thinking disabled, greedy, `use_model_defaults=False`, max 1024 new tokens. One approved idle A100 at a time. The first LoRA configuration is fixed by the execution plan; no second SFT round, RL or deployment.
