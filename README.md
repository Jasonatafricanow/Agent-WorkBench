# Agent WorkBench — 独立的多 Agent 工作台

**GitHub 开发状态：正式架构与模块接口已建立；生产接线未完成。**

工作台独立于 MR-Mem：SQL 记录项目、任务、执行历史、延期原因、等待对象与提醒；MR-Mem 提供经宿主授权的用户语义上下文，不能替代 SQL 当前任务状态。Telegram 接收需求和进度，Agent 通过固定工具门面操作，Dashboard 只读。

## 分支与仓库

- WorkBench 分支：`feat/modular-backend-20261116`
- MR-Mem 对接分支：`feat/workbench-user-context-20261116`
- Draft PR：#1（此仓库）；MR-Mem #22
- `main` 目前为初始化基线，勿将开发分支误认为已上线

## 当前已提交

- `src/workdesk/contracts.py`：固定已认证 ActorContext 的 Agent 工具门面、IdentityProvider / TaskService 协议；不承担实际身份认证
- `src/workdesk/user_context.py`：SQL 授权检查，实现 MR-Mem 的 USER 作用域权限边界；默认拒绝、支持撤销
- `db/schema.sql` 同时包含 `user_profiles` 与 `memory_context_grants`，身份映射与授权按工作空间隔离
- `db/schema.sql`：SQL 初始 Schema（非迁移程序）
- `tests/test_contracts.py`、`tests/test_schema.py`、`tests/test_user_context.py`：工具作用域、数据库隔离与用户记忆授权测试
- `docs/ARCHITECTURE.md`、`docs/DOMAIN_CONTRACT.md`、`docs/MRMEM_INTEGRATION.md`：业务/记忆边界
- `docs/IMPLEMENTATION_PLAN.md`、`docs/ACCEPTANCE.md`、`docs/SQL_MIGRATION_POLICY.md`：施工与验收
- `.github/workflows/quality.yml`：离线测试

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
