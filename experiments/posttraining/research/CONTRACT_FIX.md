# PT-B0-R1-CONTRACTFIX

The user explicitly authorized two payload type guards in `apps/retail_demo/outbox.py`, following a real rejected native ToolCaller invocation that produced a string error and crashed reply persistence. All other APP/core, confirmation, ownership, snapshot, idempotency, transaction and receipt deduplication behavior stays frozen.

The preparation and submission branches now check that the result payload is a dictionary before interpreting `ok`. Non-success payloads cannot establish a successful submission. Raw arguments and errors are unchanged. No operation-ID substitution, retries, historical fallback, broad exception suppression or fabricated success is added. Unsupported nested success formats continue to raise a visible contract error.

`contract_regression.py` exercises real native ToolCaller, SessionBoundMCP, isolated PostgreSQL, native store reply persistence and Outbox. Its legitimate success requires an actual native offer and customer confirmation; it is a clearly marked CPU contract fixture, never a model task, DEV rerun or training sample. The successful receipt is unique, and later user boundaries prevent old receipts from replacing ordinary answers. The targeted unit checks cover string, structured rejection, null, list and missing-field payloads.

Previous incomplete attempts and their freezes remain separate. The new baseline version starts the fixed 30-task order afresh because zero prior episodes completed. The cumulative ledger is seeded from all old usage/reservations with its hash retained; USD10 total and at least USD2 reevaluation reserve are not reset. B0, future expert collection and reevaluation use this same authorized contract patch. Infrastructure repair is not an SFT improvement.

Full validation results, authorization, exact old/new APP hashes and patch are private review artifacts. Source commits contain no credentials, model weights, datasets, databases or full trajectories. Performance results remain unavailable until the formal tasks complete.
