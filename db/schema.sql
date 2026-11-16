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
  FOREIGN KEY(workspace_id,task_id) REFERENCES tasks(workspace_id,id),
  UNIQUE(workspace_id,id)
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
  FOREIGN KEY(workspace_id,related_task_id) REFERENCES tasks(workspace_id,id),
  UNIQUE(workspace_id,id)
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
  FOREIGN KEY(workspace_id,task_id) REFERENCES tasks(workspace_id,id),
  FOREIGN KEY(workspace_id,dependency_id) REFERENCES dependencies(workspace_id,id),
  UNIQUE(workspace_id,id)
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
  UNIQUE(platform,bot_id,chat_id,message_id),
  UNIQUE(workspace_id,id)
);
CREATE TABLE IF NOT EXISTS message_links (
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  inbox_id TEXT NOT NULL,
  task_id TEXT NOT NULL,
  relation TEXT NOT NULL CHECK(relation IN ('request','context','feedback','delivery')),
  PRIMARY KEY(workspace_id,inbox_id,task_id,relation),
  FOREIGN KEY(workspace_id,inbox_id) REFERENCES inbox_messages(workspace_id,id),
  FOREIGN KEY(workspace_id,task_id) REFERENCES tasks(workspace_id,id)
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
  task_event_id TEXT NOT NULL UNIQUE,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  scope TEXT NOT NULL,
  source_ref TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','leased','sent','failed','dead')),
  attempts INTEGER NOT NULL DEFAULT 0,
  lease_until TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(workspace_id,task_event_id) REFERENCES task_events(workspace_id,id)
);
CREATE TABLE IF NOT EXISTS delivery_outbox (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  task_id TEXT,
  reminder_id TEXT,
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
  updated_at TEXT NOT NULL,
  FOREIGN KEY(workspace_id,task_id) REFERENCES tasks(workspace_id,id),
  FOREIGN KEY(workspace_id,reminder_id) REFERENCES reminders(workspace_id,id)
);
CREATE INDEX IF NOT EXISTS idx_inbox_pending ON inbox_messages(workspace_id,status,received_at);

-- Business wake-event inbox. Agent Host owns the Run/Session referenced here.
-- A source-specific id permits idempotent ingestion; an expired lease becomes
-- unknown and requires Host reconciliation, not blind replay of side effects.
CREATE TABLE IF NOT EXISTS wake_events (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  task_id TEXT,
  origin TEXT NOT NULL,
  origin_event_id TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN (
    'user_message','timer_fired','external_change',
    'approval_decided','agent_handoff','human_action_completed',
    'delegation_review_due','responsibility_changed')),
  source_ref TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN (
    'pending','leased','completed','deferred','failed','unknown')),
  attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
  lease_until TEXT,
  host_run_ref TEXT,
  host_session_ref TEXT,
  deferred_ref TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(workspace_id,origin,origin_event_id),
  UNIQUE(workspace_id,id),
  FOREIGN KEY(workspace_id,task_id) REFERENCES tasks(workspace_id,id)
);
CREATE INDEX IF NOT EXISTS idx_wake_claim
  ON wake_events(workspace_id,status,created_at);

-- WorkBench stores approval decisions, never tool-call state. The Agent Host
-- persists the actual suspended call and rechecks tool permission on resume.
CREATE TABLE IF NOT EXISTS approval_requests (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  wake_event_id TEXT NOT NULL,
  task_id TEXT,
  requested_by TEXT NOT NULL REFERENCES principals(id),
  approver_id TEXT NOT NULL REFERENCES principals(id),
  request_summary TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN (
    'pending','approved','denied','expired','cancelled')),
  host_deferred_ref TEXT NOT NULL,
  decision_source_ref TEXT,
  created_at TEXT NOT NULL,
  decided_at TEXT,
  UNIQUE(workspace_id,id),
  FOREIGN KEY(workspace_id,wake_event_id) REFERENCES wake_events(workspace_id,id),
  FOREIGN KEY(workspace_id,task_id) REFERENCES tasks(workspace_id,id)
);


