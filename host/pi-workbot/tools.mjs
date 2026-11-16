// Agent-facing tools: the WorkBench facade (list/get/change/whats_new) plus work-scoped memory.
// Memory is written by the Agent itself at the end of a turn (owner decision 11-17), like a handoff note.
import { createRequire } from 'node:module';
import { wb } from './workbench-client.mjs';
import { getTurn } from './turn-context.mjs';

const require = createRequire((process.env.PI_RUNTIME || '/opt/xiyue-runtime') + '/package.json');
const { Type } = require('typebox');
const MEM = (process.env.MRMEM_URL || 'http://127.0.0.1:8880').replace(/\/$/, '');
const MEM_SCOPE = process.env.WORKBOT_MEMORY_SCOPE || 'workbench';
const out = value => ({ content: [{ type: 'text', text: typeof value === 'string' ? value : JSON.stringify(value) }], details: {} });

export function createWorkbenchTools(sessionManager) {
  const turn = () => {
    const current = getTurn(sessionManager.getSessionId());
    if (!current?.identity) throw Error('no verified identity for this run');
    return current;
  };
  const call = async (path, body) => {
    try { return out(await wb(path, { identity: turn().identity, ...body })); }
    catch (error) { return { ...out({ error: error.code || 'ERROR', message: error.message, details: error.details }), isError: true }; }
  };
  const memory = async (path, body) => {
    try {
      const response = await fetch(MEM + path, {
        method: 'POST', signal: AbortSignal.timeout(8000),
        headers: { Authorization: 'Bearer ' + (process.env.MRMEM_TOKEN || ''), 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...body, scope: MEM_SCOPE, collection: 'memory' }),
      });
      if (!response.ok) throw Error('HTTP ' + response.status);
      return out(await response.json());
    } catch (error) {
      return out({ error: 'MEMORY_UNAVAILABLE', message: String(error.message).slice(0, 120) });
    }
  };
  return [
    { name: 'list_tasks', label: '工作台总览',
      description: 'WorkBench overview: NOW/NEXT focus, inbox, waiting (on whom), blocked, overdue, commitments, deliverables, my human requests. Requesters only see their own requests.',
      parameters: Type.Object({ limit: Type.Optional(Type.Integer({ minimum: 1, maximum: 200 })) }),
      execute: async (_id, { limit }) => call('/v1/tools/list_tasks', { limit: limit || 50 }) },
    { name: 'get_task', label: '读取任务',
      description: 'Read one task with its version, events, dependencies, reminders, commitments, deliverables, responsibility and latest checkpoint. Read before every change: writes need expected_version.',
      parameters: Type.Object({ task_id: Type.String({ minLength: 1 }) }),
      execute: async (_id, { task_id }) => call('/v1/tools/get_task', { task_id }) },
    { name: 'change_tasks', label: '更新工作台',
      description: 'Apply 1-40 operations atomically (all or nothing). Ops: create_task, patch_task, set_official_due, transition, append_progress, set_focus(owner), add_dependency, resolve_dependency, set_reminder, cancel_reminder, link_inbox, request_human_action, respond_human_action, record_commitment, update_commitment, register_deliverable, update_deliverable, save_checkpoint, create_schedule, set_schedule_active, add_watch, assign_responsibility(owner), set_responsibility_status(owner). Every op needs evidence_type (explicit_user|verified_tool|reported_third_party|inferred|system). Use "ref" to name a new object and "$ref" to use it later in the batch.',
      parameters: Type.Object({ operations: Type.Array(Type.Record(Type.String(), Type.Any()), { minItems: 1, maxItems: 40 }) }),
      execute: async (toolCallId, { operations }) => {
        const current = turn();
        // Idempotency key = the source event + this tool call, so a retried Run replays instead of duplicating.
        return call('/v1/tools/change_tasks', { idempotency_key: `${current.wakeEventId || 'adhoc'}:${toolCallId}`, operations });
      } },
    { name: 'whats_new', label: '自上次以来的变化',
      description: 'Changes since this identity last looked (empty = nothing new). Use instead of re-reading everything.',
      parameters: Type.Object({}),
      execute: async () => call('/v1/tools/whats_new', { view: 'workbot', mark_seen: true }) },
    { name: 'work_memory_search', label: '检索工作记忆',
      description: 'Search work-scoped memory for past decisions and context. Background only; current task state always comes from get_task.',
      parameters: Type.Object({ query: Type.String({ minLength: 1, maxLength: 500 }) }),
      execute: async (_id, { query }) => memory('/search', { text: query, limit: 5 }) },
    { name: 'work_memory_write', label: '收尾写工作记忆',
      description: 'At the end of a turn, write what future work needs (decision, preference, who/what/when and how it was settled), pointing at the source. Like a handoff note. Skip small talk.',
      parameters: Type.Object({ summary: Type.String({ minLength: 1, maxLength: 800 }), source_ref: Type.Optional(Type.String()) }),
      execute: async (toolCallId, { summary, source_ref }) => {
        const current = turn();
        return memory('/upsert', { id: `workbot:${current.wakeEventId || 'adhoc'}:${toolCallId}`, kind: 'aml-compiled-v1', text: summary,
          sources: [{ store_id: 'workbench', source_namespace: 'workbot', record_id: source_ref || current.sourceRef || '' }] });
      } },
  ];
}
