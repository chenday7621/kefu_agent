# Independent DATA visible-context contract

Only `data_expert.py`, its native PromptBuilder regression tests, and DATA
contract records change. The expert's `choose()` rule, actual Qwen prompt,
adapter, decoder, APP/core, DEV/scorer, and split policy remain unchanged.

- V1 looked for nonexistent wrapped section keys and omitted actual tool
  observations. Original failed episodes and freezes remain preserved.
- V2 reads native `BuiltInSection.INTERACTION_HISTORY` and `STAGED_EVENTS`
  props. Invalid formats and unbound successful results fail explicitly.
- V2.1 applies the existing policy text sanitizer to structured visible
  payloads, including bound identity fields, without changing actual prompts.
- V2.2 copies DATA input-reference observations using that same sanitizer.
  Original raw ToolResults remain separate and unchanged; episode, policy,
  ToolCall and ToolResult IDs remain exact. The DATA context is restored before
  native gate/execution. This keeps input references consistent with model
  visibility for the unchanged lineage validator.

The final reader passed 18 related CPU tests, including real native section
construction and read-only recovery of all 24 previously omitted historical
decisions. Existing complete records were checked for compatible input
references; they are not retroactively described as having run one reader
version. Failed whole episodes remain excluded even when individual decisions
parse correctly. All supervision is registered independent TRAIN/SFT-VALID;
DEV, Oracle solutions, B0 trajectories and corrected DEV answers are excluded.

Acquisition stopped during the 62nd TRAIN fixture materialization because of
a PostgreSQL connection timeout followed by a cleanup `ObjectInUse` error.
Three earlier DATA recovery actions remain recorded. The explicit three-recovery
limit in plan section 6.2 applies to B0; it is not cited as an explicit DATA
authorization limit. The new PG materialization/cleanup failure is the actual
stop reason. The remaining owned database was backed up and cleaned without
starting an episode. This is
an infrastructure stop, not a policy score or an SFT outcome. No LoRA training
or post-SFT evaluation started; incomplete data cannot satisfy the unchanged
training minimum. Full records, data and database backups remain outside Git.
