# Agent WorkBench — module boundaries

The authoritative current state and event log live in WorkBench SQL, not MR-Mem.
The host owns Telegram raw messages, delivery receipts, and their revisions;
MR-Mem receives only accepted, scope-bound semantic projections via SourceRef.

| Module | Concern | Persistent authority |
|---|---|---|
| identity | External identity mapping, memberships, scoped actor | SQL |
| domain | Projects, tasks, state machine, delay/decision history | SQL |
| agent-tools | Fixed verified actor, narrow command surface | None |
| intake | Telegram authenticated update, dedupe, source link | Host/SQL |
| scheduler | Reminders, follow-up, leases, receipts | SQL |
| memory-bridge | Event-to-SourceRef async outbox | MR-Mem for derived semantics only |
| dashboard | Read-only projections and task filters | None |

The MR-Mem integration counterpart is
`MR-Mem:feat/workbench-user-context-20261116`; it validates allowed
USER scope and current task-event SourceRef without adding a business DB.

Current branch is **integration preparation**, not an authenticated deployment.
A production service must not expose the pure Python WorkbenchTools constructor
or accept actor context from a request body. Authenticate Telegram Bot API
updates and bind identity using server-side membership. Confirm the active
workspace and permission for each operation. Distinguish current task state
(authoritative SQL) from narrative history (MR-Mem).

Priority of work: SQL event ledger and ACL; native agent tool service; Telegram
intake; MR-Mem host binding; persistent scheduler/outbox; read-only dashboard;
container deployment and recovery. End-to-end tests gate live rollout.
\n## Always-on ownership (2026-11-16)\n\nSee [ADR-0002](ADR-0002-ALWAYS-ON-OWNERSHIP.md) and [HOST_RUN_PROTOCOL.md](HOST_RUN_PROTOCOL.md). WorkBench owns durable **business** inbox/timers/outbox; **the existing Agent Host** owns Run, Session and deferred-tool execution. **Mind Runtime** owns cognition and cognitive wake semantics only; MR-Mem owns semantic memory. A task waiting on a person does not imply the Agent Host has a suspended tool call. No duplicate Agent Loop will be implemented here.\n\nThe new internal `runtime_bridge.WakeEventStore` persists normalized wake events with deduplication and lease/unknown handling. Its Host port is a Protocol, **not** a working Host connection.\n