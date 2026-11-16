// Work bot gateway (Agent Host) — a separate Pi process from the personal companion bot.
//
// Loop (Anthropic "always-on" shape): inbound message / timer / approval / external change
//   -> durable WorkBench event -> short Pi Run with WorkBench tools -> facts written back -> sleep.
// Host-layer fills for Pi (see docs/HOST_RUN_PROTOCOL.md): a local ledger (event id -> session, outcome),
// the tool_call approval guard (agent/guard-extension.ts), and reopening the same session after approval.
import './load-env.mjs'; // must stay first: loads WORKBOT_ENV_FILES before other modules read env
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import { wb } from './workbench-client.mjs';
import { createWorkbenchTools, retryMemoryWrites } from './tools.mjs';
import { setTurn, clearTurn } from './turn-context.mjs';

const RUNTIME = process.env.PI_RUNTIME || '/opt/xiyue-runtime';
const require = createRequire(RUNTIME + '/package.json');
const { createAgentSession, SessionManager, ModelRuntime, DefaultResourceLoader } =
  await import(pathToFileURL(path.join(RUNTIME, 'node_modules/@earendil-works/pi-coding-agent/dist/index.js')).href);
const { Bot } = require('grammy');

const env = process.env;
const BOT_ID = env.WORKBOT_BOT_ID || 'workbot';
const AGENT_IDENTITY = { provider: 'host', bot_id: BOT_ID, external_user_id: env.WORKBOT_AGENT_EXTERNAL_ID || 'agent' };
// Agents this Host runs: principal_id -> external id bound under provider "host" (default: just the one Agent).
const AGENTS = JSON.parse(env.WORKBOT_AGENTS || '{}');
const agentIdentityFor = principal => (principal && AGENTS[principal]
  ? { provider: 'host', bot_id: BOT_ID, external_user_id: AGENTS[principal] } : AGENT_IDENTITY);
const AGENT_DIR = env.WORKBOT_AGENT_DIR || '/workbot/agent';
const WORK_DIR = env.WORKBOT_WORK_DIR || '/workbot/workspace';
const LEDGER = path.join(WORK_DIR, 'ledger.jsonl');
const RECIPIENT_CHATS = JSON.parse(env.WORKBOT_RECIPIENT_CHATS || '{}'); // principal_id -> telegram chat id
const OWNER_PRINCIPAL = env.WORKBOT_OWNER_PRINCIPAL || 'owner';
const POLL_MS = Number(env.WORKBOT_POLL_SECONDS || 20) * 1000;
const TOOL_NAMES = ['list_tasks', 'get_task', 'change_tasks', 'whats_new', 'work_memory_search', 'work_memory_write',
  'send_external_message', ...Object.keys(JSON.parse(env.WORKBOT_GUARDED_TOOLS || '{}'))];
const CLI = env.WORKBOT_TRANSPORT === 'cli';
if ((!CLI && !env.WORKBOT_TELEGRAM_TOKEN) || !env.WORKBENCH_HOST_TOKEN || !env.WORKBENCH_WORKSPACE) {
  throw Error('WORKBOT_TELEGRAM_TOKEN, WORKBENCH_HOST_TOKEN and WORKBENCH_WORKSPACE are required');
}
fs.mkdirSync(WORK_DIR, { recursive: true });

const runtime = await ModelRuntime.create();
const [provider, modelId] = (env.WORKBOT_MODEL || 'xai/grok-4.7').split('/');
const model = runtime.getModel(provider, modelId);
if (!model) throw Error('model not available in runtime: ' + env.WORKBOT_MODEL);
const bot = CLI ? (await import('./cli-transport.mjs')).createCliBot(env)
  : new Bot(env.WORKBOT_TELEGRAM_TOKEN, { client: { timeoutSeconds: 90 } });

// ------------------------------------------------------------------ ledger
const ledger = new Map();
if (fs.existsSync(LEDGER)) {
  for (const line of fs.readFileSync(LEDGER, 'utf8').split('\n').filter(Boolean)) {
    try { const entry = JSON.parse(line); ledger.set(entry.event_id, { ...ledger.get(entry.event_id), ...entry }); } catch {}
  }
}
function record(entry) {
  ledger.set(entry.event_id, { ...ledger.get(entry.event_id), ...entry });
  fs.appendFileSync(LEDGER, JSON.stringify({ ...entry, at: new Date().toISOString() }) + '\n');
}

// ---------------------------------------------------------------- sessions
const sessions = new Map(); // chatKey -> { session, manager }
const lanes = new Map();    // chatKey -> promise (one Run at a time per chat)

