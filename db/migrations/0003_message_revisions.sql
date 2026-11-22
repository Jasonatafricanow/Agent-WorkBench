-- 0003: an edited inbound message keeps its original row; each edit is a revision.
ALTER TABLE inbox_messages ADD COLUMN revision INTEGER NOT NULL DEFAULT 1;
ALTER TABLE inbox_messages ADD COLUMN edited_at TEXT;
CREATE TABLE IF NOT EXISTS inbox_revisions (
  inbox_id TEXT NOT NULL REFERENCES inbox_messages(id),
  revision INTEGER NOT NULL,
  update_id TEXT NOT NULL,
  raw_source_ref TEXT NOT NULL,
  recorded_at TEXT NOT NULL,
  PRIMARY KEY(inbox_id, revision)
);

-- Monthly schedules remember the intended day so 31 -> 28 -> 31 instead of drifting to 28.
ALTER TABLE schedule_rules ADD COLUMN anchor_day INTEGER;
