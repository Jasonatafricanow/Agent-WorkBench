# Developer handoff — Progressive Delegation is a core requirement

**Read first:** [ADR-0003](ADR-0003-PROGRESSIVE-DELEGATION.md), then
[ADR-0002](ADR-0002-ALWAYS-ON-OWNERSHIP.md) and
[Project Blueprint](PROJECT_BLUEPRINT.md).

## Product intent

Build a continuously operating work system in which the human is a first-class
participant, not a mandatory scheduler or the only worker. The system keeps
moving independently inside an explicitly approved envelope while the human
is away. It asks for human input only when a genuine judgment, approval,
review or real-world action is required. Over time, recurring work should
move from human execution to Agent execution, with the human moving toward
exception handling.

This is not "make the Agent globally more autonomous". Delegation is per
workspace + work class + task + action. A routine report profile does not
authorize legal submission, money movement, new external recipients or
official deadline changes.

## What is now in the feature branch

- SQL schema: versioned `delegation_profiles`, current `task_responsibilities`,
  append-only `responsibility_events`, durable `human_action_requests`.
- Pure policy evaluator: `workdesk.delegation` with five stages, action
  routing, evidence-gated promotion assessment and explicit auto-promotion
  opt-in.
- New wake event kinds for human action completion, delegation review and
  responsibility change.
- ADR-0003 and updates to blueprint, domain contract, implementation plan,
  acceptance gates, cross-repo matrix, Host protocol, MR-Mem boundary and README.

These are design/schema/domain-contract foundations. They do **not** mean
TaskService, Telegram, Host dispatch, or production autonomy is wired.

## Mandatory implementation order

1. **Implement TaskService first**: one transaction must update task snapshot,
   append TaskEvent, update responsibility when applicable, append
   ResponsibilityEvent, create HumanActionRequest/Reminder and required
   outbox rows. Add idempotency and optimistic version checks.
2. **Implement profile administration**: owner-only create/version/approve/
   retire. No LLM tool may set `max_autonomy_level`, `allowed_actions`,
   approval conditions or auto-promotion flags.
3. **Implement task responsibility commands**: assign, delegate, propose
   promotion, approve promotion, demote, take over, pause and revoke. Every
   command records actor, reason, policy version and evidence references.
4. **Implement Human Action Inbox**: read/respond/decline/complete/expire;
   authenticate the human via Host transport; response becomes a new
   WorkBench event and re-evaluates the task. Keep ordinary human actions
   separate from Host-deferred `approval_requests`.
5. **Implement the dispatcher decision**: evaluate the current task
   responsibility and profile on every dispatch. Human override/revoke must
   block future dispatch. A previously created Host Run is not a permission
   to perform a new action.
6. **Host integration**: Host owns Session/Run/checkpoint and actual tool
   permission. It revalidates on every call and resume. No WorkBench bearer
   token or permission shortcut.
7. **Evidence pipeline**: capture verified output receipts, recipient/user
   acceptance, rework/correction and policy violations. Agent confidence or
   self-reported completion is not promotion evidence.
8. **Notifications and human attention**: batch routine questions; notify
   time-sensitive work; interrupt only for explicitly configured critical
   conditions. Human absence must not block independent authorized work.
9. **Add integration tests**: offline human, task class-specific profiles,
   action allowlist, profile ceiling, mandatory review/approval conditions, revoked profile,
   auto-promotion opt-in, demotion, multi-Agent concurrency, Host restart and
   deferred-call resume.

## Non-negotiable boundaries

- WorkBench owns business responsibility and delegation policy.
- Existing Agent Host owns Run/Session, checkpoints, credentials, tool policy
  and side effects.
- Mind Runtime can interpret and recommend attention; it cannot grant
  execution authority or approve its own proposal.
- MR-Mem can supply evidence-backed context; it cannot grant or promote
  autonomy.
- Human retains goals, policy ceiling, override/revoke and non-delegable
  accountability.
- Task state, NOW/NEXT, responsibility, and Host Run status are four
  independent dimensions.

## First PR target

Deliver a working `ResponsibilityService` and `HumanActionService` on top of
the new schema, with transactional audit and negative authorization tests.
Do not start the dashboard or broad automation before these contracts work.
Do not merge or deploy until P0 acceptance gates pass.


## Latest branch verification

WorkBench PR #1 currently has **46 offline tests passing** on the
progressive-delegation schema/policy commit. The tests validate schema
constraints and pure policy routing only. They are not evidence that
ResponsibilityService, HumanActionService, TaskService, Telegram or Host
dispatch is live. Keep the PR Draft until the real services and P0 end-to-end
gates are implemented.