async function sessionFor(chatKey, sessionFile) {
  if (!sessionFile && sessions.has(chatKey)) return sessions.get(chatKey);
  const cwd = path.join(WORK_DIR, 'chats', chatKey);
  fs.mkdirSync(cwd, { recursive: true });
  const manager = sessionFile && fs.existsSync(sessionFile) ? SessionManager.open(sessionFile) : SessionManager.continueRecent(cwd);
  const resourceLoader = new DefaultResourceLoader({ cwd, agentDir: AGENT_DIR });
  await resourceLoader.reload();
  const { session } = await createAgentSession({ cwd, model, modelRuntime: runtime, sessionManager: manager,
    resourceLoader, customTools: createWorkbenchTools(manager) });
  await session.bindExtensions({});
  session.setActiveToolsByName(TOOL_NAMES.filter(name => session.getAllToolNames?.().includes?.(name) ?? true));
  const entry = { session, manager };
  sessions.set(chatKey, entry);
  return entry;
}

function inLane(chatKey, job) {
  const next = (lanes.get(chatKey) || Promise.resolve()).then(job, job);
  lanes.set(chatKey, next.catch(() => {}));
  return next;
}

async function runTurn({ chatKey, chatId, identity, wakeEventId, taskId, sourceRef, text, sessionFile,
                         extraWakeIds = [], inboxIds = [], role, agentIdentity = AGENT_IDENTITY }) {
  const { session, manager } = await sessionFor(chatKey, sessionFile);
  const sessionId = manager.getSessionId();
  const file = manager.getSessionFile?.() || sessionFile || '';
  const turn = { identity, wakeEventId, extraWakeIds, taskId, sourceRef, sessionFile: file, agentIdentity };
  setTurn(sessionId, turn);
  for (const id of [wakeEventId, ...extraWakeIds]) {
    record({ event_id: id, status: 'running', chat_key: chatKey, chat_id: chatId, session_file: file, identity });
  }
  const start = session.state.messages.length;
  try {
    await session.prompt(text);
  } catch (error) {
    // A failed Run is settled as failed, never left leased to turn into "unknown".
    for (const id of [wakeEventId, ...extraWakeIds]) {
      const failed = { status: 'failed', run_ref: `${sessionId}:${wakeEventId}:failed` };
      record({ event_id: id, ...failed });
      await wb('/v1/wake/settle', { event_id: id, ...failed }).catch(() => {});
    }
    throw error;
  } finally {
    clearTurn(sessionId);
  }
  // Only the final answer goes out: text the model wrote between tool calls is its working narration.
  const answers = session.state.messages.slice(start).filter(m => m.role === 'assistant')
    .map(m => m.content.filter(part => part.type === 'text').map(part => part.text).join('\n').trim()).filter(Boolean);
  const reply = answers.at(-1) || '';
  // Tie the source messages to every task this Run touched, so later edits can be traced to them.
  if (inboxIds.length && turn.touched?.size) {
    const ops = [...turn.touched].flatMap(task => inboxIds.map(inbox => ({ op: 'link_inbox', task_id: task, inbox_id: inbox,
      relation: role === 'requester' ? 'request' : 'context', evidence_type: 'system' })));
    await wb('/v1/tools/change_tasks', { identity, idempotency_key: `${wakeEventId}:links`, operations: ops.slice(0, 40) })
      .catch(error => console.error('link failed:', error.code || error.message));
  }
  for (const id of [wakeEventId, ...extraWakeIds]) {
    const outcome = { status: 'completed', run_ref: `${sessionId}:${wakeEventId}`, session_ref: file };
    record({ event_id: id, ...outcome });
    await wb('/v1/wake/settle', { event_id: id, ...outcome }).catch(() => {});
  }
  return reply;
}

async function sendLong(chatId, text) {
  const ids = [];
  for (let i = 0; i < text.length; i += 3900) {
    const message = await bot.api.sendMessage(chatId, text.slice(i, i + 3900));
    ids.push(message.message_id);
  }
  return ids;
}

// ---------------------------------------------------------------- inbound
bot.command(['approve', 'deny'], async ctx => {
  const approvalId = (ctx.match || '').trim();
  const identity = { provider: 'telegram', bot_id: BOT_ID, external_user_id: String(ctx.from.id) };
  try {
    const result = await wb('/v1/approvals/decide', { identity, approval_id: approvalId,
      approve: ctx.message.text.startsWith('/approve'),
      source_ref: `tg://${BOT_ID}/${ctx.chat.id}/${ctx.message.message_id}` });
    await ctx.reply(`审批 ${approvalId}：${result.status}`);
  } catch (error) {
    await ctx.reply('审批没有记录：' + (error.code || error.message));
  }
});

