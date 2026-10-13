# First policy SFT execution

PT-B0-R1-CONTRACTFIX runs the fixed DEV-V2 order once with the unchanged greedy
Qwen policy binding. Normal negative scores continue. The only APP exception is
the separately authorized two Outbox error-payload type guards and their tests.
The old incomplete B0 evidence and costs remain separate and cumulative.

The subsequent entry does not start B0 or repeat the Oracle batch. It requires
the original30 planned units, with the explicitly authorized F3-04 missing case:23 original scored units and6 post-boundary scored units. It requires passed attribution, waits for owned resource
shutdown, and verifies all frozen B0 source hashes against the explicit three-file boundary addendum. Only newly added training
modules are committed before the independent acquisition freeze.

```bash
# POSTTRAIN_PYTHON: the isolated locked Conda Python executable
# PLAN and DEFINITIONS: local private plan and previously frozen independent definitions
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 TOKENIZERS_PARALLELISM=false
"$POSTTRAIN_PYTHON" -m experiments.posttraining.research.boundary_continuation --plan "$PLAN"
"$POSTTRAIN_PYTHON" -m experiments.posttraining.research.pipeline \
  --plan "$PLAN" --definitions "$DEFINITIONS" \
  --checkpoint-helper "$CHECKPOINT_HELPER"
```

The checkpoint helper uses a temporary askpass process reading the local ignored
0600 `.env`; tokens are not embedded in Git URLs, arguments, logs or files.
The helper is session-owned, not an installed credential store. Generated data,
trajectories, databases, adapters and audit directories stay outside Git.

The 200 TRAIN and40 SFT-VALID definitions preserve their registered order and
hash. Approved split.policy.v2 shares capabilities but isolates causal-template
groups, variants, episodes and decision steps. H1–H4 and legacy remain excluded.
The expert sees actual current user messages and native-visible tool results;
fixture/terminal expectations are accessible only to the separate verifier.
No DEV trajectory, Oracle answer or DEV correction is a target or expert input.

Every candidate uses the same native parser/gate and real isolated MCP/PG path.
Whole-episode validation does not admit every decision: step action, exact
argument source paths, pre-decision event IDs, confirmation, native execution
and results are independently checked. All steps of an episode share its split.
Targets include the complete strict policy JSON plus one Qwen end marker;
all input and padding tokens have label -100. Native prefix IDs remain exact.
Length selection is4096 then8192 then16384, never truncation. More than5% excluded
or loss of a whole critical family/action stratum blocks training.

The dataset gates remain100 TRAIN episodes,20 VALID episodes,1000 TRAIN samples,
200 CALL and200 hard NO_CALL minimum. CALL and hard NO_CALL are retained;
simple NO_CALL is capped at2 per joint round with seed42. No duplicate filling.
USD10 total includes old attempts and all native paid responses; at leastUSD2
is protected for reevaluation, and unknown usage retains its reservation.

Training has exactly the plan's fixed LoRA settings and one epoch. A separate
adapter first tests the real longest TRAIN sample and at most2 optimizer steps.
It must show finite gradients, changed LoRA, frozen base and exact save/reload.
Formal training creates fresh seed42 weights and optimizer, never the sanity
adapter. Completion loss retains full causal attention and every target token;
only the ignored LM-head positions are omitted using installed Qwen's public
`logits_to_keep`. CPU tests prove equivalence to full masked causal loss and
gradient accumulation's token denominator. No context reduction, quantization,
extra card, second epoch or score-driven checkpoint choice is automatic.

Final epoch1 is reevaluated on the original30 DEV tasks with the original
decoder, ordinary response provider, patched APP, native budgets and scorer.
Improvement, ties and regressions are all archived. This is a limited DEV
comparison with shared capabilities and isolated concrete templates/episodes,
not an unseen broad-family or clean-test generalization claim.

The boundary.contract.v2 addendum preserves parseable unknown/protected-name
proposals and confirms rejection using native validate_tool_arguments before
the zero-argument converter can erase them. CANNOT_RUN is not NO_CALL; no fake
ToolCall/ToolResult is created. Actual configured trusted value/secret exposure
stops the run. Native scalar/default validation and all business constraints
remain unchanged. Generation cancellation has a distinct unexecuted record.

Original23 results and scores are immutable. Their saved raw completions undergo
CPU compatibility checks; changed units are explicitly excluded from the
primary paired set, never relabeled under the new contract. F3-04 consumed its
30 decisions and remains unscored/incomplete. Only the six previously unstarted
units execute after the addendum. EVAL_COMPARABILITY freezes the eligible paired
IDs before acquisition/training; reevaluation still includes all30 and reports
the excluded and missing units separately. Planned denominator30, completion
fraction and completed-case success are distinct; missing reward is null.
