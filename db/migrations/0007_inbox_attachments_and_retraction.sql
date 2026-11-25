-- 0007: inbound message attachment references and source retraction markers.
ALTER TABLE inbox_messages ADD COLUMN retracted_at TEXT;
ALTER TABLE inbox_messages ADD COLUMN retracted_source_ref TEXT;

CREATE TABLE IF NOT EXISTS inbox_attachments (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  inbox_id TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('document','photo','audio','video','voice','other')),
  file_name TEXT NOT NULL DEFAULT '',
  mime_type TEXT NOT NULL DEFAULT '',
  file_size INTEGER,
  file_ref TEXT NOT NULL,
  caption TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  FOREIGN KEY(workspace_id, inbox_id) REFERENCES inbox_messages(workspace_id, id)
);
CREATE INDEX IF NOT EXISTS idx_inbox_attachments ON inbox_attachments(workspace_id, inbox_id);
