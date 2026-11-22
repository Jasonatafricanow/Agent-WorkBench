-- 0005: approvals the owner gave in another system's flow (e.g. a Facetide review passphrase in Xiyue).
-- Audit only: the record says who approved what payload, citing the owner's own message; it grants nothing.
CREATE TABLE IF NOT EXISTS external_approvals (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  system TEXT NOT NULL,
  action TEXT NOT NULL,
  subject_ref TEXT NOT NULL,
  payload_digest TEXT NOT NULL,
  approver_id TEXT NOT NULL REFERENCES principals(id),
  recorded_by TEXT NOT NULL,
  source_ref TEXT NOT NULL,
  issued_at TEXT,
  decided_at TEXT NOT NULL,
  recorded_at TEXT NOT NULL,
  UNIQUE(workspace_id, system, source_ref)
);
CREATE INDEX IF NOT EXISTS idx_external_approvals ON external_approvals(workspace_id, decided_at);
