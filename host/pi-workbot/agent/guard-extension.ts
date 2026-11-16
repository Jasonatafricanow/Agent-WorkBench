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

const GUARDED: Record<string, any> = JSON.parse(process.env.WORKBOT_GUARDED_TOOLS || '{}');
const PROMPT_FILE = process.env.WORKBOT_PROMPT_FILE || new URL('./WORKBOT.md', import.meta.url).pathname;
const APPROVAL_HOURS = Number(process.env.WORKBOT_APPROVAL_HOURS || 24);

export default function (pi: any) {
  pi.on('before_agent_start', async (event: any) => {
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
    let route = 'human_approval_required';
    if (turn.taskId) {
      try {
        route = (await wb('/v1/evaluate_action', { identity: turn.identity, task_id: turn.taskId,
          action: rule.action, triggered_conditions: conditions })).route;
      } catch { route = 'human_approval_required'; }
    }
    if (route === 'agent_execute' || route === 'agent_execute_with_review') return undefined;
    if (route !== 'human_approval_required') {
      return { block: true, reason: `Not delegated (${route}). Prepare it for the owner instead of executing.` };
    }
    const consumed = await wb('/v1/approvals/consume', { tool_name: event.toolName, tool_input: event.input }).catch(() => null);
    if (consumed) return undefined;
    const expires = new Date(Date.now() + APPROVAL_HOURS * 3600e3).toISOString().replace(/\.\d+Z$/, 'Z');
    try {
      const request = await wb('/v1/approvals/request', {
        identity: turn.identity, wake_event_id: turn.wakeEventId, task_id: turn.taskId || null,
        tool_name: event.toolName, tool_input: event.input,
        summary: `${rule.action}: ${JSON.stringify(event.input).slice(0, 300)}`,
        host_deferred_ref: `${turn.sessionFile || sessionId}#${event.toolCallId}`, expires_at: expires,
      });
      turn.pendingApproval = request.approval_id;
      return { block: true, terminate: true,
        reason: `Approval requested (${request.approval_id}). Stop here and tell the user it is waiting for the owner's approval.` };
    } catch (error: any) {
      return { block: true, terminate: true, reason: 'Approval could not be filed: ' + String(error?.message || error).slice(0, 120) };
    }
  });
}
