# MR-Mem 接入合同：不破坏 Sidecar 边界

依据：MR-Mem `main` 的 `docs/ARCHITECTURE.md`、`docs/ADR-0001-SIDECAR-MEMORY-BOUNDARY.md`、`docs/PATH_A.md`（2026-11-16 检查）。本文件提出工作台宿主侧适配合同；**没有声称已经接通实际 MR-Mem 服务**。

## 权威划分

- `workdesk.tasks`、`task_events`、`reminders`、`dependencies`：**业务事实权威**，在 Workbench 的 SQL 中。任何 Agent 必须先通过 workdesk.read/overview 读即时任务状态。
- 原始 Telegram 消息/文件/工具结果：归属宿主 Agent 的原文存储，`inbox_messages.raw_source_ref` 只保存可回查的引用。必须保证宿主可对 SourceRef 按当前权限回查。
- MR-Mem：对宿主给出的已接受 SemanticBlock 准入，维护 Thread/LCE 与检索。它的结果有 support 和源文回溯，但**不是任务字段的写入权威**。
- `memory_outbox`：Workbench 的单向通知队列，只是需要宿主消费的事实变更信号。它不直接写 Canonical，不应强制把每个 task_event 变成长期记忆。

## 读链

```
Agent 用户请求
 -> 认证主体 + 权限范围
 -> workdesk.read / overview (SQL 当前状态/version)
 -> 如需历史语义，宿主向 MR-Mem 检索同一授权 scope
 -> 有需要时透过 SourceRefReader 回查原文/任务 event
 -> Agent 形成回答，引用事实来源
```

禁止只用 MR-Mem 中旧的语义摘要认定当前 task 为 DONE/WAITING。对外用户的“任务不存在”和“无权限看”返回一致；禁止把内部任务 ID 作为检索越权凭据。

## 写链

```
Telegram/其他 Host 输入 -> 宿主原文存储 + stable SourceRef
 -> Host Semantic Authority 判断意图
 -> workdesk.apply (校验 + 事务提交 task_snapshot/event/memory_outbox)
 -> receipt -> 对人确认
 -> memory_outbox worker 按 event id 消费
 -> Host 编译器决定 KEEP/DEFER/DROP
 -> 需要时通过 SemanticAdmissionService.admit 准入
 -> Thread / LCE Path A 原有处理流程
```

- `task_event_id` 是编译流水线的稳定幂等键；`workdesk://{workspace}/tasks/{task}/events/{event}` 是业务 SourceRef 的候选 source namespace，与 Telegram 消息 SourceRef 不混同。实际映射须遵循 MR-Mem 已存在的 `SourceRefReader` 实际类型要求，不能直接把 URI 字符串当作该 Python 类型。
- 事件如何转化为 SemanticMemoryCandidate 由 Host 的 Semantic Authority 决定，而非在 Workbench 新增第二个理解模型。
- task 修改后在 outbox `pending`，MR-Mem 停机不回滚工作台业务提交。恢复后按 event_id 重放并使用既有 admission 幂等机制。
- task_event 发生订正或权限撤销时，宿主明确计算 affected SourceRefs 并调用 Canonical 的源失效接口；业务事件本身留审计轨迹，权限变化要使检索层立即拒绝，不可等待重编译。

## 多 Agent 同步协议

1. 所有 Agent 都先查询 Workbench SQL 当前 task + version；跨 Agent 的即时一致性由单写 SQL 事务保障，**不是靠 MR-Mem 的语义投影传播**。
2. 写入使用 `expected_version` 乐观锁，重试只能用相同 `idempotency_key`+完全相同 payload；冲突必须重新读取并重新判断。
3. 需要将事实提供给后续对话时，MR-Mem 提供背景检索；Agent 回答须同时检验最新 SQL 快照。
4. 外部请求者每个工具调用都经服务器 ACL 检查，记忆检索也须 scope+SourceRef 再检查。向量检索候选不能成为绕过 row-level policy 的旁路。
5. 对第三方说法（如“他说已经完成”）标记 `reported_third_party`，不自动把执行事实置为 DONE。

