# Framework status (2026-11-17)

All modules of the plan now exist as running code with offline tests. "Built" means
code + tests on a real SQLite file; it does not mean deployed or audited. The owner
asked for the full framework first; a detailed audit and fixes come next.

| Plan item | Module | Status |
|---|---|---|
| WB-01 TaskService, ACL, idempotency, versions | `task_service.py` | built, tested |
| WB-02 migrations, date granularity, backup | `storage.migrate`, `db/migrations/0002_*` | built, tested (bootstrap DB → v2 with backup) |
| WB-03 commitments / deliverables / decision inbox | ops `record_commitment`, `update_commitment`, `register_deliverable`, `update_deliverable`; `human_action_requests` | built, tested |
| WB-04 work checkpoints, changes since last view | ops `save_checkpoint`; `changes_since` + `view_watermarks` | built, tested |
| WB-05 timers → wake events, recurring schedules | `scheduler.fire_due` (+ timer thread in `serve`) | built, tested |
| WB-06 dispatch, one Run per workspace, unknown reconcile | `dispatcher.py`; Host pulls via `/v1/wake/claim` | built, tested |
| WB-07 approvals bound to one exact tool call | `approvals.py`; `agent/guard-extension.ts` | built; hook exercised end to end against a live server |
| WB-08 delivery outbox, quiet hours, unknown | `outboxes.DeliveryOutbox` | built, tested |
| WB-09 identity + inbound intake | `identity.py`; gateway | built, tested (gateway not run with a real bot) |
| WB-10 memory | Agent writes MR-Mem at wrap-up (`work_memory_write`); `MemoryPointerQueue` for retry | built; MR-Mem side not connected |
| WB-11 dashboard, external watch, recurrence | `dashboard.py` (read-only, Basic auth); `observe_watch`; schedules | built, tested |
| WB-12 stagnation / schedule load | `insights.py` | built |
| Delegation (ADR-0003) | `assign_responsibility`, `set_responsibility_status`, `evaluate_action`, `DelegationProfileAdmin` | built, tested |
| Host (Pi work bot) | `host/pi-workbot/` | written; not deployed |
| Deploy | `deploy/Dockerfile`, `deploy/compose.example.yaml` | example only; nothing deployed |

Known gaps to audit: no automatic promotion from evidence (only owner-approved level
changes); GitHub/CI watch adapters not written (only the `observe_watch` endpoint);
Telegram edits/deletions not tracked as revisions; dashboard has no history view;
`memory_outbox` consumer to MR-Mem lives in the Host and is not written; the work bot
has no message batching window yet.
