# 正式实施方案与施工顺序

这是**实施顺序/工程完成条件**，不是重新做 MVP 或让用户再验证需求。通过下面各阶段的客观测试后才可称为上线。

## Phase 0 — 现状盘点与版本锁定

- 固定项目为独立仓库 `agent-workbench`；MR-Mem/主体 MR 作为外部依赖，**不要新业务代码进入 MR-Mem 内核**。
- 核查 NAS 实际部署环境、容器卷、备份方式、Tailscale 入口和目前承载 Telegram 的 Agent Runtime；锁定可用 Python、MR-Mem commit、真实宿主 RawSource/SourceRef 接口。
- 做 adapter capability inventory：MR-Mem 的 `SemanticAdmissionService`、`SourceRefReader`、`PathAReader` 是否实际可接；哪些需要在 Host 层补一个薄适配器，哪些完全不用碰。
- 定稿真实用户 ID、workspace 划分、Owner/高老师及其他 Agent 权限；密钥在 secret store，不进配置示例/Git。

**Gate**：部署拓扑、真实连接端点和权限模型都已在配置表落地；不得假设 AML HTTP 就是宿主接口。

## Phase 1 — SQL 领域内核（必须先完成）

- 落地 `db/schema.sql` 和正式 migrations（Alembic/SQL migrations 任选一种；生产迁移具备版本/回退策略）。
- 实现 Project/Task/TaskEvent/Dependency/Focus/Reminder/Inbox/Outbox/Identity/Membership 的受控事务服务；版本冲突与命令幂等。
- 定义触发动作、允许状态转换、延期原因、自动取消僵尸提醒、同一条消息多任务与补充任务的关联规则。
- event append-only（不可通过普通业务 API 修改）；task 表保存最新快照，加速读；两者写在同一事务。
- 引入结构化时间处理：UTC + IANA 时区、相对日期解析的确认策略、日期变更历史。

**Gate**：离线测试通过：并发冲突、同消息重放、多操作事务回滚、事件及 outbox 原子提交、恢复后任务状态不丢失、等待→催促→解阻→恢复流程完整。

## Phase 2 — 统一服务接口与鉴权

- 在受信环境中部署单写服务（推荐 FastAPI + Pydantic 作网络层，领域核心不绑定框架），HTTP `/v1/read`, `/v1/apply`, `/v1/overview`；Schema 严格校验，不允许任意 SQL 字符串。
- API 必须识别**真实调用主体**：服务身份 + 代理最终用户/用户委托，处理 owner、editor、requester、viewer、scheduler。对 agent service 不凭请求体上的 `actor_id` 信任身份。
- 强制任务级/工作区级 ACL，行级过滤，最小化日志，request_id 和 trace_id；只读前端独立 credential。
- 处理 rate-limit、payload 上限、API 超时、错误代码及请求重试。

**Gate**：未授权任意 ID 全拒绝、跨 workspace/任务泄露为零；Agent 之间的同任务读取和版本冲突行为一致。

## Phase 3 — Telegram 双入口与消息接单

- 使用受控 Bot token；优先 long polling 由 NAS 内网主动连接 Telegram，避免为接收消息暴露额外公网端口。若选择 webhook，验证 Telegram secret/反代、仅允许受控群和账户。
- 私有入口与你对外共享入口逻辑或物理隔离；账号 ID + chat ID 双白名单，拒绝匿名管理员、未知 bot、被撤权用户。
- update 先持久化 inbox，再回复/交 Agent；重启可恢复 `received/processing/failed` 任务。防止 Telegram update 再投造成重复任务。
- reply 必须关联原 message ID；如需文件，限制类型/体积并隔离存储；对消息编辑、删除和撤回制定明确处理策略。

**Gate**：重复 update、不认识的账号、匿名管理员、进程崩溃、群/私聊隔离与回执关联测试均通过。

## Phase 4 — Agent 工作台工具与自然语言任务解释

- Agent 仅暴露 `workdesk.read / workdesk.apply / workdesk.overview`；工具 SDK 和 JSON Schema 文档化。
- 语义路由先理解“咨询/自动执行/人工任务/澄清/等待/状态更新”，必要时读取 MR-Mem 历史上下文；目标 ID 不明确时先搜候选并确认。
- 意图转换生成 operations，然后服务端做业务校验。任务字段来自明确消息、已确认计划或可靠工具回执；禁止语义推断偷改官方 due、完成状态。
- 输出可核对的变更摘要：“修改了任务 X 的状态，设置某时间提醒”；写入失败不能宣称成功；外发结果需要 delivery receipt。

**Gate**：同一句复杂变更（已做/待做/等反馈/延后/定时催促）拆成正确原子操作，且无多余建单、越权或虚假完成。

## Phase 5 — MR-Mem 上下文与跨 Agent 同步

- 在宿主维护真实来源消息；写 Workbench task_events 后投递 `memory_outbox`。为每个事件映射稳定 SourceRef、scope、source revision 和 permission filter。
- 通过已存在的 Host semantic compiler/`SemanticAdmissionService`/Path A 接缝处理可接受语义，不重新写 MemoryCore，不把频繁的技术日志全部塞入长期记忆。
- Agent 的读链顺序：Workbench SQL current state →（需要语义背景时）授权 MR-Mem retrieval → 原文/事件证据。MR-Mem 的旧历史与 SQL 新事实冲突时，SQL 任务事实优先；背景仍可保留为历史。
- 同步失败 outbox 可重试，支持补偿/版本更新/撤权后重新索引或 source invalidation；不运行双向状态同步。

