// Work bot Pi extension: system prompt + tool_call approval guard (Host-layer fill for deferred tool calls).
//
// Guarded tools are configured in WORKBOT_GUARDED_TOOLS (JSON):
//   {"telegram_send_external": {"action": "send_external_message", "conditions_from": "recipient_is_new"}}
// For a guarded call the hook asks WorkBench how the business action routes for the current task:
//   agent_execute / agent_execute_with_review -> allowed
//   human_approval_required -> allowed only if an approval for this exact tool+input exists (consumed once);
//                              otherwise an approval request is filed and the run ends (terminate).
//   out_of_scope / human_execution_required / agent_prepare_only -> blocked with the reason.
import { readFileSync } from 'node:fs';
import { wb } from '../workbench-client.mjs';

const GUARDED: Record<string, any> = {
  send_external_message: { action: 'send_external_message', task_from: 'task_id', conditions_from: 'new_recipient' },
  ...JSON.parse(process.env.WORKBOT_GUARDED_TOOLS || '{}'),
};
const PROMPT_FILE = process.env.WORKBOT_PROMPT_FILE || new URL('./WORKBOT.md', import.meta.url).pathname;
const APPROVAL_HOURS = Number(process.env.WORKBOT_APPROVAL_HOURS || 24);
// Delegation is evaluated for the Agent that executes, not for the person who is chatting.
const AGENT_IDENTITY = { provider: 'host', bot_id: process.env.WORKBOT_BOT_ID || 'workbot',
  external_user_id: process.env.WORKBOT_AGENT_EXTERNAL_ID || 'agent' };

export default function (pi: any) {
  pi.on('before_agent_start', async (event: any, ctx: any) => {
    const turn = (globalThis as any).__workbotTurns?.get(ctx?.sessionManager?.getSessionId?.());
    if (turn?.wakeEventId) {
      try { pi.appendEntry('workbench-wake', { wake_event_ids: [turn.wakeEventId, ...(turn.extraWakeIds || [])] }); } catch {}
    }
    let prompt = '';
    try { prompt = readFileSync(PROMPT_FILE, 'utf8'); } catch { prompt = ''; }
    return { systemPrompt: event.systemPrompt + '\n\n' + prompt };
  });

  pi.on('tool_call', async (event: any, ctx: any) => {
    const rule = GUARDED[event.toolName];
    if (!rule) return undefined;
    const sessionId = ctx.sessionManager?.getSessionId?.();
    const turn = (globalThis as any).__workbotTurns?.get(sessionId);
    if (!turn?.identity) return { block: true, reason: 'No verified identity for this run; guarded tool refused.', terminate: true };
    const conditions = rule.conditions_from && event.input?.[rule.conditions_from] ? [rule.conditions_from] : [];
    // The guarded call must name the task it serves (rule.task_from, default "task_id"); the task that
    // happened to wake this Run does not authorise unrelated calls.
    const taskId = event.input?.[rule.task_from || 'task_id'] || null;
    let route = 'human_approval_required';
    if (taskId) {
      try {
        route = (await wb('/v1/evaluate_action', { identity: turn.agentIdentity || AGENT_IDENTITY, task_id: taskId,
          action: rule.action, triggered_conditions: conditions })).route;
      } catch { route = 'human_approval_required'; }
    }
    if (route === 'agent_execute' || route === 'agent_execute_with_review') return undefined;
    if (route !== 'human_approval_required') {
      return { block: true, reason: `Not delegated (${route}). Prepare it for the owner instead of executing.` };
    }
    const sessionRef = turn.sessionFile || sessionId;
    const consumed = await wb('/v1/approvals/consume', { tool_name: event.toolName, tool_input: event.input,
      session_ref: sessionRef, task_id: taskId }).catch(() => null);
    if (consumed) return undefined;
    const expires = new Date(Date.now() + APPROVAL_HOURS * 3600e3).toISOString().replace(/\.\d+Z$/, 'Z');
    try {
      const request = await wb('/v1/approvals/request', {
        identity: turn.identity, wake_event_id: turn.wakeEventId, task_id: taskId,
        tool_name: event.toolName, tool_input: event.input,
        summary: `${rule.action}: ${JSON.stringify(event.input).slice(0, 300)}`,
        host_deferred_ref: `${sessionRef}#${event.toolCallId}`, expires_at: expires,
      });
      turn.pendingApproval = request.approval_id;
      return { block: true, terminate: true,
        reason: `Approval requested (${request.approval_id}). Stop here and tell the user it is waiting for the owner's approval.` };
    } catch (error: any) {
      return { block: true, terminate: true, reason: 'Approval could not be filed: ' + String(error?.message || error).slice(0, 120) };
    }
  });
}