// Messages are stored durably the moment they arrive; messages sent in quick succession by the
// same person are answered in one Run (merge window), and every one of their wake events is settled.
const MERGE_MS = Number(env.WORKBOT_MERGE_MS || 2500);
const pending = new Map(); // chatKey -> { ctx, items: [{intake, text, sourceRef}], timer }

function queueForRun(chatKey, ctx, item) {
  const key = chatKey; // chatKey includes the sender: people in a group never share a session or a merge
  const slot = pending.get(key) || { ctx, items: [], timer: null };
  slot.items.push(item);
  slot.ctx = ctx;
  clearTimeout(slot.timer);
  slot.timer = setTimeout(() => { pending.delete(key); flush(chatKey, slot).catch(() => {}); }, MERGE_MS);
  pending.set(key, slot);
}

async function flush(chatKey, { ctx, items }) {
  const first = items[0];
  const identity = { provider: 'telegram', bot_id: BOT_ID, external_user_id: String(ctx.from.id) };
  const text = items.map(i => `[${i.label} · 来自 ${i.intake.actor.principal_id}（${i.intake.actor.role}）· source_ref ${i.sourceRef}]\n${i.text}`).join('\n\n');
  await inLane(chatKey, async () => {
    const typing = setInterval(() => ctx.replyWithChatAction('typing').catch(() => {}), 4500);
    const ok = await (async () => {
      try {
        const reply = await runTurn({ chatKey, chatId: ctx.chat.id, identity, wakeEventId: first.intake.wake_event_id, sourceRef: first.sourceRef,
          text, extraWakeIds: items.slice(1).map(i => i.intake.wake_event_id),
          inboxIds: items.map(i => i.intake.inbox_id), role: first.intake.actor.role });
        if (reply) await sendLong(ctx.chat.id, reply);
        return true;
      } catch (error) {
        console.error('turn failed:', String(error?.message || error).slice(0, 200));
        await ctx.reply('这次没处理完，已经记在工作台，稍后会再看。').catch(() => {});
        return false;
      }
    })();
    clearInterval(typing);
    for (const item of items) {
      if (item.label === '消息') await wb('/v1/intake/finish', { inbox_id: item.intake.inbox_id, ok }).catch(() => {});
    }
    bot.turnDone?.(chatKey); // CLI transport waits on this instead of on the first outgoing message
  });
}

bot.on('message:text', async ctx => {
  const sourceRef = `tg://${BOT_ID}/${ctx.chat.id}/${ctx.message.message_id}`;
  let intake;
  try {
    intake = await wb('/v1/intake/inbound', { platform: 'telegram', bot_id: BOT_ID, update_id: String(ctx.update.update_id),
      chat_id: String(ctx.chat.id), message_id: String(ctx.message.message_id), external_user_id: String(ctx.from.id),
      raw_source_ref: sourceRef, handled_inline: true });
  } catch (error) {
    return ctx.reply('工作台暂时连不上，稍后再试。');
  }
  if (intake.duplicate) return;
  if (!intake.accepted) return ctx.reply('这个账号还没有接入工作台。');
  queueForRun(`tg-${ctx.chat.id}-${ctx.from.id}`, ctx, { intake, sourceRef, label: '消息', text: ctx.message.text });
});

bot.on('edited_message:text', async ctx => {
  const message = ctx.editedMessage;
  const sourceRef = `tg://${BOT_ID}/${ctx.chat.id}/${message.message_id}?edit=${ctx.update.update_id}`;
  const intake = await wb('/v1/intake/edited', { platform: 'telegram', bot_id: BOT_ID, update_id: String(ctx.update.update_id),
    chat_id: String(ctx.chat.id), message_id: String(message.message_id), external_user_id: String(ctx.from.id),
    raw_source_ref: sourceRef }).catch(() => null);
  if (!intake?.accepted || intake.duplicate) return;
  const note = intake.linked_tasks?.length ? `（这条消息关联任务：${intake.linked_tasks.join(', ')}，请核对是否需要更正）` : '';
  queueForRun(`tg-${ctx.chat.id}-${ctx.from.id}`, ctx, { intake, sourceRef, label: '编辑了之前的消息' + note, text: message.text });
});

// -------------------------------------------------------- wake events loop
const EVENT_PROMPTS = {
  timer_fired: '一个提醒或周期任务到点了。先 get_task 看当前状态：条件已不成立就不打扰；需要知道或行动，就用一两句话说明（只会发给团队内部的人，不会发给外部请求方）。',
  external_change: '外部状态变了（例如 CI、文件、对方回复）。核对任务，更新进度/依赖；需要主人知道才说。',
  human_action_completed: '主人回复了一个待办请求。读取任务和回复内容，按回复继续推进。',
  approval_decided: '审批有结果了。若已批准，用完全相同的参数重新发起刚才被拦下的调用；若被拒绝或过期，告诉对方并停止。',
  agent_handoff: '另一个 Agent 交接了一项工作。读任务和最新书签，核实后继续。',
  delegation_review_due: '委托复查到期。按任务记录汇总事实给主人，不要自己改委托。',
  responsibility_changed: '这项任务的执行人或委托级别变了。读任务，按新的范围继续或停下。',
  user_message: '有一条稍后处理的消息。',
};

