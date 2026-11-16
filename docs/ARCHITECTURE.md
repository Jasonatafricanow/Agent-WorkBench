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
