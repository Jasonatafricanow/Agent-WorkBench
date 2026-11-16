# Agent Workbench — 多 Agent 共享工作台

状态：**架构与接口基线 + 可运行的 SQL 领域内核参考实现**（不是已接通 Telegram/MR-Mem 的产品）。

该项目为独立业务服务，不修改 MR-Mem 内核。SQL 维护计划、任务、执行、等待、延期、下一步、定时催促等业务事实；MR-Mem 只负责经授权的语义记忆及上下文检索；Agent 通过一个授权工具门面读写业务状态；Telegram 是人机入口；Dashboard 是只读投影。

## 已交付文件

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)：边界、拓扑、权限、事实与推断、事件及多 Agent 同步。
- [docs/DOMAIN_CONTRACT.md](docs/DOMAIN_CONTRACT.md)：业务状态机、数据字段、Agent 工具接口、自然语言转换规则、时钟/定时任务。
- [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md)：按依赖顺序的工程施工、完成条件、部署及回滚。
- [docs/ACCEPTANCE.md](docs/ACCEPTANCE.md)：端到端场景、安全与可靠性验收矩阵。
- [docs/ADR.md](docs/ADR.md)：冻结设计决策与尚未确定的部署变量。
- [db/schema.sql](db/schema.sql)：SQLite DDL（原始消息不复制入 MR-Mem）。
- [examples/agent_commands.json](examples/agent_commands.json)：Agent 工具意图示例（规范示例，不代表已部署 API）。
- [src/workdesk/store.py](src/workdesk/store.py)：可运行的事务化核心参考实现。
- [tests/test_store.py](tests/test_store.py)：离线核心测试。

## 快速验证领域内核

使用 Python 3.12+，不需要模型/API Token：

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

`store.py` 仅验证 SQLite 任务领域的权限、状态机、幂等、事件/记忆 outbox、乐观锁和提醒领取等基础机制。尚未提供网络服务、真实 Bot、模型调用、MR-Mem SourceRef 适配或前端。不要将示例视为生产部署。

## 参考依据与版本边界（2026-11-16 核对）

- MR-Mem `main` README：<https://github.com/Jasonatafricanow/MR-Mem/blob/main/README.md>
- Sidecar 边界：<https://github.com/Jasonatafricanow/MR-Mem/blob/main/docs/ARCHITECTURE.md>
- ADR-0001：<https://github.com/Jasonatafricanow/MR-Mem/blob/main/docs/ADR-0001-SIDECAR-MEMORY-BOUNDARY.md>
- Path A：<https://github.com/Jasonatafricanow/MR-Mem/blob/main/docs/PATH_A.md>
- AML adapter：<https://github.com/Jasonatafricanow/MR-Mem/blob/main/docs/AML.md>

注意：MR-Mem 的 AML `/add`、`/search` 不是本项目的宿主接入契约，不能据此宣称已完成记忆集成。

## 设计补充

- [docs/MRMEM_INTEGRATION.md](docs/MRMEM_INTEGRATION.md)：MR-Mem 源引用、记忆 outbox、读写链与多 Agent 同步。
- [docs/SECURITY_OPERATIONS.md](docs/SECURITY_OPERATIONS.md)：访问控制、NAS 部署边界、备份恢复与故障处理。
- [examples/local_demo.py](examples/local_demo.py)：合成任务创建及工作台状态读取。
- [pyproject.toml](pyproject.toml)：标准 Python 包元信息，支持 `python -m pip install -e .`。

内核离线验收：`PYTHONPATH=src python -m unittest discover -s tests -v`。
演示：`PYTHONPATH=src python examples/local_demo.py`。

本包尚未包含生产级 HTTP 认证层、Telegram Adapter、MR-Mem Host Adapter、Scheduler 守护进程或 Dashboard 页面。详细接口和施工门槛已经定义；不能将其误认作可部署成品。