async function pollWake() {
  const event = await wb('/v1/wake/claim', { lease_seconds: 900 }).catch(() => null);
  if (!event) return false;
  // Background work runs as the Agent that currently executes the task (after a handoff: the new one),
  // in that Agent's own ops lane.
  const agentIdentity = agentIdentityFor(event.executor_id);
  let chatKey = `ops-${agentIdentity.external_user_id}`, identity = agentIdentity, sessionFile, chatId = null;
  if (event.kind === 'approval_decided' && event.deferred_ref) {
    // Reopen the very session whose tool call was blocked; it keeps the original sender's identity.
    const blockedSession = event.deferred_ref.split('#')[0];
    const original = [...ledger.values()].reverse().find(e => e.session_file === blockedSession && e.chat_key);
    if (original) ({ chat_key: chatKey, identity, session_file: sessionFile, chat_id: chatId } = original);
  }
  const text = `[系统事件 ${event.kind} · event ${event.event_id} · task ${event.task_id || '无'} · source_ref ${event.source_ref}]\n`
    + (EVENT_PROMPTS[event.kind] || '处理这个事件。');
  await inLane(chatKey, async () => {
    try {
      const reply = await runTurn({ chatKey, chatId, identity, wakeEventId: event.event_id, taskId: event.task_id,
        sourceRef: event.source_ref, text, sessionFile, agentIdentity });
      if (reply && chatKey.startsWith('ops-')) {
        // A fired reminder goes to its own recipient; other background results go to the owner.
        await wb('/v1/delivery/enqueue', { recipient_id: event.recipient_id || OWNER_PRINCIPAL, channel: 'telegram',
          payload: { text: reply }, dedupe_key: `wake:${event.event_id}`, task_id: event.task_id }).catch(() => {});
      } else if (reply && chatId) {
        await sendLong(chatId, reply);
      }
    } catch (error) {
      // Leave the lease to expire: WorkBench marks it unknown and the ledger tells us on restart.
      console.error('wake run failed:', event.kind, String(error?.message || error).slice(0, 200));
    }
  });
  return true;
}

async function pumpDeliveries() {
  const items = await wb('/v1/delivery/claim', { limit: 10 }).catch(() => []);
  for (const item of items || []) {
    const chatId = RECIPIENT_CHATS[item.recipient_id];
    if (!chatId) {
      await wb('/v1/delivery/settle', { delivery_id: item.delivery_id, status: 'failed' }).catch(() => {});
      continue;
    }
    try {
      const ids = await sendLong(chatId, String(item.payload?.text || ''));
      await wb('/v1/delivery/settle', { delivery_id: item.delivery_id, status: 'sent', provider_message_id: ids.join(',') });
    } catch (error) {
      const definite = Number(error?.error_code) >= 400 && Number(error?.error_code) < 500;
      await wb('/v1/delivery/settle', { delivery_id: item.delivery_id, status: definite ? 'failed' : 'unknown' }).catch(() => {});
    }
  }
}

async function reconcileFromLedger() {
  // The ledger is the Host's record per event: completed runs are re-settled; a run still "running"
  // when this process died ended without a result and is settled as failed.
  for (const entry of ledger.values()) {
    if (entry.status === 'completed' && entry.run_ref) {
      await wb('/v1/wake/settle', { event_id: entry.event_id, status: 'completed', run_ref: entry.run_ref,
        session_ref: entry.session_ref }).catch(() => {});
    } else if (entry.status === 'running') {
      const failed = { status: 'failed', run_ref: `${entry.event_id}:host-restarted` };
      record({ event_id: entry.event_id, ...failed });
      await wb('/v1/wake/settle', { event_id: entry.event_id, ...failed }).catch(() => {});
    }
  }
}

await reconcileFromLedger();
setInterval(async () => {
  try {
    while (await pollWake()) { /* drain one lane at a time; WorkBench allows one live Run per workspace */ }
    await pumpDeliveries();
    await retryMemoryWrites();
  } catch (error) {
    console.error('loop error:', String(error?.message || error).slice(0, 200));
  }
}, POLL_MS);
bot.catch(error => console.error('telegram error:', String(error?.message || error).slice(0, 200)));
await bot.start({ onStart: () => console.log('work bot polling') });