**Gate**：Agent A 更新后 Agent B **立即**从 SQL 看到新状态；MR-Mem 暂停/落后时任务执行不受阻，恢复后可追上；对外主体不能召回私人 scope。

## Phase 6 — 时间调度与通知

- scheduler 从 reminders 原子领取 due 触发，按租约/退避/最大重试处理；离线后恢复补扫 overdue。
- 默认“提醒 owner 去催”，自动给高老师催促需显式授权；阻塞解除或等待结果到达自动取消相应提醒。
- 发送通过 delivery_outbox，保存 provider_message_id 和失败原因；网络模糊结果单独归 `unknown`，不胡乱无限重试。
- 提供静默时段、时区、节流和失败通知；以消息标记“提醒已发送/待核验”，不把已发当已读。

**Gate**：重启后不丢提醒、不重复插任务，等待解除后不催，静默时段不打扰，发送失败可追踪。

## Phase 7 — 只读网页与部署

- Read-only Dashboard 按 `overview` + scoped `read` 构建 NOW/NEXT/WAITING/BLOCKED/到期/项目池/事件流；提供移动端适配，不把业务逻辑放前端。
- NAS Docker Compose：workdesk-api、scheduler/worker、dashboard（可同进程分路由）、独立数据卷，MR-Mem 与 Agent Runtime 不随工作台重启而重建。
- 用 Tailscale 进入内网；网页持独立只读 session；API 只监听 private interface/内部网络。数据库使用本地 volume；日志脱敏；secret 不进 git。
- 制定备份、迁移、恢复、报警及观察窗口，验收之后开启常驻服务。

**Gate**：手机/电脑读视图一致；网页凭据泄露不能写、不能访问 Agent 管理端；备份恢复可重建任务池与提醒，无重复外发。

## Phase 8 — 运维与后续优化（不是上线前置验证）

- 关注 real-world：未处理 inbox、WAITING 逾期、MR outbox backlog、重复候选率、提醒成功/unknown、数据恢复点。
- 任务池随项目增长后再做归档和检索优化；优先维护有根据的优先级与下一步，不先堆 AI 自动打分、复杂多 Agent 调度或全功能甘特图。
- 当跨机并发/写吞吐超出单写服务能力，再按业务接口迁到 PostgreSQL；不是现在的前置条件。

## 施工依赖图

```text
Phase 0 ---> Phase 1 ---> Phase 2 ---> Phase 3 ---> Phase 4
                             |                      |
                             +-------------> Phase 5
                             |                      |
                             +-------------> Phase 6
                             |                      |
                             +-------------> Phase 7 ---> Phase 8
```

可以并行开发 UI 静态设计与 MR adapter，但只有领域模型/权限/接口稳定后才能接真数据。对外通知必须等鉴权、outbox、可追溯审计成型后再启用。

## 边界内的具体技术选型

- Python 3.12+；SQLite WAL；单数据库写入服务；SQL migration 工具（生产选定）；FastAPI/Pydantic 网络 DTO；pytest/unittest 契约测试；Docker Compose on NAS。
- Bot：Telegram Bot API 长轮询（webhook 为可切换实现），配独立 outbound delivery worker。
- MR：通过 Host Adapter 调 MR-Mem Canonical + Path A；不直接复用 AML server 的存储协议。
- UI：轻量 SSR/HTML 或简易前端，只读刷新可通过 GET；没有必要引入复杂前端状态管理。
- 配置：环境变量 + 不含密钥的配置文件，统一时区/静默时段/允许群和用户/通知政策。

## 上线与回滚

1. 在独立 staging DB 用合成任务跑端到端，禁止用真实 Telegram 消息作为公开 fixtures。
2. 先只启入站/建单，外部发送保持 dry-run；对照源消息、task_event、outbox。
3. 启用给 owner 的提醒，再逐项授权对外自动回复/催促；监控冲突和 unknown。
4. 发布前备份 DB 与 Host/MR 配置信息，记录 git SHA、schema migration version。
5. 回滚代码时保持 schema 向后兼容；如需 DB 回退，先停写，恢复一致快照并重播**尚未发生外部副作用**的事件；绝不能盲目重发已发送通知。\n## Phase addition — event-to-Run separation (ADR-0002)\n\nBefore Telegram/Agent wiring, finalize the Host capability inventory: stable event-ID dedup, Session and Run reference model, deferred-tool-call checkpoint and resume, tool policy revalidation, and query-by-event-ID reconciliation. Lack of any of these cannot be patched by putting Agent Run state in WorkBench or MR.\n\nA Timer firing must atomically emit a WorkBench wake event. The dispatcher delivers only event metadata to Host; Host owns the Session and its own exactly-once safeguards. On lost acknowledgements, mark WorkBench wake as `unknown` and reconcile with Host; don't retry tool effects blindly. Approval answers go through authenticated business events, **not** direct tool resume. Keep outbound delivery separate from Agent Run outcomes. See [ADR-0002](ADR-0002-ALWAYS-ON-OWNERSHIP.md).\n\nCurrent implementation: normalized `wake_events`, `approval_requests` schema, `WakeEventStore` and offline tests; host worker/approval policy/timer atomic fanout and actual session resume are pending.\n