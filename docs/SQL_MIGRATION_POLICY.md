# SQL authority and migrations

Current `db/schema.sql` is an initial bootstrap schema, not a production
migration history. Before persistent deployment add numbered, immutable
migrations, a schema version table, backup validation and a reversible rollout
process for each destructive migration. `CREATE TABLE IF NOT EXISTS` does
not upgrade a deployed table.

Every mutation must atomically write current task state + append-only event +
idempotency receipt + outbox record. Multiple agents may attempt updates:
compare `tasks.version`, reject stale writers, and re-read rather than
silently applying a second change.

Outbox jobs must be leased with expiry and claimed transactionally.
Telegram has no sendMessage client idempotency parameter. An ambiguous send
must be marked `unknown` and reconciled; it must not be blindly repeated.

Task deadlines, plan dates and follow-up times are distinct. Due instants
are stored as UTC; the originating IANA timezone and original local date
must be preserved where a date-only deadline is entered.

Treat SQLite as a single-writer service with its WAL, database and backup
all confined to one trusted filesystem. Do not run two WorkBench writer
containers against a shared NFS/SMB database file.


## Workspace-level referential integrity hardening (2026-11-16)

The initial schema now uses composite `(workspace_id,id)` foreign keys for
`message_links`, `reminders.dependency_id`, `memory_outbox.task_event_id`,
and `delivery_outbox`'s task/reminder links. The normalized `wake_events`
and `approval_requests` already use composite workspace keys. This is
defense in depth: every service operation **still** checks authenticated
principal/role and object visibility before touching a row.

This remains an **initial bootstrap schema**, not a migration for an existing
persistent database. Before deploying into a nonempty DB, ship explicit,
transaction-safe migration versions and backfill `message_links.workspace_id`
from authenticated native source associations; reject inconsistent legacy
links rather than inventing their workspace. Do not rely on
`CREATE TABLE IF NOT EXISTS` to apply new foreign keys.
