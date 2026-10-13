# PT-SFT1-BINDING-R1

The PEFT 0.18.1 temporary disable/re-enable context enables adapter gradients on exit. The planned inference comparison now rejects an initially trainable model, snapshots each named parameter object and flag, restores those flags in a finally block, and rejects a changed parameter collection or identity. Unknown new parameters are never silently frozen. Original postconditions, finite/nonzero weights, active-module participation, adapted logits and 1e-4 comparison tolerances remain.

Eight fixed-parameter CPU regressions use the installed PEFT methods; the exact schema-router regression remains independent. These tests do not load Qwen, initialize random LoRA, run an optimizer or call an API.

The actual check reuses PT-SFT1-FINALIZE-R1 published weights and the original shortest TRAIN prompt. It performs the same base/disabled/enabled forward checks without generation. After success, the original DEV-V2 30 tasks run once in frozen order with the same provider, decoder, scorer, boundaries and original recovery limits. SingleToolBatchSchema alone uses the local adapter; other schemas retain the reference provider.

The one formal epoch remains64 steps. No training, sanity, VALID, DATA or B0 rerun occurs. Main comparison is the pre-frozen20 tasks; nine contract-affected units and the missing B0 F3-04 stay separate. USD6.337684496 carried forward under the same USD10 total cap; the existing reevaluation reserve is part of the remaining balance. Runtime, locks, trajectories, data and weights remain outside Git.
