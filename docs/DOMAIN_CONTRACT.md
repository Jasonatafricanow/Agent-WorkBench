# 领域合同与接口 v1.0

## 1. 数据对象

| 对象 | 关键字段/约束 | 作用 |
|---|---|---|
| Principal / Identity | principal_id、kind、provider、external_user_id | 用户/Agent 的稳定身份和 TG 身份映射 |
| Workspace / Membership | owner、member_role、scope | 隔离私人和共享工作；授权不是昵称匹配 |
| Project | id、goal、phase、status | 长期目标/持续工作容器；允许无项目的独立任务 |
| Task | title、goal、next_action、status、assignee、priority、version | 当前“做什么、做到哪里、下一步是什么” |
| TaskEvent | event_type、actor、before/after、reason、occurred_at、recorded_at、source_ref | 不可覆盖的事实和进展/变更历史 |
| TaskFocus | workspace、NOW/NEXT1/NEXT2、task | 人工确定的当前注意力位置；不是任务状态 |
| Dependency | awaited_outcome、owner、related_task、resolved、next_check | 等谁、等什么、何时恢复；被满足的证据 |
| Reminder | due_at、recipient、status、lease、attempt、policy | 时间驱动的跟进、到期提醒与重试 |
| InboxMessage / MessageLink | platform/bot/update/chat/message ID、source_ref、processing | 入站消息去重、接单闭环、任务来源 |
| DeliveryOutbox | destination、payload_ref、status、provider_message_id | 给人发消息的状态和回执 |
| MemoryOutbox | event_id、scope、source_ref、status、retry | 向 MR-Mem 异步提交语义线索 |
| CommandReceipt | idempotency_key、payload_hash、result | Agent 重试不会生成重复任务/事件 |

时间全部存 UTC ISO-8601（含 UTC 标识），显示时按 Principal 的 IANA 时区。官方截止、软截止、计划启动、下次跟进、发生时间是独立字段。日粒度截止明确本地日与截止时间；缺少时区/时间时，不默认为当地午夜，不编造官方期限。

| DelegationProfile | workspace、work_class、version、max_autonomy_level、action/reversible/review allowlist、approval conditions、evidence/promotion policy | 每类工作经 owner 批准的自主权上限 |
| TaskResponsibility | current_executor、profile/version、autonomy_level、有效动作范围、success/evidence/escalation | 每个任务由谁推进，以及 Agent 可做到什么程度 |
| ResponsibilityEvent | from/to actor、from/to level、event、reason、evidence、source_ref | 委派、升级、降级、人工接管和撤权的审计历史 |
| HumanActionRequest | assignee、input/decision/review/execution、due、urgency、response | 人类作为循环中的执行者/决策者，而不只是工具审批人 |

## 2. 任务状态机

## 2. 任务状态机

核心状态：

```text
INBOX --[信息齐备/受理]--> READY --[开始]--> DOING --[交付待答复]--> WAITING
  |                              |   |                         |      |
  |                              |   +--> BLOCKED <-------------+      |
  |                              |               |                    |
  |                              +<--[恢复]-------+----[条件满足]-------+
  |                              +--------------> READY (暂停/重新排队)
  +--[拒绝/取消]-----------------> CANCELLED
READY / DOING / WAITING / BLOCKED --> DONE (核实完成)
DONE --[明确重新打开]--> READY
```

`INBOX` 尚待受理/澄清；`READY` 已可安排但未在执行；`DOING` 正在实施；`WAITING` 已完成本方动作，等待外部反馈/到期信号；`BLOCKED` 因依赖/条件而不能推进；`DONE` 目标已经满足且有证据；`CANCELLED` 终止不再追求该目标。

WAITING 与 BLOCKED 有差别：等待反馈本身是一项可追踪的承诺，允许催促；阻塞可来自权限、设备、预算、前置任务等。**NOW / NEXT 不是持久状态，而是独立的 Focus 位置**。一个 NEXT 任务可以是 READY，NOW 可 DOING；WAITING/BLOCKED 默认不占 NOW/NEXT，除非 owner 明确指定要处理其催促/解阻动作。Focus 修改必须有事件记录且 owner 优先权最高。最多一个 NOW，最多两个 NEXT，可为空；不根据所有任务的 priority 自动覆盖 owner 锁定的槽位。

