# ADR-0002 — Always-on collaboration authority and event/Run separation

- Status: **PROPOSED (feature branch)**; no architectural freeze or production claim
- Date: 2026-11-16
- References: [Anthropic, Building always-on agents (2026-11-13)](https://claude.com/resources/articles/building-always-on-agents);
  Mind Runtime `MR_ARCHITECTURE_LOCK_v1_1.md`; MR-Mem `ADR-0001`.
- Applies to: Agent-WorkBench, the existing Agent Host, Mind Runtime and MR-Mem.

## Decision

A long-lived *business obligation* must survive the absence of an Agent Run.
A resumable *tool call* must survive the absence of its executing worker.
A durable *semantic insight* must survive the current conversation.
These are three separate authorities. Do not infer that an Agent process must
always be consuming tokens.

| Concept | Authority | Durable identity | Never owns |
|---|---|---|---|
| Work items, commitments, deliverables, dependencies | WorkBench SQL | workspace + task/event IDs | Agent planning or cognition |
| Business event Inbox and business timers | WorkBench SQL | source event key + timer ID | Agent Host Run state |
| Message source / platform update | Existing transport/Host + WorkBench source pointer | provider account + update/message ID | Semantic memory |
| Run, Session, tool call, interruption, checkpoint | Existing **Agent Host** | opaque run/session/deferred refs | Business task truth |
| Cognitive WakeSignal, intent bias, decision context | **Mind Runtime** | MR runtime scope / internal intent ID | Tool safety, carrier, Host scheduling |
| Semantic memory, Thread/LCE, retrieval | **MR-Mem** | semantic memory ID / SourceRef | Operational ledger, Run or task snapshot |
| Executed external side effect | Host / tool provider | verified tool/provider receipt | MR perception or presumed business success |

### Authority constraints

1. **MR is not a generic Agent OS.** Its cognitive wake is advisory input to
   the Host. It never claims tool execution, carrier selection, credentials,
   Session/Run ownership or business workflow scheduling.
2. **WorkBench is not a new Agent host.** It records events and business
   conditions, and requests execution from a separately authenticated Host.
3. **MR-Mem is not a task database.** Its readings may lag SQL, and
   operational tool receipts alone must not form user preferences or
   experiential/relationship evidence.
4. The current SQL task state wins for factual questions about tasks. Host
   Run state wins for execution-status questions. Native evidence wins when
   evaluating what an external participant actually said.

## Human participation and responsibility

WorkBench's always-on loop includes the human as a first-class actor, but
human presence is not a prerequisite for every cycle. WorkBench owns the
accountable owner, current executor, work class, versioned delegation profile,
task-level autonomy envelope, human action queue and delegation history. The
Agent Host still owns Runs, Sessions, tool authorization and side effects. MR
may provide cognition or recommend attention; MR-Mem may provide
evidence-backed context. Neither may promote autonomy or grant permissions.
See [ADR-0003 — Progressive Delegation](ADR-0003-PROGRESSIVE-DELEGATION.md).

## Paths

```text
Telegram / monthly import / GitHub event / authorized user
         |
         v
verified source -> WorkBench durable business inbox [pending]
                      |
timer due ------------+  (schedule emits a new event; no direct model call)
                      |
                      v
scoped dispatch -> existing Agent Host [run/session is HOST-owned]
                      |                  |
                      | optional MR      +-- load authorized MR-Mem context
                      | context/wake          |
                      |                       +-- host interprets and executes
                      |
                      +-- Host result -> WorkBench reference/status
                                                |
                 +------------------------------+----------------------+
                 |                              |                      |
            no response                     outbox send             deferred call
                 |                              |                      |
           mark observed                 provider receipt     Host saves tool/session
                                                                   |
                                                              WorkBench approval
                                                                   |
                                                           authenticated decision
                                                                   |
                                                            new business event
                                                                   |
                                                            Host resumes session
```

### Clarifications

- `task.state=WAITING` is **not** a suspended Agent Run.
- `wake_events.status=deferred` is **not** an approval decision.
- `approval_requests.status=approved` is **not** a tool permission grant.
- `HostOutcome(status="completed")` is **not** proof that the task is DONE.
- An MR `WakeSignal` may motivate a Host wake, but remains cognitive input;
  the Host enforces its own scheduling, authorization and execution policy.
- Do not invent a new Worker platform in MR. WorkBench's generic business
  wake event is an integration *request*, not the Host Run lifecycle.

## Event delivery guarantees

- Every input source is normalized into `wake_events` with a stable,
  source-derived `origin + origin_event_id`; exact replays are idempotent.
- WorkBench's existing `inbox_messages` owns Telegram delivery metadata;
  `wake_events` is the normalized cross-source business wake ledger.
- A business timer due event must be written in the **same SQL transaction**
  as updating that timer's next due/status. No direct timer → Agent call.
- `WakeEventStore.claim` provides an atomic one-event lease. No two active
  business dispatch workers should claim the same event.
- When a Host call may have occurred but its response is lost, lease expiry
  becomes `unknown`; **no blind redelivery**. Reconcile through Host's
  stable event ID and recorded Run reference, or through a provider receipt,
  before deciding whether another attempt is safe.
- Host must itself implement stable event-id deduplication and durable Run
  checkpoints; this WorkBench repository cannot supply those guarantees.
- Source-of-truth completion requires task evidence and an authorized task
  state change, not a successful Host acknowledgment.

## Deferred tool call protocol

1. Agent Host attempts a restricted action; Host/tool policy decides the
   action requires approval. Host persists its own Session and deferred
   call state and returns a **non-authorizing reference**.
2. WorkBench creates an `approval_requests` business object referencing
   the event/task and **a specific approver**. Notification goes through
   `delivery_outbox`.
3. Authenticated approver's decision is persisted (immutable audit source,
   decision time, who approved, exact action/scope). Approval may expire
   or be revoked. No string matching or LLM inference constitutes approval.
