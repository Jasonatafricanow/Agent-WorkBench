-- 0002: date granularity, commitments, deliverables, work checkpoints, view
-- watermarks, recurring schedules, external watches, approval fingerprints.

-- Official due keeps what the source said: a local date (precision='date') or a
-- local date-time, plus its IANA timezone. official_due_at stays the UTC instant
-- used for sorting/overdue (end of the local day for date-only deadlines).
ALTER TABLE tasks ADD COLUMN official_due_local TEXT;
ALTER TABLE tasks ADD COLUMN official_due_timezone TEXT;
ALTER TABLE tasks ADD COLUMN official_due_precision TEXT
  CHECK (official_due_precision IS NULL OR official_due_precision IN ('date','datetime'));

CREATE TABLE IF NOT EXISTS commitments (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  task_id TEXT,
  promisor_id TEXT NOT NULL REFERENCES principals(id),
  promisee_id TEXT REFERENCES principals(id),
  promise_text TEXT NOT NULL,
  due_at TEXT,
  due_local TEXT,
  due_timezone TEXT,
  status TEXT NOT NULL DEFAULT 'open'
    CHECK (status IN ('open','kept','broken','rescheduled','withdrawn')),
  evidence_type TEXT NOT NULL CHECK (evidence_type IN (
    'explicit_user','verified_tool','reported_third_party','inferred','system')),
  source_ref TEXT,
  version INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(workspace_id,id),
  FOREIGN KEY(workspace_id,task_id) REFERENCES tasks(workspace_id,id)
);
CREATE INDEX IF NOT EXISTS idx_commitments_open ON commitments(workspace_id,status,due_at);

CREATE TABLE IF NOT EXISTS deliverables (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  task_id TEXT NOT NULL,
  title TEXT NOT NULL,
  artifact_ref TEXT NOT NULL,
  revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1),
  status TEXT NOT NULL DEFAULT 'DRAFT' CHECK (status IN (
    'DRAFT','SUBMITTED','ACKNOWLEDGED','REVIEW_REQUIRED','ACCEPTED','REJECTED')),
  submit_receipt_ref TEXT,
  acceptance_source_ref TEXT,
  submitted_at TEXT,
  acknowledged_at TEXT,
  accepted_at TEXT,
  version INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(workspace_id,id),
  FOREIGN KEY(workspace_id,task_id) REFERENCES tasks(workspace_id,id)
);

-- Human/agent-readable resume point; never a Host session or tool state.
CREATE TABLE IF NOT EXISTS work_checkpoints (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  task_id TEXT NOT NULL,
  task_version INTEGER NOT NULL,
  author_id TEXT NOT NULL REFERENCES principals(id),
  verified_at TEXT NOT NULL,
  repo_ref TEXT,
  artifact_refs_json TEXT NOT NULL DEFAULT '[]'
    CHECK(json_valid(artifact_refs_json) AND json_type(artifact_refs_json)='array'),
  completed_steps_json TEXT NOT NULL DEFAULT '[]'
    CHECK(json_valid(completed_steps_json) AND json_type(completed_steps_json)='array'),
  unverified_json TEXT NOT NULL DEFAULT '[]'
    CHECK(json_valid(unverified_json) AND json_type(unverified_json)='array'),
  next_action TEXT NOT NULL DEFAULT '',
  blockers_json TEXT NOT NULL DEFAULT '[]'
    CHECK(json_valid(blockers_json) AND json_type(blockers_json)='array'),
  source_ref TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(workspace_id,id),
  FOREIGN KEY(workspace_id,task_id) REFERENCES tasks(workspace_id,id)
);
CREATE INDEX IF NOT EXISTS idx_checkpoints_task ON work_checkpoints(workspace_id,task_id,created_at);

-- "Since I last looked": per principal and view (e.g. telegram, dashboard).
CREATE TABLE IF NOT EXISTS view_watermarks (
  workspace_id TEXT NOT NULL,
  principal_id TEXT NOT NULL,
  view TEXT NOT NULL,
  last_seen_seq INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(workspace_id,principal_id,view),
  FOREIGN KEY(workspace_id,principal_id) REFERENCES memberships(workspace_id,principal_id)
);

-- Recurring work: each occurrence creates one task (deduped) and one wake event.
CREATE TABLE IF NOT EXISTS schedule_rules (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  title TEXT NOT NULL,
  template_json TEXT NOT NULL DEFAULT '{}'
    CHECK(json_valid(template_json) AND json_type(template_json)='object'),
  timezone TEXT NOT NULL,
  frequency TEXT NOT NULL CHECK (frequency IN ('daily','weekly','monthly')),
  interval_n INTEGER NOT NULL DEFAULT 1 CHECK(interval_n >= 1),
  local_time TEXT NOT NULL,
  next_fire_local TEXT NOT NULL,
  next_fire_at TEXT NOT NULL,
  occurrences INTEGER NOT NULL DEFAULT 0,
  max_occurrences INTEGER,
  until_at TEXT,
  active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
  created_by_principal_id TEXT NOT NULL REFERENCES principals(id),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(workspace_id,id)
);
CREATE INDEX IF NOT EXISTS idx_schedule_due ON schedule_rules(active,next_fire_at);
CREATE TABLE IF NOT EXISTS schedule_occurrences (
  rule_id TEXT NOT NULL REFERENCES schedule_rules(id),
  occurrence_local TEXT NOT NULL,
  task_id TEXT NOT NULL,
  wake_event_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(rule_id,occurrence_local)
);

-- External change monitoring: an adapter reports observed values; a match on
-- the condition becomes one external_change wake event (and may resolve a dependency).
CREATE TABLE IF NOT EXISTS external_watches (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  task_id TEXT,
  dependency_id TEXT,
  source_kind TEXT NOT NULL,
  target_ref TEXT NOT NULL,
  expected_value TEXT NOT NULL,
  last_observed_value TEXT,
  status TEXT NOT NULL DEFAULT 'watching' CHECK (status IN ('watching','matched','cancelled')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(workspace_id,id),
  FOREIGN KEY(workspace_id,task_id) REFERENCES tasks(workspace_id,id),
  FOREIGN KEY(workspace_id,dependency_id) REFERENCES dependencies(workspace_id,id)
);

-- Approval binds one exact Host tool call (name + input fingerprint) and expires.
ALTER TABLE approval_requests ADD COLUMN tool_name TEXT NOT NULL DEFAULT '';
ALTER TABLE approval_requests ADD COLUMN action_fingerprint TEXT NOT NULL DEFAULT '';
ALTER TABLE approval_requests ADD COLUMN expires_at TEXT;
ALTER TABLE approval_requests ADD COLUMN consumed_at TEXT;

-- Owner-facing delivery preferences (quiet hours etc.) live in user_profiles;
-- notifications record why they were sent.
ALTER TABLE delivery_outbox ADD COLUMN not_before TEXT;