所有状态变更应支持 `reason`、`evidence_type`、`source_ref`、`occurred_at`；完成/取消/重开必须有明确证据或经授权确认。变更 `official_due` 需要保留原值和来源，不自动覆盖官方日期；`soft_due` 可由规则建议但要标明计算依据与人是否接受。多个官方里程碑宜作为 project milestones/子任务，不强塞单一日期。

## 3. 计划与延误

任务统一支持：`official_due_at`、`soft_due_at`、`planned_start_at`、`completed_at`，并在日历动作中维护 `follow_up_at`（更准确的实现放在 Reminder/Dependency）。进度不要滥用百分比；`done_summary` 记录已做事项，`next_action` 记录可执行的下一步；日期变更和延期理由要写 task_event。延期原因类型 `external_wait | reprioritized | underestimated | blocked | unavailable | scope_changed | other`，同时保留原话摘要，切忌模型擅自归因。

例：“先不做了，等高老师回复；周三没反馈提醒我” => 任务可能变成 WAITING，创建 `external_response` 依赖，将 follow_up 安排为该用户时区的指定周三；如果“周三”有歧义，按消息发生时间的下一次周三提出具体日期，并在确认后生效。若完成通知需要发送给高老师，必须确认消息已被外部传输层接受，不能因为模型说“已发送”就置为完成。

## 4. 最小 Agent 工具面

为了降低 Agent 工具学习负担，只公开三个逻辑工具。所有写操作经过相同的服务端 policy：

**`workdesk.read`**：参数 `workspace_id, task_id? / query?, filters?, include_events?, limit?`；返回当前任务版本、授权后的视图、引用与可能的歧义候选。对人名/项目名搜索返回候选，不直接做写入定位。

**`workdesk.apply`**：参数 `workspace_id, idempotency_key, operations[]`，每项为 `create_task | patch_task | transition | append_progress | set_focus | add_dependency | resolve_dependency | set_reminder | cancel_reminder | link_source | mark_deliverable`，并带 `task_id?, expected_version?, fields?, reason?, evidence_type?, source_ref?`。服务端在同一事务内预检所有操作的访问权限、状态变更、版本、业务不变量；失败不部分提交。禁止开放任意 SQL 或任意字段更新。

**`workdesk.overview`**：返回 NOW、两个 NEXT、WAITING、BLOCKED、到期和最近事件聚合，确保 Dashboard 与 Agent 使用同一个读模型。

**原则**：工具的 `actor` 来自服务端认证上下文，不能由 LLM 自行传入。Agent 面向用户可以解释“我打算调整什么”，但执行后只能根据 API receipt 声称成功。外部系统副作用有独立许可和回执。

参考契约（非已部署 API）：

```json
{
  "tool": "workdesk.apply",
  "workspace_id": "ws-personal",
  "idempotency_key": "tg:bot-A:chat-55:message-391:extract-v1",
  "operations": [
    {"op":"transition","task_id":"task-123","expected_version":5,
     "to":"WAITING","reason":"初稿已交，等待高老师反馈",
     "evidence_type":"explicit_user","source_ref":"tg://bot-A/55/391"},
    {"op":"add_dependency","task_id":"task-123","kind":"external_response",
     "awaited_outcome":"高老师反馈初稿","responsible_principal_id":"principal-gao"},
    {"op":"set_reminder","task_id":"task-123","local_at":"2026-11-18T10:00:00",
     "timezone":"Asia/Shanghai","recipient_principal_id":"principal-owner",
     "condition":"if_still_waiting","action":"remind_owner_to_follow_up"}
  ]
}
```

对外 Agent 不开放任意 `set_focus`/修改 owner 私人任务的权能；高老师请求只允许创建属于可见 shared 范围的任务或查询自己有权访问的状态。

## 4.1 责任与渐进式自主权

`tasks.assignee_id` 是业务结果的 accountable owner；`task_responsibilities.current_executor_id` 是当前下一步执行者。两者不能合并：人可以仍对结果负责，而 Agent 已获授权执行日常步骤。

工作类别（例如 data retrieval、document drafting、routine follow-up、external communication、financial action、legal submission）由 workspace 的版本化 DelegationProfile 管理。每个 profile 定义最高等级、允许动作、可逆动作、强制事后审查动作、强制事前审批条件、成功标准、证据要求、升级策略和 promotion gate。TaskResponsibility 可以进一步缩小 profile 的范围，不允许扩大。策略更新创建新版本，旧事件保留原 policy version。

