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