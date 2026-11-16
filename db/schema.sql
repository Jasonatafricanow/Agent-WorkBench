-- Agent WorkBench — schema baseline; SQLite 3.38+, foreign_keys=ON, WAL in service.
-- All stored instants are canonical UTC YYYY-MM-DDTHH:MM:SSZ. No secrets/raw chats.
CREATE TABLE IF NOT EXISTS principals (
  id TEXT PRIMARY KEY,
  display_name TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('human','agent','service')),
  active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workspaces (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  owner_id TEXT NOT NULL REFERENCES principals(id),
  timezone TEXT NOT NULL DEFAULT 'UTC',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memberships (
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  principal_id TEXT NOT NULL REFERENCES principals(id),
  role TEXT NOT NULL CHECK (role IN ('owner','editor','requester','viewer','scheduler')),
  active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
  PRIMARY KEY(workspace_id, principal_id)
);
-- User-side host configuration; semantic memory contents are NOT copied here.
CREATE TABLE IF NOT EXISTS user_profiles (
  workspace_id TEXT NOT NULL,
  principal_id TEXT NOT NULL,
  locale TEXT NOT NULL DEFAULT 'zh-CN',
  timezone TEXT,
  work_preferences_json TEXT NOT NULL DEFAULT '{}',
  version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
  updated_at TEXT NOT NULL,
  PRIMARY KEY(workspace_id,principal_id),
  FOREIGN KEY(workspace_id,principal_id) REFERENCES memberships(workspace_id,principal_id)
);
-- Explicit host ACL for a WorkBench principal to read/write an MR USER scope.
-- Never derive access from shared agent runtime identity or workspace membership alone.
CREATE TABLE IF NOT EXISTS memory_context_grants (
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  actor_principal_id TEXT NOT NULL REFERENCES principals(id),
  memory_user_id TEXT NOT NULL,
  capability TEXT NOT NULL CHECK(capability IN ('memory.read','memory.write')),
  granted_by_principal_id TEXT NOT NULL REFERENCES principals(id),
  grant_source_ref TEXT NOT NULL,
  created_at TEXT NOT NULL,
  revoked_at TEXT,
  PRIMARY KEY(workspace_id,actor_principal_id,memory_user_id,capability),
  FOREIGN KEY(workspace_id,actor_principal_id)
    REFERENCES memberships(workspace_id,principal_id)
);

CREATE TABLE IF NOT EXISTS external_identities (
  provider TEXT NOT NULL,
  bot_id TEXT NOT NULL,
  external_user_id TEXT NOT NULL,
  principal_id TEXT NOT NULL REFERENCES principals(id),
  PRIMARY KEY(provider,bot_id,external_user_id)
);
CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  title TEXT NOT NULL,
  goal TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL DEFAULT 'active'
    CHECK (state IN ('active','paused','done','cancelled')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(workspace_id,id)
);
CREATE INDEX IF NOT EXISTS idx_projects_ws ON projects(workspace_id,state);
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  project_id TEXT,
  requester_id TEXT REFERENCES principals(id),
  assignee_id TEXT REFERENCES principals(id),
  title TEXT NOT NULL,
  goal TEXT NOT NULL DEFAULT '',
  next_action TEXT NOT NULL DEFAULT '',
  done_summary TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL DEFAULT 'INBOX'
    CHECK (state IN ('INBOX','READY','DOING','WAITING','BLOCKED','DONE','CANCELLED')),
  priority INTEGER NOT NULL DEFAULT 2 CHECK(priority BETWEEN 0 AND 4),
  official_due_at TEXT,
  soft_due_at TEXT,
  planned_start_at TEXT,
  completed_at TEXT,
  version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(workspace_id,project_id) REFERENCES projects(workspace_id,id),
  UNIQUE(workspace_id,id)
);
CREATE INDEX IF NOT EXISTS idx_tasks_queue ON tasks(workspace_id,state,priority,updated_at);
CREATE INDEX IF NOT EXISTS idx_tasks_due ON tasks(workspace_id,official_due_at,soft_due_at);
CREATE INDEX IF NOT EXISTS idx_tasks_requester ON tasks(workspace_id,requester_id);
CREATE TABLE IF NOT EXISTS task_events (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  task_id TEXT NOT NULL,
  actor_id TEXT NOT NULL REFERENCES principals(id),
  kind TEXT NOT NULL,
  before_json TEXT NOT NULL DEFAULT '{}',
  after_json TEXT NOT NULL DEFAULT '{}',
  reason TEXT NOT NULL DEFAULT '',
  evidence_type TEXT NOT NULL CHECK (evidence_type IN (
    'explicit_user','verified_tool','reported_third_party','inferred','system')),
  source_ref TEXT,
  occurred_at TEXT NOT NULL,
  recorded_at TEXT NOT NULL,
  FOREIGN KEY(workspace_id,task_id) REFERENCES tasks(workspace_id,id)
);
CREATE INDEX IF NOT EXISTS idx_task_events ON task_events(workspace_id,task_id,recorded_at);
CREATE TABLE IF NOT EXISTS task_focus (
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  slot TEXT NOT NULL CHECK (slot IN ('NOW','NEXT1','NEXT2')),
  task_id TEXT,
  actor_id TEXT NOT NULL REFERENCES principals(id),
  updated_at TEXT NOT NULL,
  PRIMARY KEY(workspace_id,slot),
  UNIQUE(workspace_id,task_id),
  FOREIGN KEY(workspace_id,task_id) REFERENCES tasks(workspace_id,id)
);
CREATE TABLE IF NOT EXISTS dependencies (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  task_id TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN (
    'external_response','prerequisite_task','artifact','permission','other')),
  awaited_outcome TEXT NOT NULL,
  responsible_principal_id TEXT REFERENCES principals(id),
  related_task_id TEXT,
  status TEXT NOT NULL DEFAULT 'open'
    CHECK (status IN ('open','resolved','cancelled')),
  resolved_source_ref TEXT,
  created_at TEXT NOT NULL,
  resolved_at TEXT,
  FOREIGN KEY(workspace_id,task_id) REFERENCES tasks(workspace_id,id),
  FOREIGN KEY(workspace_id,related_task_id) REFERENCES tasks(workspace_id,id)
);
CREATE TABLE IF NOT EXISTS reminders (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  task_id TEXT NOT NULL,
  dependency_id TEXT REFERENCES dependencies(id),
  due_at TEXT NOT NULL,
  timezone TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('followup','official_due','soft_due','checkin')),
  condition TEXT NOT NULL DEFAULT 'if_open'
    CHECK (condition IN ('if_open','if_still_waiting','always')),
  recipient_principal_id TEXT NOT NULL REFERENCES principals(id),
  channel TEXT NOT NULL DEFAULT 'telegram',
  status TEXT NOT NULL DEFAULT 'scheduled'
    CHECK (status IN ('scheduled','leased','sent','unknown','failed','cancelled')),
  attempts INTEGER NOT NULL DEFAULT 0,
  lease_until TEXT,
  dedupe_key TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(workspace_id,task_id) REFERENCES tasks(workspace_id,id)
);
CREATE INDEX IF NOT EXISTS idx_due_reminders ON reminders(status,due_at,lease_until);
CREATE TABLE IF NOT EXISTS inbox_messages (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  platform TEXT NOT NULL,
  bot_id TEXT NOT NULL,
  update_id TEXT NOT NULL,
  chat_id TEXT NOT NULL,
  message_id TEXT NOT NULL,
  sender_principal_id TEXT REFERENCES principals(id),
  raw_source_ref TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'received'
    CHECK (status IN ('received','processing','done','failed','rejected')),
  received_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(platform,bot_id,update_id),
  UNIQUE(platform,bot_id,chat_id,message_id)
);
CREATE TABLE IF NOT EXISTS message_links (
  inbox_id TEXT NOT NULL REFERENCES inbox_messages(id),
  task_id TEXT NOT NULL REFERENCES tasks(id),
  relation TEXT NOT NULL CHECK(relation IN ('request','context','feedback','delivery')),
  PRIMARY KEY(inbox_id,task_id,relation)
);
CREATE TABLE IF NOT EXISTS command_receipts (
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  idempotency_key TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  result_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(workspace_id,idempotency_key)
);
CREATE TABLE IF NOT EXISTS memory_outbox (
  id TEXT PRIMARY KEY,
  task_event_id TEXT NOT NULL UNIQUE REFERENCES task_events(id),
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  scope TEXT NOT NULL,
  source_ref TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','leased','sent','failed','dead')),
  attempts INTEGER NOT NULL DEFAULT 0,
  lease_until TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS delivery_outbox (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  task_id TEXT REFERENCES tasks(id),
  reminder_id TEXT REFERENCES reminders(id),
  recipient_principal_id TEXT NOT NULL REFERENCES principals(id),
  channel TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','leased','sent','failed','unknown','cancelled')),
  provider_message_id TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  lease_until TEXT,
  dedupe_key TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_inbox_pending ON inbox_messages(workspace_id,status,received_at);
