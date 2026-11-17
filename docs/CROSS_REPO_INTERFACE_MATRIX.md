# 三仓协作协议矩阵 · v0.1（供审查，不是已接通的网络 API）

## 1. 统一命名

| 术语 | 唯一定义 | 来源 |
|---|---|---|
| Business Event | 受信外部事件、计时器或审批决定，经 WorkBench 规范化形成的持久事实 | WorkBench |
| WakeEvent | 可交付 Host 的业务事件引用；不是模型命令，也不是 MR WakeSignal | WorkBench |
| Agent Run | Host 管理的一次执行实例与恢复状态 | Agent Host |
| Deferred Call | Host 保存、可被授权回复恢复的工具调用 | Agent Host |
| Cognitive WakeSignal | MR 的认知意图/动机信号 | Mind Runtime |
| SourceRef | MR-Mem 指向宿主当前原始权威证据的版本化引用 | MR-Mem ↔ Host |
| TaskEvent | WorkBench 的业务变化证据记录 | WorkBench |
| Operational Receipt | 执行/投递确认，不等价用户体验或商业验收 | Host / WorkBench |

## 2. 写入口与读入口的契约

| 源→目的 | 请求 | 回执 | 拒绝与恢复 |
|---|---|---|---|
| Telegram/外部→WorkBench | 已认证消息来源、update_id、workspace、时间 | inbox ID / dedupe | 无 ACL 拒绝；重复 update 不建第二个任务 |
| WorkBench→Host | 稳定 WakeEvent + source_ref + event_id | opaque run_ref/session_ref/必要的 deferred_ref | 超时 unknown，按 event_id 查询 Host；禁止盲目重启 |
| Host→WorkBench | 固定的 ActorContext 下 read/overview/apply | Task 版本、事件和命令 receipt | 版本冲突重新读；权限不足 fail-closed |
| WorkBench→MR-Mem | 授权/源版本校验通过的候选语义 + SourceRef | canonical memory ID/admission receipt | 编译延迟可重试；不阻塞 SQL |
| MR→Host | 认知上下文、倾向、WakeSignal | Host 选择采纳/忽略的操作反馈 | 不提升为工具执行权限 |
| Host→WorkBench | Run 状态与来源引用 | WorkBench 中的引用记录 | Run completed 不自动 task DONE |

接口由拥有者独立版本化。模型自然语言只产生**待验证提案**；授权、官方 due、发消息和工具调用由受信服务执行。

## 3. 三类原子性和时间语义

- 本地 SQL：Task 快照、TaskEvent、command receipt、outbox 在一个事务；timer next due 和 WakeEvent 在一个事务。
- 远端 Host/Telegram/MR-Mem：不存在与 SQL 横跨三方的 ACID。靠 inbox/outbox、稳定 ID、租约、回执和 unknown/reconcile 处理失败。
- 发生时间 `occurred_at` ≠ WorkBench 记录时间 `recorded_at` ≠ 语义认知提交时间 `known_at`。保留各自来源；不以墙钟回填虚构事实。
- 官方 due、内部软截止、下次跟进、Host Run 截止与审批有效期是不同字段。日期无具体时间时保留当地日期及 IANA timezone。
- 两个 Agent 共享任务业务版本，不共享未授权的 User 记忆 Scope。

## 4. 需要实际 Host 确认的能力

WorkBench 不得假设当前 Hermes/其他宿主具有：
1. 按业务 event_id 幂等提交 Run；
2. 按 event_id 查询已存在 Run 和最终状态；
3. Deferred Tool Call 跨进程持久化与续跑；
4. 对每次续跑重新计算工具实际权限；
5. 原始 Telegram 记录的稳定身份、编辑/撤回版本；
6. 可用于权限检查的 Agent 身份映射。

在 Host 能力盘点前，只能提交协议与离线 ledger，不打开自动派发或外部自动催促。

## 4.1 Responsibility / Human Participation contract

| Contract | Owner | Consumer | Boundary |
|---|---|---|---|
| DelegationProfile + TaskResponsibility | WorkBench SQL | Existing Host | Versioned work-class ceiling and task-level narrower envelope; not a Host tool token |
| HumanActionRequest | WorkBench SQL | Human via Telegram/dashboard; Host after response | Human input/decision/review/execution is a durable business action |
| ApprovalRequest | WorkBench SQL | Existing Host | Specific deferred tool call only; Host resumes and rechecks permission |
| ResponsibilityEvent | WorkBench SQL | Agents, audit and incremental summary | Append-only delegation/takeover/promotion/demotion evidence |
| Cognitive recommendation | Mind Runtime | Existing Host / WorkBench policy service | Advisory only; cannot change delegation or authorize tools |
| Semantic preference/experience | MR-Mem | Mind Runtime / Host | Context only; cannot promote autonomy or grant a permission |

Human responses are authenticated input events. The WorkBench service evaluates the current responsibility contract; the Host separately validates actual tool capability. Human absence does not stop authorized independent work.

## 5. 跨仓验收条件

## 5. 跨仓验收条件

- WorkBench event 重复投递不会得到两次现实执行；未知状态等待 Host reconcile；
- MR-Mem 读权限撤销后旧 context 无权 hydration；SourceRef 失效不泄漏旧原文；
- Host Run 完成不修改 WorkBench 任务 DONE；
- MR 认知信号不能绕过 Host 的任何安全策略；
- 业务 WAITING 与 Host deferred 完全独立，审批回复有明确来源且需重新授权；
- 读取来源错工作空间、对象错权限、版本过期都拒绝；
- 本地 SQL 提交成功、MR-Mem 投影失败可以恢复而不重复推进任务。

参见 [项目总体蓝图](PROJECT_BLUEPRINT.md)、[ADR-0002](ADR-0002-ALWAYS-ON-OWNERSHIP.md) 与 [验收规格](ACCEPTANCE.md)。
