# DATA initialization and first-prefix collection addendum

This user-authorized addendum changes only DATA runner/materializer/cleanup,
related tests, collection order and stopping point. APP/core, the APP 5-second
connection contract, expert `choose()`, DATA_VISIBLE_CONTEXT_V2.2, prompts,
models, decoder, DEV/scorer and split ownership remain frozen.

Initialization checks the exact run-owned cluster, database owner, native
migration hashes and pre-native fixture state before writes. A known transient
connection timeout permits at most two extra attempts (1s/2s backoff), only
before native/user/expert/MCP/API stages. Committed fixture rows are verified,
never reinserted or reanchored; unknown state stops. Failed partial state is
backed up before cleanup. Cleanup closes owned services and connections,
refuses foreign clients, and preserves primary and cleanup errors separately.

The run addendum contains the exact same registered task IDs and a hashed
execution order: preserve/skip the completed61 TRAIN units, recover TRAIN62,
then VALID in its original order, then remaining TRAIN in its original order.
Original200 TRAIN/40 VALID are maxima. Existing failed complete units are not
relabeled or repeated. Original attempts, freezes and cumulative fees remain.

The original assembler determines the first acceptable prefix: >=100 eligible
TRAIN episodes, >=1000 TRAIN decisions, >=200 CALL, >=200 hard NO_CALL,
>=20 eligible SFT-VALID episodes, and unchanged per-step evidence, schema,
confirmation, provenance, token, context, deduplication and split checks.
Episode count alone does not authorize training. Stop new acquisition at the
first prefix that passes, then execute the original two-step sanity, fresh
one-epoch LoRA and fixed30 reevaluation. No DEV/B0/Oracle supervision, extra
epoch, prompt optimization, B0 rerun or holdout/legacy execution is allowed.

The USD10 cumulative limit and at leastUSD2 reevaluation reserve remain.
Runtime records include independent DATA recovery rules, old/new attempt
links, actual capacity bounds, code/input hashes and per-prefix quality checks.
Data, database backups, trajectories and weights remain outside Git.