-- Progressive Delegation: human participation and per-work-class autonomy.
-- Profiles are versioned and owner-approved; a task assignment can only narrow them.
CREATE TABLE IF NOT EXISTS delegation_profiles (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  work_class TEXT NOT NULL,
  version INTEGER NOT NULL CHECK(version >= 1),
  max_autonomy_level TEXT NOT NULL CHECK(max_autonomy_level IN (
    'human_executes','agent_prepares','agent_supervised','agent_bounded','agent_exception_only')),
  allowed_actions_json TEXT NOT NULL DEFAULT '[]'
    CHECK(json_valid(allowed_actions_json) AND json_type(allowed_actions_json)='array'),
  reversible_actions_json TEXT NOT NULL DEFAULT '[]'
    CHECK(json_valid(reversible_actions_json) AND json_type(reversible_actions_json)='array'),
  mandatory_approval_conditions_json TEXT NOT NULL DEFAULT '[]'
    CHECK(json_valid(mandatory_approval_conditions_json) AND json_type(mandatory_approval_conditions_json)='array'),
  success_criteria_json TEXT NOT NULL DEFAULT '[]'
    CHECK(json_valid(success_criteria_json) AND json_type(success_criteria_json)='array'),
  evidence_requirements_json TEXT NOT NULL DEFAULT '[]'
    CHECK(json_valid(evidence_requirements_json) AND json_type(evidence_requirements_json)='array'),
  escalation_policy_json TEXT NOT NULL DEFAULT '{}'
    CHECK(json_valid(escalation_policy_json) AND json_type(escalation_policy_json)='object'),
  promotion_policy_json TEXT NOT NULL DEFAULT '{}'
    CHECK(json_valid(promotion_policy_json) AND json_type(promotion_policy_json)='object'),
  auto_promotion_enabled INTEGER NOT NULL DEFAULT 0 CHECK(auto_promotion_enabled IN (0,1)),
  status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','retired')),
  created_by_principal_id TEXT NOT NULL,
  approved_by_principal_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(workspace_id,id),
  UNIQUE(workspace_id,id,work_class),
  UNIQUE(workspace_id,id,version),
  UNIQUE(workspace_id,work_class,version),
  FOREIGN KEY(workspace_id,created_by_principal_id)
    REFERENCES memberships(workspace_id,principal_id),
  FOREIGN KEY(workspace_id,approved_by_principal_id)
    REFERENCES memberships(workspace_id,principal_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_delegation_profile
  ON delegation_profiles(workspace_id,work_class) WHERE status='active';

-- tasks.assignee_id remains the accountable owner; current_executor_id is
-- the person/Agent responsible for the next executable step.
CREATE TABLE IF NOT EXISTS task_responsibilities (
  workspace_id TEXT NOT NULL,
  task_id TEXT NOT NULL,
  current_executor_id TEXT NOT NULL,
  profile_id TEXT,
  work_class TEXT NOT NULL,
  autonomy_level TEXT NOT NULL CHECK(autonomy_level IN (
    'human_executes','agent_prepares','agent_supervised','agent_bounded','agent_exception_only')),
  effective_allowed_actions_json TEXT NOT NULL DEFAULT '[]'
    CHECK(json_valid(effective_allowed_actions_json) AND json_type(effective_allowed_actions_json)='array'),
  effective_reversible_actions_json TEXT NOT NULL DEFAULT '[]'
    CHECK(json_valid(effective_reversible_actions_json) AND json_type(effective_reversible_actions_json)='array'),
  effective_approval_conditions_json TEXT NOT NULL DEFAULT '[]'
    CHECK(json_valid(effective_approval_conditions_json) AND json_type(effective_approval_conditions_json)='array'),
  success_criteria_json TEXT NOT NULL DEFAULT '[]'
    CHECK(json_valid(success_criteria_json) AND json_type(success_criteria_json)='array'),
  evidence_requirements_json TEXT NOT NULL DEFAULT '[]'
    CHECK(json_valid(evidence_requirements_json) AND json_type(evidence_requirements_json)='array'),
  escalation_policy_json TEXT NOT NULL DEFAULT '{}'
    CHECK(json_valid(escalation_policy_json) AND json_type(escalation_policy_json)='object'),
  profile_version INTEGER NOT NULL DEFAULT 1 CHECK(profile_version >= 1),
  status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','paused','revoked')),
  version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
  updated_by_principal_id TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(workspace_id,task_id),
  FOREIGN KEY(workspace_id,task_id) REFERENCES tasks(workspace_id,id),
  FOREIGN KEY(workspace_id,current_executor_id) REFERENCES memberships(workspace_id,principal_id),
  FOREIGN KEY(workspace_id,updated_by_principal_id) REFERENCES memberships(workspace_id,principal_id),
  FOREIGN KEY(workspace_id,profile_id,work_class)
    REFERENCES delegation_profiles(workspace_id,id,work_class),
  FOREIGN KEY(workspace_id,profile_id,profile_version)
    REFERENCES delegation_profiles(workspace_id,id,version),
  CHECK(profile_id IS NOT NULL OR (
    autonomy_level='human_executes' AND effective_allowed_actions_json='[]'))
);
CREATE INDEX IF NOT EXISTS idx_task_responsibility_executor
  ON task_responsibilities(workspace_id,current_executor_id,status);

-- Immutable audit of assignment, delegation, review, promotion, demotion and override.
CREATE TABLE IF NOT EXISTS responsibility_events (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  task_id TEXT NOT NULL,
  event_type TEXT NOT NULL CHECK(event_type IN (
    'assigned','delegated','promotion_proposed','promoted','demoted',
    'human_takeover','agent_resumed','reviewed','override','revoked','escalated')),
  from_actor_id TEXT,
  to_actor_id TEXT,
  from_autonomy_level TEXT CHECK(from_autonomy_level IS NULL OR from_autonomy_level IN (
    'human_executes','agent_prepares','agent_supervised','agent_bounded','agent_exception_only')),
  to_autonomy_level TEXT CHECK(to_autonomy_level IS NULL OR to_autonomy_level IN (
    'human_executes','agent_prepares','agent_supervised','agent_bounded','agent_exception_only')),
  profile_id TEXT,
  profile_version INTEGER,
  actor_id TEXT NOT NULL,
  reason TEXT NOT NULL,
  evidence_json TEXT NOT NULL DEFAULT '{}'
    CHECK(json_valid(evidence_json) AND json_type(evidence_json)='object'),
  source_ref TEXT,
  occurred_at TEXT NOT NULL,
  recorded_at TEXT NOT NULL,
  UNIQUE(workspace_id,id),
  FOREIGN KEY(workspace_id,task_id) REFERENCES tasks(workspace_id,id),
  FOREIGN KEY(workspace_id,from_actor_id) REFERENCES memberships(workspace_id,principal_id),
  FOREIGN KEY(workspace_id,to_actor_id) REFERENCES memberships(workspace_id,principal_id),
  FOREIGN KEY(workspace_id,actor_id) REFERENCES memberships(workspace_id,principal_id),
  FOREIGN KEY(workspace_id,profile_id) REFERENCES delegation_profiles(workspace_id,id),
  FOREIGN KEY(workspace_id,profile_id,profile_version)
    REFERENCES delegation_profiles(workspace_id,id,version),
  CHECK((profile_id IS NULL AND profile_version IS NULL) OR
        (profile_id IS NOT NULL AND profile_version IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_responsibility_events_task
  ON responsibility_events(workspace_id,task_id,recorded_at);
CREATE TRIGGER IF NOT EXISTS trg_responsibility_events_no_update
  BEFORE UPDATE ON responsibility_events
  BEGIN SELECT RAISE(ABORT,'responsibility_events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_responsibility_events_no_delete
  BEFORE DELETE ON responsibility_events
  BEGIN SELECT RAISE(ABORT,'responsibility_events are append-only'); END;

-- Human work is a first-class loop participant, not only a tool-approval gate.
-- Host-deferred tool approvals remain in approval_requests as a distinct object.
CREATE TABLE IF NOT EXISTS human_action_requests (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  task_id TEXT,
  requested_by_principal_id TEXT NOT NULL,
  assignee_principal_id TEXT NOT NULL,
  request_kind TEXT NOT NULL CHECK(request_kind IN ('input','decision','review','execution','exception')),
  title TEXT NOT NULL,
  request_json TEXT NOT NULL DEFAULT '{}'
    CHECK(json_valid(request_json) AND json_type(request_json)='object'),
  due_at TEXT,
  urgency TEXT NOT NULL DEFAULT 'batch' CHECK(urgency IN ('batch','notify','interrupt')),
  status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN (
    'pending','in_progress','completed','declined','expired','cancelled')),
  response_json TEXT CHECK(response_json IS NULL OR (
    json_valid(response_json) AND json_type(response_json)='object')),
  response_source_ref TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  responded_at TEXT,
  UNIQUE(workspace_id,id),
  FOREIGN KEY(workspace_id,task_id) REFERENCES tasks(workspace_id,id),
  FOREIGN KEY(workspace_id,requested_by_principal_id)
    REFERENCES memberships(workspace_id,principal_id),
  FOREIGN KEY(workspace_id,assignee_principal_id)
    REFERENCES memberships(workspace_id,principal_id)
);
CREATE INDEX IF NOT EXISTS idx_human_action_inbox
  ON human_action_requests(workspace_id,assignee_principal_id,status,due_at);
