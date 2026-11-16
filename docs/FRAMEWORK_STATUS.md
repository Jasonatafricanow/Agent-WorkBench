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
| WB-06 dispatch, one background Run per workspace, outcome reconcile | `dispatcher.py`; Host pulls via `/v1/wake/claim`; gateway ledger re-settles after restart; `/v1/wake/unknown` + dashboard list | built, tested (ledger path not run live) |
| WB-07 approvals bound to one exact tool call | `approvals.py`; `agent/guard-extension.ts` | built; hook exercised end to end against a live server |
| WB-08 delivery outbox, quiet hours, unknown | `outboxes.DeliveryOutbox` | built, tested |
| WB-09 identity + inbound intake | `identity.py`; gateway | built, tested (gateway not run with a real bot) |
| WB-10 memory | Agent writes MR-Mem at wrap-up (`work_memory_write`); `MemoryPointerQueue` for retry | built; MR-Mem side not connected |
| WB-11 dashboard, external watch, recurrence | `dashboard.py` (read-only, Basic auth); `observe_watch`; schedules | built, tested |
| WB-12 stagnation / schedule load | `insights.py` | built |
| Delegation (ADR-0003) | `assign_responsibility`, `set_responsibility_status`, `evaluate_action`, `DelegationProfileAdmin` | built, tested |
| Host (Pi work bot) | `host/pi-workbot/` | written; not deployed |
| Deploy | `deploy/Dockerfile`, `deploy/compose.example.yaml` | example only; nothing deployed |

## Added after the first pass (2026-11-17)

- Evidence-based delegation review (`promotion.py`, daily): review request to the owner, or a
  one-step auto-promotion when the owner pre-enabled it in the profile.
- GitHub watch adapter (`watchers.py`: `github_ci`, `github_pr`, every 5 min).
- Edited Telegram messages become revisions (migration 0003); linked tasks get `source_edited`.
- Work bot: per-person sessions, merge window, auto-link source messages, memory write retry.
- Dashboard task page with history; dashboard no longer writes anything.

## Audit fixes (2026-11-17, 13 findings, all reproduced or read end to end)

1. Failed Runs are settled `failed` (inline and background); restart settles dangling runs; unknown outcomes are listed.
2. Only follow-ups go stale; "task done" reports still reach the person.
3. `evaluate_action` gives a delegated route only to the current executor; the guard evaluates the task named in the call.
4. Approvals are consumed only by the same session and task that asked.
5. Requesting an approval notifies the approver; pending approvals are on the overview/dashboard.
6. A fired reminder is delivered to its own recipient.
7. Monthly schedules keep their anchor day (31 → 28 → 31).
8. A requester's "what's new" shows only their own events and public state/official-due changes, without actor or evidence.
9. Empty `source_ref` is rejected; a malformed wake row is marked failed instead of blocking the inbox.
10. SQLite errors map to 409/503 instead of dropping the connection; `project_id` is validated.
11. After downtime only the latest missed occurrence becomes a task.
12. Group chats: each person has their own model session.
13. Owner-only changes (focus, delegation) made through Telegram must cite an inbox message the owner actually sent.

Remaining known gaps: the Telegram gateway has not run with a real bot token; MR-Mem
side of the memory pointer queue is not consumed; message deletions are not tracked.