## 接线施工清单

- [ ] 锁定 MR-Mem 生产 commit 和 Host 实际依赖接口，不取 AML 适配层代替。
- [ ] 明确 Telegram 宿主原文保存/编辑/撤回策略，SourceRef 稳定性与版本号。
- [ ] 编写 `WorkbenchSourceRefAdapter`：解析 `task_events`，过滤权限，返回确切当前版本。
- [ ] 编写 `WorkbenchMemoryOutboxWorker`：原子领取、按 event id 幂等、重试与 dead-letter。
- [ ] 编写 `WorkbenchContextAssembler`：先当前任务，再历史语义及证据，并在回答中分清“事实”和“推断”。
- [ ] 用合成跨 Agent 历史验证：事件重复、宿主修订、检索时撤权、LCE 落后、MR-Mem 离线。

**明确未实现**：本参考工程当前只有 `memory_outbox` 事务写入与领取/确认 API；没有真实 `SemanticAdmissionService.admit`/`PathAReader` 适配、语义编译器或运行时端点。这些必须在实际 Host 接线阶段完成并测试。

## Always-on event and semantic admission policy (2026-11-16)

Reference: [ADR-0002](ADR-0002-ALWAYS-ON-OWNERSHIP.md).
`wake_events` is a **business-to-Host dispatch ledger**, not an MR-Mem
admission feed. WorkBench events such as `timer_fired`, `host_run_completed`,
`approval_sent` or delivery receipts are operational data. They must not
automatically create a SemanticBlock that claims a person has a durable
preference, commitment, changed emotion or relationship.

Only selected source-backed `task_events` may become semantic candidates,
under the Host's existing Semantic Authority. User speech, authenticated
human approval and independent real-world evidence are distinct from Agent
output or external-tool success. MR-Mem's `SourceRefReader` must validate
current source revision, workspace and **the particular task/event visibility**
at each read. A grant is rechecked after context bind; a shared workspace
does not authorize one user to read all another user's evidence.

A Host Session/Run never enters MR-Mem as a second truth store. Host owns
deferred calls and checkpoint/resume. SQL task facts, business reminders and
`wake_events` remain queryable even if memory projection is delayed.

**Status correction:** `memory_outbox` currently exists as an SQL table,
but a production memory-outbox writer/consumer with lease/retry and real
`SemanticAdmissionService` integration has **not** been implemented in
this remote WorkBench branch. Likewise, no actual Agent Host Run port has
been connected. Existing `runtime_bridge` tests cover only the local
event ledger and contract invariants.


## Responsibility-transfer boundary

MR-Mem can supply historical experience and user preferences that help the
Agent understand how to perform a work class. It MUST NOT infer, grant, widen,
promote or persist WorkBench execution authority. Delegation profiles,
task-level action envelopes, human approvals and revocation are owned by
WorkBench SQL; the Agent Host separately checks its tool policy at execution
and resume time. Memory retrieval is context, never a capability.

## Owner decision (2026-11-17): memory is written by the handling Agent at wrap-up

The Agent that handled the turn already understood it. As the last step of the
turn it writes the memory itself through MR-Mem `write` (like a session handoff
note), with the source pointing at the task_event or original message. There is
**no separate online KEEP/DEFER/DROP pass** over task events ("what was already
reasoned is not reasoned again"). `memory_outbox` is only a pointer queue: the
Host enqueues a pointer when MR-Mem is unavailable so the write can be retried,
or for later offline curation. `WorkbenchEventSourceReader` stays inside MR-Mem
for permission-checked source lookup; Agents get MR-Mem `write` and `search`
only, never a separate read tool. `SqliteTaskService` therefore does not write
`memory_outbox` rows itself.
