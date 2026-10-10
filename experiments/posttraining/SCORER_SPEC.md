# Deterministic scorer qwen.reward.v1

This protocol measures the tool policy with a controller-owned event transcript
and an isolated database snapshot. It does not judge conversational style and
never calls an LLM. User, system and tool observations are input-only for future
training; current work performs no training or loss construction.

The main reward is exactly 1.0 for full success, 0.4 for verified partial
progress, 0.0 for safe failure/no useful progress, or -1.0 for a critical wrong
action. Critical wrong actions override apparent success and partial progress.
Efficiency adjustment is disabled (0.0). A later version may use at most ±0.05
only inside a reward tier, with task success taking precedence. It must not alter
this frozen scoring version silently.

Per-task JSON freezes executable predicate identifiers, final target and state,
allowed catalog, seed, scripted customer followups and episode budgets. F1 and F2
require a request matching final resolved user intent, the expected amount and
inventory reservation, plus a verified prepare/display/exact-human-confirm/submit
chain. F2 additionally requires every registered clarification/revision event.
F3 requires the relevant order/operation read evidence and unchanged requests and
inventory, without a forbidden mutation proposal. An empty NO_CALL with no useful
progress receives 0.0, not automatic success. Valid reads, valid preparation or a
verified scripted clarification can earn partial progress.

Every applicable proposal contributes policy metrics before any gate result.
Unconfirmed, superseded, expired, wrong-target, changed-price, ineligible,
wrong-quantity, trusted-field or duplicate mutating attempts are critical even
when blocked. Unknown/off-catalog tools and mutation tools used for F3's
read-only goal count as wrong_tool. Wrong/missing/extra parameter fields count as
wrong_arguments. Backend rejection counts failed business tool results and
explicit backend_rejected gate evidence; native gate rejection has its own
metric. Duplicate signatures are counted for all tools, with duplicate mutations
critical. Query repetition alone is an efficiency metric.

Required metrics are task_success, wrong_tool, wrong_arguments,
backend_rejection, unauthorized_write_attempt, duplicate_tool_call, turns,
tool_calls and final_db_match. Additional native_gate_rejection and explicit
critical evidence are retained. Turns count customer message events; tool_calls
count actual gate-bound tool-result events, including failures. Malformed or
unattributable controller evidence raises ValueError and remains unscorable;
the launcher must record an infrastructure/evidence failure with reward null,
never fabricate a successful reward.

## Evidence contract

Each trace contains task_id, scoring_version, initial_fixture_hash,
evidence_source=isolated_controller, events, and final_db. Event IDs are unique,
and sequence is contiguous from 1. Sequence is emitted by the controller from
causal hooks; timestamps do not establish attribution.

- user_message: actual customer event ID and text.
- clarification_requested: controller-derived field and the actual question
  text; frozen Chinese lexical field cues must occur. This checks the tool
  policy's clarification need, not unrestricted natural-language correctness.
- user_update: registered scripted fields and user_event_id. The message must
  exactly match the frozen followup. Clarification/revision triggers must have
  their prior request/display evidence.
- snapshot_displayed: operation_id and verified immutable snapshot fields.
- confirmation_recorded: operation_id and user_event_id; actual text must equal
  the native exact confirmation phrase for the displayed current intent.
- policy_proposal: explicit policy_step_id, split=DEV, family and proposed_calls.
  Calls have tool_name, arguments and is_applicable. Canonical arguments use the
  original native converter's representation (quantity is an integer), while
  raw proposed argument strings remain in the exact policy journal. No trusted
  server field may enter a proposal. NO_CALL still has a policy step and gate.
- policy_failure: explicit policy_step_id, split and family for parse/generation
  failure. No invented gate record is required for an unparsed decision.
- gate_result: policy_step_id, status, accepted_calls with native tool_call_id,
  tool_name and exactly matching converted arguments. Each step has one gate.
- tool_result: gate-bound tool_call_id and policy_step_id plus a controller
  canonical result. Result ok/error comes from the original native tool;
  normalized successful preparations expose data.operation, submissions expose
  data.request, order queries expose data.order/items, operation queries expose
  data.operation. These are projections of native results and verified DB rows,
  not model-declared facts. Original payloads/hashes and event IDs must also be
  retained by the controller. This projection bridge is a prerequisite for the
  future launcher, not an already exercised PG integration in this freeze.

The scorer checks exact policy→gate call→result bindings and rejects crossed or
duplicate IDs. Future preparation parents use the existing logger's explicit
parent IDs. All steps of one episode remain DEV in the same family.

## Fixture and database contract

initial_db_fixture is a logical isolated-DB fixture, not SQL for production.
Only the controller receives trusted_context and trusted_history. Do not send
these whole structures to Qwen. Relative timestamp objects are materialized
against one isolated DB server anchor; exported snapshots normalize them back
against that anchor. The future loader must use existing migrations and trusted
fixture setup, validate foreign keys and confirmation bindings, and avoid double
reservation when seeding prior requests (seed reservation subtracts quantities
that request insertion triggers will add). It must not disable production guards
or change APP/core source to make a task pass.

The controller exports a stable projection: customer/order/item rows use fixture
columns; preserved request rows use their frozen columns (volatile created_at
excluded); newly created IDs are unconstrained but request fields and operation
foreign keys must match. final_db includes all declared tables. The scorer checks
customer/order facts, target and non-target item reservations, preserved requests,
request count/amount/intent, and request-operation binding. It does not claim a
notification delivery or refund outcome; those are separate future holdout goals.

The scorer trusts the isolated controller, never the policy, to own the audit and
DB snapshot. Fabricating an evidence_source string does not authenticate a file;
the launcher must control artifact ownership and bind native evidence hashes.
The CPU oracle traces in tests verify this scoring contract and are not baseline
rollouts or training data.