4. Decision creates a new, deduped `approval_decided` event. Host receives
   it and verifies **current** tool permission, recipient, action parameters,
   approval scope, expiry and underlying Session version before continuing.
5. WorkBench stores only the Host outcome. Host alone resumes or abandons
   the saved tool call; it may ask for renewed approval.

Do not store bearer tokens or a capability-bearing deferred resume secret in
SQL, frontend, MR-Mem or logs. Opaque correlation references are allowed if
they have no standalone privilege.

## Recovery matrix

| Failure | Owner | Correct response |
|---|---|---|
| Telegram webhook delivered twice | WorkBench intake | source dedup; single wake event |
| Service dies before Host dispatch | WorkBench | pending event remains claimable |
| Service dies during Host call | WorkBench + Host | mark unknown; reconcile Host event ID; no blind replay |
| Host dies during tool call | Host | recover its Session/tool checkpoint, then settle outcome |
| Approval request unanswered | WorkBench | persist pending; optional business follow-up timer |
| Approval arrives after revocation | Host + WorkBench | reject/fail closed; never resume tool blindly |
| MR-Mem compiler unavailable | MR-Mem integration | keep SQL facts and outbox; retry projection |
| MR cognitive wake unavailable | Host | business reminders still operate; no claim MR decided |
| Outbound Telegram send ambiguous | WorkBench | delivery status unknown; verify before retry |

## Scope of this change

**Implemented in this branch:** initial `wake_events`, `approval_requests`
schema, `WakeEventStore`'s transactional dedup/claim/outcome/unknown semantics,
`AgentHostRunPort` Protocol and synthetic tests.

**NOT implemented:** authenticated transport, event-to-Host worker, robust
timer recurrence, approval policy/service, actual Host session suspension
or recovery, delivery sender, live MR connection or production deployment.

## Acceptance gates

- T1: duplicate source update, race, crash-before/after-dispatch recovery
- T2: one Host Run per stable event after Host idempotence/reconciliation
- T3: cross-scope source/approval isolation and revocation after bind
- T4: deferred tool call can only resume through verified authorized decision
- T5: task WAITING can survive after Host Run ended; task state unaffected by Run outcome
- T6: MR-Mem unavailable doesn't lose business event or falsely complete work
- T7: reminder fires by creating an event, not calling the Agent directly
- T8: external send unknown is never automatically treated as success

No delivery should be enabled before the contract tests above and real Host
capability inventory are complete.
