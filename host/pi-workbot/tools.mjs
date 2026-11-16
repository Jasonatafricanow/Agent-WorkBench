// Agent-facing tools: the WorkBench facade (list/get/change/whats_new) plus work-scoped memory.
// Memory is written by the Agent itself at the end of a turn (owner decision 11-17), like a handoff note.
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { wb } from './workbench-client.mjs';
import { getTurn } from './turn-context.mjs';

const require = createRequire((process.env.PI_RUNTIME || '/opt/xiyue-runtime') + '/package.json');
const { Type } = require('typebox');
const MEM = (process.env.MRMEM_URL || 'http://127.0.0.1:8880').replace(/\/$/, '');
const MEM_SCOPE = process.env.WORKBOT_MEMORY_SCOPE || 'workbench';
const AGENT_IDENTITY = { provider: 'host', bot_id: process.env.WORKBOT_BOT_ID || 'workbot',
  external_user_id: process.env.WORKBOT_AGENT_EXTERNAL_ID || 'agent' };
export const RETRY_FILE = path.join(process.env.WORKBOT_WORK_DIR || '/workbot/workspace', 'memory-retry.jsonl');

// Replay queued memory writes (called by the gateway loop). Keeps lines that still fail.
export async function retryMemoryWrites() {
  if (!fs.existsSync(RETRY_FILE)) return 0;
  const lines = fs.readFileSync(RETRY_FILE, 'utf8').split('\n').filter(Boolean);
  const left = [];
  for (const line of lines) {
    try {
      const { path: p, body, identity } = JSON.parse(line);
      // Re-check the grant at replay time: a revoked grant drops the queued write.
      const allowed = await wb('/v1/memory/authorize', { identity: identity || AGENT_IDENTITY, action: 'memory.write' })
        .then(() => true).catch(error => (error.status === 403 ? false : null));
      if (allowed === false) continue;
      if (allowed === null) { left.push(line); continue; }
      const response = await fetch(MEM + p, { method: 'POST', signal: AbortSignal.timeout(8000),
        headers: { Authorization: 'Bearer ' + (process.env.MRMEM_TOKEN || ''), 'Content-Type': 'application/json' },
        body: JSON.stringify(body) });
      if (!response.ok) left.push(line);
    } catch { left.push(line); }
  }
  fs.writeFileSync(RETRY_FILE, left.length ? left.join('\n') + '\n' : '');
  return lines.length - left.length;
}
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
  const memory = async (path, body, { retry = false } = {}) => {
    // Reading: the person in this chat must hold a grant (requesters normally do not), so nothing private
    // reaches their turn. Writing at wrap-up is done by the handling Agent, under the Agent's own grant.
    const current = turn();
    const identity = path === '/search' ? current.identity : (current.agentIdentity || AGENT_IDENTITY);
    let grant;
    try {
      grant = await wb('/v1/memory/authorize', { identity, action: path === '/search' ? 'memory.read' : 'memory.write' });
    } catch {
      return { ...out({ error: 'MEMORY_FORBIDDEN', message: 'no memory access for this identity' }), isError: true };
    }
    body = { ...body, user_id: grant.memory_user_id };
    try {
      const response = await fetch(MEM + path, {
        method: 'POST', signal: AbortSignal.timeout(8000),
        headers: { Authorization: 'Bearer ' + (process.env.MRMEM_TOKEN || ''), 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...body, scope: MEM_SCOPE, collection: 'memory' }),
      });
      if (!response.ok) throw Error('HTTP ' + response.status);
      return out(await response.json());
    } catch (error) {
      if (retry) {  // the write has a deterministic id, so the gateway can replay it safely later
        fs.appendFileSync(RETRY_FILE, JSON.stringify({ path, identity, body: { ...body, scope: MEM_SCOPE, collection: 'memory' } }) + '\n');
        return out({ queued: true, message: 'memory service unavailable; write queued for retry' });
      }
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
      description: 'Apply 1-40 operations atomically (all or nothing). Ops: create_task, patch_task, set_official_due, transition, append_progress, set_focus(owner), add_dependency, resolve_dependency, set_reminder, cancel_reminder, link_inbox, request_human_action, respond_human_action, record_commitment, update_commitment, register_deliverable, update_deliverable, save_checkpoint, create_schedule, set_schedule_active, add_watch, assign_responsibility(owner), set_responsibility_status(owner), propose_change, create_project, update_project, handoff. add_dependency kind: external_response|prerequisite_task|artifact|permission|other. set_reminder accepts repeat_every_days + max_repeats for chasing until answered. Every op needs evidence_type (explicit_user|verified_tool|reported_third_party|inferred|system). Use "ref" to name a new object and "$ref" to use it later in the batch.',
      parameters: Type.Object({ operations: Type.Array(Type.Record(Type.String(), Type.Any()), { minItems: 1, maxItems: 40 }) }),
      execute: async (toolCallId, { operations }) => {
        const current = turn();
        // Idempotency key = source event + this tool call: a re-sent identical call replays its receipt.
        // A new Run has new tool call ids, so the model must still read before it writes (versions).
        try {
          const result = await wb('/v1/tools/change_tasks', { identity: current.identity,
            idempotency_key: `${current.wakeEventId || 'adhoc'}:${toolCallId}`, operations });
          current.touched ??= new Set();
          for (const item of result || []) if (item.task_id) current.touched.add(item.task_id);
          return out(result);
        } catch (error) {
          return { ...out({ error: error.code || 'ERROR', message: error.message, details: error.details }), isError: true };
        }
      } },
    { name: 'whats_new', label: '自上次以来的变化',
      description: 'Changes since this identity last looked (empty = nothing new). Use instead of re-reading everything.',
      parameters: Type.Object({}),
      execute: async () => call('/v1/tools/whats_new', { view: 'workbot', mark_seen: true }) },
    { name: 'send_external_message', label: '对外发消息',
      description: 'Send a message to someone (e.g. a requester) on behalf of the owner about a specific task. Always goes through the delegation/approval guard; if it is blocked, stop and say it awaits approval. Not for replying in the current chat — just reply normally there.',
      parameters: Type.Object({ task_id: Type.String({ minLength: 1 }), recipient_principal_id: Type.String({ minLength: 1 }),
        text: Type.String({ minLength: 1, maxLength: 3500 }), new_recipient: Type.Optional(Type.Boolean()) }),
      execute: async (toolCallId, { task_id, recipient_principal_id, text }) => {
        const current = turn();
        try {
          const result = await wb('/v1/delivery/enqueue', { recipient_id: recipient_principal_id, channel: 'telegram',
            payload: { text, kind: 'external_message' }, task_id, dedupe_key: `ext:${current.wakeEventId || 'adhoc'}:${toolCallId}` });
          return out({ queued: result.delivery_id, note: 'queued for delivery; it is only "sent" once the carrier confirms' });
        } catch (error) {
          return { ...out({ error: error.code || 'ERROR', message: error.message }), isError: true };
        }
      } },
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
          sources: [{ store_id: 'workbench', source_namespace: 'workbot', record_id: source_ref || current.sourceRef || '' }] },
          { retry: true });
      } },
  ];
}