| Level | 语义 | 人的参与 |
|---|---|---|
| 0 human_executes | Agent 只解释/建议 | 人实际执行 |
| 1 agent_prepares | Agent 准备草稿、数据和步骤 | 人审查并提交 |
| 2 agent_supervised | Agent 执行允许的可逆内部步骤 | 人在关键/不可逆提交前审查 |
| 3 agent_bounded | Agent 在任务级动作白名单内执行 | 触发审批条件时介入 |
| 4 agent_exception_only | Agent 在工作类别批准范围内处理常规工作 | 人处理例外、目标和策略变更 |

普通人类工作进入 `human_action_requests`（输入、决策、审查、实际执行、异常处理）；对 Host 挂起工具调用的许可仍使用 `approval_requests`。两者的 UI 可以汇总到一个 Human Inbox，但领域对象和权限语义必须分开。

升级只基于 verified runs、交付验收、返工率、策略违规及来源证据。默认升级需人批准；仅当 owner 对某个 work class 显式开启 `auto_promotion_enabled` 时，系统才能在既定 max level 内自动升级。模型信心、语义记忆或 Agent 自己的声明不能改变授权。人可立即接管、降级、暂停和撤销；Host 每次调用/续跑仍重新验证工具权限。

## 5. 入站处理与“完成”定义

## 5. 入站处理与“完成”定义

```text
Telegram update -> verify bot/secret/user/chat -> durable inbox receipt
 -> resolve identity + policy + host raw SourceRef
 -> dedupe/update existing task candidate
 -> classify intent: consult / autonomous action / human action / clarify / wait
 -> authorized tools (read/apply) -> API receipt
 -> reply with accurate result (not premature success)
 -> record outbound delivery result
```

答复咨询 ≠ 完成承诺：如果用户提出交付物和截止日，即便 Agent 在消息里给出建议，也必须追踪交付责任。自动执行须有工具侧完成证据；需要你处理的写入任务池；信息不足应先澄清（可保留 INBOX），不能凭空生成 deadline、assignee 或不存在的依赖。同一 Telegram 消息多个独立目标时可拆分任务，保留同一 message_link；相反，补充原任务不能再造重复任务。

## 6. Scheduler 的具体语义

每个 reminder 维护 `scheduled_for`、`condition`、`recipient`、`channel`、`status`、`attempts`、`lease_until`、`dedupe_key`。cron/定时 worker 根据 UTC 领取 due 项；在领取和发送前确认任务状态与依赖仍满足触发条件。`if_still_waiting` 在已解决/取消时关闭而不发送。默认向 owner 提醒“应该跟进谁”，**不在缺少明确授权时主动向高老师发送催促**。用户可逐项授权自动催促、提醒窗口、重复上限和静默时段；静默期推迟但不清除提醒。

- 当天截止、提前 N 天、soft_due 到期、等待外部反馈逾期为不同提醒种类；避免一个日期重复轰炸。
- 完成任务/依赖解决，取消相关提醒或重新计算下一轮；不能保留僵尸提醒。
- 发送前形成 outbox 消息，调用成功记录 `provider_message_id`；超时后无法判定时标 `unknown`，制定人工核验或保守补偿策略；不虚报“已送达/对方已读”。
- 支持一次性提醒和明确设定的周期跟进（例如每周三直到收到反馈），周期要有最大次数和停机条件。

## 7. 前端只读投影

首页：NOW 1、NEXT 2、WAITING、BLOCKED、最近已完成、今日/本周到期、等待催促、任务池摘要；任务详情：总目标、任务链、下一步、软/官方截止、责任人、来源引用、事件时间线和延期理由；项目视图：长期目标与子任务；过滤：项目、请求人、状态、标签、优先级、日期、逾期。页面所有信息来自授权 API，不能自建事实源或在客户端推断最终状态。

## 8. 决策与审批策略

立即执行：受权 scope 内“查询当前状态、提炼任务建议、追加无风险的事实报告、设置自我提醒（在已明确时间时）”。

需确认：身份/任务匹配歧义；完成与取消缺少证据；变更官方截止日；从多人共享任务转私人/反向；发送外部正式成果；跨系统产生金钱/业务法律后果；批量覆盖任务。Agent 的“建议优先级”不得覆盖手工 focus。

拒绝：外部用户访问私有任务；无授权外发私有内容；从消息自然语言直接授予权限；越权调用工具；模型臆测已完成或到期。