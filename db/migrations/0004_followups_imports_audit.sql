-- 0004: repeating follow-ups, monthly imports, access-denial audit, notification bookkeeping.

-- "Chase every Wednesday until they answer, at most N times".
ALTER TABLE reminders ADD COLUMN repeat_every_days INTEGER;
ALTER TABLE reminders ADD COLUMN repeats_left INTEGER;
ALTER TABLE reminders ADD COLUMN series_id TEXT;

-- Monthly business data (e.g. the official-due list exported from an internal system).
CREATE TABLE IF NOT EXISTS import_batches (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  source_name TEXT NOT NULL,
  period TEXT NOT NULL,
  checksum TEXT NOT NULL,
  imported_by TEXT NOT NULL REFERENCES principals(id),
  summary_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  UNIQUE(workspace_id, source_name, checksum)
);
CREATE TABLE IF NOT EXISTS import_keys (
  workspace_id TEXT NOT NULL,
  source_name TEXT NOT NULL,
  external_key TEXT NOT NULL,
  task_id TEXT NOT NULL,
  last_batch_id TEXT NOT NULL REFERENCES import_batches(id),
  last_row_json TEXT NOT NULL,
  PRIMARY KEY(workspace_id, source_name, external_key),
  FOREIGN KEY(workspace_id, task_id) REFERENCES tasks(workspace_id, id)
);

-- A04: refused access is recorded as a summary (no content), outside the failed transaction.
CREATE TABLE IF NOT EXISTS access_denials (
  workspace_id TEXT NOT NULL,
  principal_id TEXT NOT NULL,
  action TEXT NOT NULL,
  code TEXT NOT NULL,
  at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_access_denials ON access_denials(workspace_id, at);

-- One notice per (subject, kind, local day): due-today, due-soon, overdue, digest, expiry.
CREATE TABLE IF NOT EXISTS notices_sent (
  workspace_id TEXT NOT NULL,
  dedupe_key TEXT NOT NULL,
  at TEXT NOT NULL,
  PRIMARY KEY(workspace_id, dedupe_key)
);
