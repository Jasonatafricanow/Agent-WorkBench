# ADR-0003 — Progressive Delegation and Human-in-the-Loop Work

- Status: **PROPOSED (feature branch)**; not a production capability claim
- Date: 2026-11-17
- Applies to: Agent-WorkBench, existing Agent Host, Mind Runtime and MR-Mem
- References: ADR-0002; Anthropic, [Building always-on agents](https://claude.com/resources/articles/building-always-on-agents) (2026-11-13)

## Decision

Agent-WorkBench is not merely a task board or a queue of instructions waiting for the user. It is a **continuously operating work system** in which the human is a first-class participant, but human presence is not a prerequisite for every work cycle.

The system should continue all work already inside its authorized execution envelope while the human is absent. It should create durable, actionable human requests only when a decision, input, review, or physical action is genuinely required. As verified performance accumulates, the human may deliberately delegate more of a recurring work class to the Agent, reducing their role from executor to reviewer and ultimately to exception handler.

This is **Progressive Delegation / Responsibility Transfer**. It is a first-class WorkBench domain, not a prompt convention, global model-confidence score, or MR-Mem inference.

## 1. The always-on work loop

```text
External / human / timer / system event
                |
                v
       WorkBench durable Inbox
                |
                v
   Reconcile business facts and task state
                |
                v
 Evaluate responsibility + work-class policy
                |
      +---------+----------+----------------+
      |                    |                |
      v                    v                v
 Agent can proceed   Human action needed  External condition needed
      |                    |                |
      v                    v                v
 Host Run executes   HumanActionRequest   WAITING / dependency / timer
      |                    |                |
      +---------+----------+----------------+
                |
                v
       Verify outcome with evidence
                |
                v
   Commit task + responsibility events
                |
                v
    Re-evaluate and schedule next step
                |
                +----> next work cycle
```

A Run is short-lived and event-triggered; durable business work survives its absence. Human replies, corrections, approvals, goal changes and explicit overrides are ordinary input events into the same loop. If the human is offline, unrelated authorized work continues. Work that genuinely depends on the human remains pending with a due/escalation policy; the system must never fabricate a decision or silently clear the dependency.

## 2. Separate four independent dimensions

| Dimension | Authority | Meaning |
|---|---|---|
| Task lifecycle | WorkBench SQL | INBOX / READY / DOING / WAITING / BLOCKED / DONE / CANCELLED |
| Attention focus | WorkBench SQL | NOW / NEXT1 / NEXT2; selected attention, not assignment or autonomy |
| Responsibility & delegation | WorkBench SQL | Who is accountable, who executes next, work class, autonomy level, allowed actions, approval gates and escalation |
| Agent Run / tool call | Existing Agent Host | Session, Run, checkpoint, actual tool authorization and side effects |

Do not overload task status or NOW/NEXT to express autonomy. A task may be WAITING for a customer while an unrelated task is autonomously progressing. A task may be assigned to a human while its Agent prepares research. A completed Host Run does not complete the business task.

## 3. Responsibility and autonomy are per work class

Autonomy is never a global rating for an Agent or user. A principal may be trusted to draft routine reports but not to send external legal commitments, move money, change official deadlines or submit a filing.

Each workspace defines versioned `delegation_profiles` for work classes, for example:
`data_retrieval`, `document_drafting`, `routine_followup`,
`internal_reconciliation`, `external_communication`,
`financial_action`, `legal_submission`, `official_deadline_change`.

A profile defines:
- maximum autonomy level approved by a human owner;
- action allowlist, reversible-action subset and mandatory post-execution review actions;
- mandatory approval conditions;
- success criteria and required evidence;
- escalation and attention policy;
- promotion thresholds and whether automatic promotion is explicitly pre-authorized.

Each task has an effective `task_responsibilities` record. Its action set and autonomy may be narrower than its profile, never broader. A policy change creates a new version; the current task responsibility references the version used for its decision. The business accountable owner remains explicit (the existing `tasks.assignee_id`); the current executor is separate (`task_responsibilities.current_executor_id`).

## 4. Five delegation stages

| Level | Name | Agent may do | Human role |
|---|---|---|---|
| 0 | `human_executes` | Explain, research or advise only; no execution | Performs the work |
| 1 | `agent_prepares` | Prepare draft, data, checklist or proposed action | Reviews and commits/performs |
| 2 | `agent_supervised` | Execute approved reversible/internal steps | Reviews before consequential or irreversible commit |
| 3 | `agent_bounded` | Execute an explicit task-level allowlist within policy | Reviews configured actions; approves risk triggers and exceptions |
| 4 | `agent_exception_only` | Handle routine class work inside the approved class envelope | Handles exceptions, changes goals/policy and overrides |

Level 4 is not unrestricted autonomy. It remains bounded by the profile, action allowlist, approval conditions, evidence requirements, Host tool policy and human override. Promotion from level 3 to 4 must not expand to a new high-risk action class unless that action is separately authorized.

## 5. What may trigger human participation

Human work is not limited to approving deferred tool calls. There are two distinct durable objects:

- `human_action_requests`: request a human to provide missing information, make a business decision, review a deliverable, or physically perform a task. These appear in the decision/action inbox and have assignee, due time, urgency, status and response evidence.
- `approval_requests`: a narrow approval for a specific Host-deferred tool call. The Host owns the suspended Session and rechecks tool permissions at resume time. Approval does not create a general tool grant.

Human input creates a new authenticated WorkBench event and re-enters the work loop. Use `batch` for routine decisions, `notify` for time-sensitive requests, and `interrupt` only for explicitly configured urgent/high-risk exceptions. The human should not be interrupted for work the Agent can safely finish.

## 6. Evidence-based delegation progression

A successful Agent message or a model's confidence score is not evidence of competence. Evidence may include verified tool receipts, accepted deliverables, human acceptance without rework, independent source checks, correction/rework rates, missed deadlines and policy violations.

Promotion rules:
1. The owner explicitly approves a versioned work-class profile and its maximum level.
2. The profile states measurable evidence thresholds (minimum verified runs, acceptance ratio, maximum rework and policy violations).
3. The system may recommend promotion when evidence meets the gate.
4. Default: human approves the promotion. Automatic promotion is allowed only when the owner explicitly enables it in that profile, and only up to its already approved ceiling.
5. No memory, inference, successful run count or agent identity can grant new tool permissions.

Demotion / pause:
- Human override or revocation takes effect immediately for future dispatches.
- A policy breach, stale source, repeated rework or evidence failure can trigger a configured downgrade or pause.
- A downgrade is recorded with cause and evidence; it must not erase previous history.
- The Host independently revalidates tool permissions on every execution and resume.

## 7. WorkBench data model

The feature branch adds these first-class tables:
- `delegation_profiles`: versioned per-work-class policy and approved autonomy ceiling.
- `task_responsibilities`: current executor, effective autonomy, narrower action envelope and policy version for a task.
- `responsibility_events`: append-only assignment, delegation, promotion, demotion, takeover, override and revocation history with evidence references.
- `human_action_requests`: durable human input/decision/review/execution work, separate from Host tool approval.

The `workdesk.delegation` module is a pure policy evaluator for action routing and promotion assessment. It does not write SQL or authorize tools. The future TaskService must atomically write task changes, responsibility changes, responsibility events and required outbox rows.

## 8. How this relates to the ten useful workbench features

| Feature | Responsibility-transfer contribution | Implementation home |
|---|---|---|
| Two-way commitments | Distinguish who owes whom; transfer execution without losing accountable owner | WorkBench commitment/task domain |
| Deliverable lifecycle | Agent prepares/submits; human or recipient accepts; evidence gates DONE | WorkBench deliverables + evidence |
| Work resumption | Resume the work item and point to Host checkpoint; never copy Session authority | WorkBench bookmark + Host checkpoint |
| Incremental change summary | Summarize events since each participant/Agent's cursor | WorkBench event query |
| External change monitoring | A changed dependency re-evaluates the responsible actor and next step | Watch adapter → WorkBench Inbox |
| Recurring work templates | New occurrences inherit only the approved class profile and task-specific envelope | WorkBench recurrence + delegation |
| Decision inbox | Route only genuine human actions, decisions and approvals | HumanActionRequest + approval request |
| Multi-Agent handoff | Transfer current executor with versioned responsibility event and source refs | WorkBench responsibility + Host Run |
| Capacity/conflict detection | Expose workload and human-attention demand before promising dates | WorkBench projections; no autonomous priority override |
| Stagnation review | Trigger review/demotion/owner attention from verified event history | WorkBench timers + responsibility evidence |

## 9. Cross-project ownership

- **Agent-WorkBench** owns business goal, accountable owner, current executor, work class, delegation profile, autonomy level, human requests, approval records, due dates, commitments, deliverables and task truth.
- **Agent Host** owns Run/Session/checkpoints, actual tool capabilities, credentials, execution policy and external side effects. It rechecks permission at every call and resume.
- **Mind Runtime** may interpret context, user intent, relationships and propose what needs attention. It may produce cognitive WakeSignal or recommendations, but cannot set WorkBench autonomy, approve itself, or grant tool permission. The existing MR architecture lock remains authoritative.
- **MR-Mem** stores evidence-backed semantic experience and preferences. It can inform a recommendation but cannot grant permission, promote autonomy, decide business ownership or replace WorkBench SQL.
- **Human** sets goals and boundaries, approves policy ceilings, provides judgment where required, reviews exceptions and can override/revoke at any time. Routine work should not wait for the human once it is within the approved envelope.

## 10. Explicit non-goals and safety rules

- Do not create a second Agent Loop, Session store, generic planner or tool runner inside WorkBench.
- Do not let the model change its own autonomy level, allowed actions, profile ceiling or approval requirements.
- Do not infer consent or delegation from a casual “okay” unless the scope/action is explicit and authenticated.
- Do not automatically promote from semantic memories or unverified claims.
- Do not equate `approval_requests.approved` with a permanent capability grant.
- Do not block independent tasks merely because a human request is pending.
- Do not claim that software assumes legal or professional accountability; the human/business owner remains accountable for non-delegable decisions.

## 11. Acceptance gates

- The same task class can be at different autonomy levels in different workspaces; no global Agent score exists.
- A task cannot exceed the active approved profile ceiling or broaden its action set.
- Level 0/1/2/3/4 routing matches the table above.
- An approval condition always routes to a human regardless of current level.
- Promotion without verified evidence fails; default promotion requires human approval.
- Auto-promotion works only when explicitly enabled in an owner-approved profile and never exceeds its ceiling.
- Revocation/override immediately blocks future dispatch; Host checks tool permission again at resume.
- Human requests remain durable while the human is offline; unrelated authorized work continues.
- Human response re-enters the same event loop and reevaluates the task.
- Responsibility changes are append-only auditable events and can be reconstructed after restart.
