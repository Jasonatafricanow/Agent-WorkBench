# Agent Host / WorkBench protocol — draft v1

Status: **design plus internal SQL inbox implementation**, external Host adapter not wired.

## Port shape

```python
class AgentHostRunPort(Protocol):
    def submit_event(self, event: WakeEvent) -> HostOutcome: ...
```

`WakeEvent` is host-facing metadata, not executable instructions. Its
`source_ref` references an authenticated native source; the Agent Host
hydrates content from its own trusted source adapter. The Agent must not use
`event_id` as an authorization capability.

```json
{
  "workspace_id": "workspace-1",
  "event_id": "wake-7f21",
  "origin": "telegram",
  "origin_event_id": "bot-A/update-456",
  "kind": "user_message",
  "source_ref": "telegram://bot-A/chat-1/message-200",
  "created_at": "2026-11-16T15:00:00Z",
  "task_id": null
}
```

An Event has one dedupe key scoped to its workspace: `origin` +
`origin_event_id`. Sources include `user_message`, `timer_fired`,
`external_change`, `approval_decided`, `agent_handoff`. Events do not
contain arbitrary Agent prompt text or an `actor_id` supplied by the model.

## Run outcome acknowledgement

```json
{
  "status": "deferred",
  "run_ref": "host://runs/r-123",
  "session_ref": "host://sessions/s-1",
  "deferred_ref": "host://deferred/d-9"
}
```

- `completed`: Host Run completed processing; does not mark WorkBench Task DONE.
- `deferred`: Host owns a persisted suspended call, references required.
- `failed`: Host reports failure; WorkBench stores outcome for human/Agent review.
- `unknown`: WorkBench knows a Host dispatch may have occurred, but has no
  trustworthy outcome. It does **not** submit another Run automatically.
- `pending/leased`: WorkBench inbox dispatch states only; Host does not own them.

Host may return Run and Session refs but WorkBench must not load/overwrite
Host Session content. Agent Host must guarantee idempotent acceptance by
`event_id` and provide reconciliation/query-by-event-ID before production
use. Without those Host capabilities, automatic retries remain disabled.

## Decision / approval

`approval_requests` records business approval. The Host independently
evaluates effective tool permissions at the moment of execution or resume.
An approval answer comes back as a **new authenticated wake event**, never
by invoking a stored tool call directly from Telegram.

## Human action vs. deferred tool approval

WorkBench's `human_action_requests` is a durable queue for a person to supply
information, make a business decision, review a deliverable or perform a
real-world action. It is **not** the same as `approval_requests`, which
records a specific Host-deferred tool call. A human reply becomes a new
authenticated business event; it does not directly resume a saved tool call.
The Host receives the event and rechecks current tool permissions itself.

The task's responsible executor and autonomy level are WorkBench-owned,
versioned business policy. A Host Run is merely one execution attempt under
that policy. Human absence leaves human-dependent requests pending, while
other independently authorized events continue to dispatch. See
[ADR-0003](ADR-0003-PROGRESSIVE-DELEGATION.md).

## Linkage and observability

## Linkage and observability

For every business task change, append `task_events` plus a durable outbox
entry in one WorkBench transaction. Preserve `event_id`, `task_id`, optional
`host_run_ref` / `host_session_ref`, and `source_ref` for traceability.
Do not write Host tool logs, raw conversations or long-term semantic analyses
into WorkBench SQL as a parallel authority.

MR cognition signals are *suggestions* for Host context/activation;
MR-Mem retrieval is evidence-backed memory context. Neither authorizes
actions. Host controls rate limits, credentials, recipient selection and final
side effects.

## Implementer checklist

1. Record Host identity API, source hydration and permitted workspace mapping.
2. Implement Host `submit_event` idempotency by WorkBench event ID.
3. Implement Host lookup/reconciliation for unknown outcomes.
4. Verify Host Session persistence and deferred tool call resume semantics.
5. Verify per-tool auth revocation after approval was issued.
6. Test event batching/single active Run policy per user where the Host supports it.
7. Only after those gates, add the service worker and enable real notifications.

## Owner decision (2026-11-17): a separate work bot on Pi, gaps filled in the Host layer

External collaborators (e.g. Gao) talk to a **dedicated work bot** running as its
own Pi process, with only WorkBench tools and work-scoped memory. The personal
companion bot is never reused as this Host.

Pi (verified on the deployed 1.5.2 runtime) has no built-in event-id lookup or
in-place deferred tool call. These are filled in the work bot's Host layer (Pi
extension + gateway), not in Pi core, WorkBench or Mind Runtime:

| Need | Pi primitive | Host-layer fill |
|---|---|---|
| Session checkpoint / resume | sessions are JSONL; `SessionManager.open(path)` | use as is |
| Idempotent submit by `wake_event_id`, lookup by event | `pi.appendEntry()` (custom entries outside LLM context) | gateway ledger `event_id → session file, status, outcome`; checked before every dispatch; the event id is also appended into the session. Not found after timeout → `unknown`, human reconcile |
| Deferred tool call | `tool_call` hook can block (`block`, `reason`, `terminate`) and read input | hook calls `SqliteTaskService.evaluate_action`; when approval is required it blocks, stores tool name + input fingerprint in `approval_requests`, terminates the run. On `approval_decided` the same session is reopened, the model re-issues the call, and the hook only lets through a call whose fingerprint matches the approved one |
| Auto-mode style guard | same hook | action allowlist from the task's delegation envelope; a classifier can be added later |
| One lane per person, batching | gateway already serialises replies per chat | add a merge window |
| Credentials outside the Agent | tool set is configurable | no generic bash on the work bot; credentials injected only inside specific tools |
