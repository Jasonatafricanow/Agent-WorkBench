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

## Design review round (2026-11-17): gaps found against the design docs and filled

| Gap | Source in the docs | Now |
|---|---|---|
| Work memory was readable by anyone in a turn (incl. requesters) | ADR-0001/MR-Mem boundary | `/v1/memory/authorize` checks `memory_context_grants`; bootstrap grants owner + work Agent only; `grant-memory` CLI |
| No automatic due reminders | DOMAIN_CONTRACT §6 | `notices.sweep`: official due today / within N days, soft due passed — once each |
| No "chase every N days until answered" | DOMAIN_CONTRACT §6 | `set_reminder` `repeat_every_days` + `max_repeats`; a series stops when the dependency resolves |
| Human requests were never announced | ADR-0003 §5 | `notify`/`interrupt` deliver at once; `batch` goes into the daily digest |
| No daily digest | handoff §2 ("quiet", one summary) | digest at the owner's `digest_time`: routine decisions, inbox, waiting, overdue, stalled, overloaded days |
| Expired approvals / overdue requests / commitments ignored | ADR-0003, F1 | approvals expire and wake the blocked session; overdue requests and commitments notify once |
| Official due without evidence was only refused | ACCEPTANCE A07 | `propose_change` files a decision with current/proposed values |
| Agent-to-agent handoff missing | ACCEPTANCE A39 | `handoff` by the current executor (same level, Agents only) + checkpoint + `agent_handoff` wake |
| No soft-due rule | owner's workflow | owner pref `soft_due_rule` → `suggested_soft_due` with its basis, never auto-applied |
| No monthly import | handoff W6 | `imports.import_rows` / `import-csv`: new → INBOX task; due moved → decision (no overwrite); missing → review; same file twice → no-op |
| Projects not creatable | DOMAIN_CONTRACT | `create_project`, `update_project`; `project_id` validated |
| Refused access not logged | ACCEPTANCE A04 | `access_denials` (who, action, code; no content) |
| No backup / restore check | ACCEPTANCE A26 | `backup` (online, keeps 14, daily when `WORKBENCH_BACKUP_DIR` set), `verify` |
| Dashboard lacked "due this week", "upcoming follow-ups" | DOMAIN_CONTRACT §7 | added |

## Second audit (2026-11-17): design deviations, omissions, new bugs — all fixed

| # | Finding | Fix |
|---|---|---|
| A1 | A reminder could target the requester; a full-context background Run's reply was sent to them | outsider reminders need the owner's real message and must concern the outsider's own request/dependency; they are delivered as fixed text with no model Run; background replies only go to team members |
| A2 | Narrowing/lowering a profile left existing delegations on the old envelope | publishing re-narrows every active delegation of the class (or pauses it); a retired profile routes to owner approval |
| A3 | An Agent could fabricate its own promotion evidence | SUBMITTED needs a carrier-confirmed delivery; acceptance needs a real human's inbox message |
| A4 | A human request could be "answered" by the model | the answer must cite the assignee's real message |
| A5 | Requesters could trigger interrupts | requester requests are always `batch` |
| A6 | Memory: write checked against the chat person; no user binding; retries skipped revocation | reading is checked for the chat person, the wrap-up write for the handling Agent; `memory_user_id` is passed to MR-Mem; queued retries re-authorize |
| A7 | `propose_change` urgency unvalidated and never announced | shared urgency rules + announcement |
| B1 | After a handoff nobody ran as the new executor | claim returns `executor_id`; the Host runs background work as that Agent (`WORKBOT_AGENTS`) in its own lane; the guard evaluates as that Agent |
| B2 | No guarded outward tool existed | `send_external_message` (always guarded; `new_recipient` is derived by WorkBench, see E3) |
| B3 | Recurring occurrences lost delegation | an owner-approved `template.delegate` is applied to each occurrence, capped by the profile active at that time |
| B4 | No session cross-evidence | the wake event ids are appended into the Pi session (`workbench-wake` entry) |
| B5 | No "recently done" | added to overview and dashboard |
| B6 | Load view had no suggestions | concrete soft-due move suggestions (official dues never move) |
| B7 | Auto-promotion was silent | task event, version bump and an owner notice |
| C1 | Repeating chases burst after downtime | skip to the first future slot, counting missed ones |
| C2 | "Missing from the list" asked every month | asked once while pending |
| C3 | Requesters didn't see their official due move | they now see it (without internal fields) |
| C4 | A requester could be made a task's assignee | assignee limited to owner/editor |

## Third audit (Codex, 2026-11-17, after the first live CLI runs) — all fixed

| # | Finding | Fix |
|---|---|---|
| E1 | A made-up `tg://` source could close a task or move an official due | strong evidence through a Host must resolve: `explicit_user` -> a message a team member (or the task's requester) really sent; `verified_tool` -> this task's confirmed delivery, matched watch or accepted deliverable |
| E2 | An unrelated `sent` receipt plus any human's message could accept a deliverable | SUBMITTED needs a delivery of this task; outcomes need a message from the requester / commitment promisee (else the team), sent after submission |
| E3 | `approve: "false"` counted as approval; fake decision sources accepted; `new_recipient` came from the model | `approve` must be a real boolean; the decision cites the approver's real inbox message (`/approve` is recorded through intake first); WorkBench derives `new_recipient` from delivery history |
| E4 | A message whose Run died stayed `processing` forever; a reply could be lost after `completed` | Host spools inbound text until its Run finishes and replays on restart; WorkBench resumes the same wake event when its Run is dead; the reply is recorded (`reply_pending`) and sent before the Run is settled, and re-sent if it was not |
| E5 | CLI runs wrote simulated `sent` receipts | CLI deliveries settle as `simulated` -> stored `cancelled` / `cli-simulated`, never usable as a receipt |
| E7 | (live run) An outward message the owner asked for in chat was flatly refused when the task had no delegation | the owner's own request becomes an approval request (one `/approve`); for anyone else it is still refused; the reopened session keeps the requester's role |
| E8 | (live run) An answer given inside a chat Run woke a second Run that repeated the same follow-up | a response recorded by a Host source is logged as handled inline; only CLI responses wake a Run |
| E6 | MR-Mem rejected work-memory writes (no `session_id` / `fingerprint`) | sources carry the Pi session id and the sha256 of the turn's source text; 4xx rejections are reported, not queued forever |

Remaining known gaps: the Telegram gateway has not run with a real bot token; the MR-Mem
write path is fixed against the service's validation but not yet exercised live; MR-Mem
side of the memory pointer queue is not consumed; message deletions and attachments are
not handled; dashboard has no filters by project/requester yet.
