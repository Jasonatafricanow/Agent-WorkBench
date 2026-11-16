# Agent WorkBench — 独立的多 Agent 工作台

**首先阅读：[责任转移与人类参与核心设计 ADR-0003](docs/ADR-0003-PROGRESSIVE-DELEGATION.md)**，然后阅读 [总体蓝图、设计推导与整体框架](docs/PROJECT_BLUEPRINT.md)（为什么这样分工、数据流、状态机、模块依赖、故障与分阶段实施）。对应的跨仓边界见 [协作协议矩阵](docs/CROSS_REPO_INTERFACE_MATRIX.md)。

**GitHub 开发状态：正式架构与模块接口已建立；生产接线未完成。**

工作台是持续运转的个人工作系统：人类是工作循环中的一等参与者，但不是系统持续推进的前置条件；已授权工作在用户离线时继续，需人工判断的事项进入持久 Human Action Inbox。Progressive Delegation 允许按工作类别和证据逐步转移执行责任。SQL 记录项目、任务、执行历史、责任分配、授权范围、延期原因、等待对象与提醒；MR-Mem 提供经宿主授权的用户语义上下文，不能替代 SQL 当前任务状态。Telegram 接收需求和进度，Agent 通过固定工具门面操作，Dashboard 只读。

## 分支与仓库

- WorkBench 分支：`feat/modular-backend-20261116`
- MR-Mem 对接分支：`feat/workbench-user-context-20261116`
- Draft PR：#1（此仓库）；MR-Mem #22
- `main` 目前为初始化基线，勿将开发分支误认为已上线

## 新增核心架构（2026-11-17）

- [ADR-0003 — Progressive Delegation](docs/ADR-0003-PROGRESSIVE-DELEGATION.md)：五级自主权、按工作类别授权、证据门槛、人工接管与撤权。
- `delegation_profiles` / `task_responsibilities` / `responsibility_events` / `human_action_requests`：责任转移和人类参与的 SQL 事实源。
- `src/workdesk/delegation.py`：纯策略路由与升级评估；不代替 Host 工具授权。

## 当前已提交

- `src/workdesk/contracts.py`：固定已认证 ActorContext 的 Agent 工具门面、IdentityProvider / TaskService 协议；不承担实际身份认证
- `src/workdesk/user_context.py`：SQL 授权检查，实现 MR-Mem 的 USER 作用域权限边界；默认拒绝、支持撤销
- `db/schema.sql` 同时包含 `user_profiles` 与 `memory_context_grants`，身份映射与授权按工作空间隔离
- `db/schema.sql`：SQL 初始 Schema（非迁移程序）
- `tests/test_contracts.py`、`tests/test_schema.py`、`tests/test_user_context.py`：工具作用域、数据库隔离与用户记忆授权测试
- `docs/ARCHITECTURE.md`、`docs/DOMAIN_CONTRACT.md`、`docs/MRMEM_INTEGRATION.md`：业务/记忆边界
- `docs/IMPLEMENTATION_PLAN.md`、`docs/ACCEPTANCE.md`、`docs/SQL_MIGRATION_POLICY.md`：施工与验收
- `.github/workflows/quality.yml`：离线测试

## 已实现：SQL TaskService（2026-11-17）

- `src/workdesk/task_service.py`：`SqliteTaskService` 实现 `TaskService` 协议（`overview / read / apply`），并提供 Host 侧的 `evaluate_action`（按任务委托范围路由业务动作，不是工具授权）。
- 一次 `apply` 全有或全无：任务快照、`task_events`、委托记录与 `responsibility_events`、`human_action_requests`、提醒、wake event、`command_receipts` 同一个 `BEGIN IMMEDIATE` 事务。
- 幂等（同 key 同 payload 返回原回执；不同 payload 或不同主体报冲突）、`expected_version` 乐观锁、批内 `ref` 引用（如先建任务再挂提醒）。
- 权限每次从 `memberships` 重读；requester 只看到自己提的任务（精简视图），看不到的任务与不存在的任务返回同一个 `NOT_FOUND`；requester 只能提需求、写备注、请 owner 处理。
- 证据规则：DONE/CANCELLED/重开、改已有官方截止日需 `explicit_user`/`verified_tool` + source_ref + reason；截止日推后需 `delay_kind` + reason；WAITING 必须有未解除的依赖；依赖解除自动取消对应的催促提醒。
- 委托：只有人类 owner 能改执行人和自主级别，只能收窄已批准的工作类别策略；暂停/撤权立即生效；人回复 Human Action 后在同一事务生成 `human_action_completed` wake event，回到工作循环。
- `src/workdesk/profiles.py`：`DelegationProfileAdmin`，owner 专用的策略发布入口，**不经过 Agent 工具门面**。
- 测试：`tests/test_task_service.py`（真实 SQLite 文件，含双写并发、批量回滚、重启、越权、委托上限、人回复回环）。

尚未实现：版本化迁移与日期粒度（WB-02）、承诺/交付物/工作书签实体、timer worker、Host 派发与 unknown 对账、delivery outbox 发送端、Telegram 入口、只读看板、NAS 部署。

## 下一步的真实工程工作

将已形成的领域内核参考实现逐模块迁入本仓库，重点是 SQL 事务状态机、task_events、幂等与版本冲突、requester 作用域、dependency/reminder、outbox。随后接入已认证 Telegram Bot、MR-Mem Host Adapter、调度守护进程与只读 Dashboard，再做 NAS 部署。**上述事项未接通前不能声称交付完整生产系统。**

## 验证

```bash
python -m pip install -e .
python -m unittest discover -s tests -v
```

无模型调用、无公网发送、无凭据。访问控制始终由宿主服务端和业务层执行，不能信任 Agent 参数中的用户 ID、权限和来源。

## 冻结边界

不要把任务数据库、Bot Token、原始会话记录、调度机制或业务状态机写入 MR-Mem 内核。语义来源经当前的 SourceRef 与宿主认可的 Semantic Admission 进入记忆；原始任务状态始终来自 WorkBench SQL。

## Always-on architecture update (2026-11-16)

The independent WorkBench now includes a **business wake-event ledger** and a typed Agent Host handoff contract. This is not a new always-running Agent service. **Agent Host** retains Sessions/Runs/deferred tool calls; **Mind Runtime** retains cognition/wake intent; **MR-Mem** retains semantic memory. See [ADR-0002](docs/ADR-0002-ALWAYS-ON-OWNERSHIP.md) and [Host protocol](docs/HOST_RUN_PROTOCOL.md). The new code remains an integration foundation; Host/Telegram live wiring is not finished.
