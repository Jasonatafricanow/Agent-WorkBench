# 系统组件边界（2026-11-17）

一句话：**WorkBench 管工作，MR-Mem 管记忆，Agent Host 管 Agent 的运行，Mind Runtime 管认知，Facetide 管人生评价，Statebar 管短期用户状态，LiteMonitor 只做桌面指示灯。** 各组件之间只通过明确的接口协作，谁的数据谁做权威，不在别处复制一份当事实。

## 1. 组件与数据权威

| 组件 | 是什么 | 权威数据 | 不做什么 |
|---|---|---|---|
| **Agent-WorkBench**（本仓库，独立项目） | 工作管理系统：任务、项目、期限、进度、等待/依赖、提醒、承诺、交付物、审批、委托，以及这些事的业务历史 | 自有 SQLite（`tasks`、`task_events`、`wake_events`、`task_responsibilities` 等） | 不跑模型、不管理 Agent 进程、不存语义记忆 |
| **MR-Mem** | 长期记忆：语义记忆、SemanticBlock/Thread/LCE、多 Agent 共享上下文 | MemoryCore（工作记忆在其中单独的 `workbench` scope） | 不存任务状态、不排期、不做业务审批 |
| **Agent Host** | 让 Agent 真正跑起来：会话、Run、工具调用、在工具调用那一刻执行拦截、中断与恢复、结果投递 | Host 执行记录（Pi 会话文件、宿主账本 `ledger.jsonl`、入站暂存 `inbound.jsonl`） | 不自己定义业务规则；业务上"能不能做"问 WorkBench |
| **Mind Runtime** | 认知与情感运行时（含认知 WakeSignal） | 既有 Runtime State | 本项目不改动它；它的建议不能改委托、不能授权工具 |
| **Facetide** | 人生轮评估：七领域评分、成长/自我实现反思、长期目标 | 自有 SQLite（只有用户确认过的 review） | 不管任务；WorkBench 只经 `facetide.summary.v1` 只读 |
| **Statebar** | 短期用户状态（睡没睡、今天的安排）的"现实探针" | 自有 SQLite | 不观察任务状态 |
| **LiteMonitor**（另一台电脑上的任务栏性能监控软件） | 用一个 JSON 插件在任务栏显示工作台状态 | **不拥有任何数据** | 不发唤醒、不选 Agent、不写入、不管理进程 |

### 现有的 Agent Host 是哪些

没有一个统一的"多 Agent Host"服务，现状是几个各自独立的宿主进程：

- `host/pi-workbot`（本仓库内）：工作 bot 网关，WorkBench 的 Host 参考实现。
- NAS 上的 `xiyue-pi`（溪月）、`Kayla-Home`：各自的 Pi 网关，与工作台无关。

本次**不新建**统一 Host。多个工作 Agent 的运行已由 `pi-workbot` 支持（`WORKBOT_AGENTS`，交接后以新执行 Agent 的身份运行）。等真的需要统一调度多个宿主时，再按 [HOST_RUN_PROTOCOL.md](HOST_RUN_PROTOCOL.md) 抽出 Host，而不是现在先造。

## 2. 两条容易被误拆的边界

**委托与授权分两层。** "这个 Agent 能不能对这个任务做这类业务动作、什么情况要人批准"是业务事实，属于 WorkBench（`delegation_profiles`、`task_responsibilities`、`evaluate_action`、`approval_requests`，见 ADR-0003）。Host 负责在工具调用发生的那一刻执行这个判断（`guard-extension.ts` 的 `tool_call` 钩子）。所以不是"授权归 Host"，而是 **WorkBench 判定、Host 执行**。

**WorkBench 的定时器和唤醒队列不是第二套调度系统。** `reminders`、`schedule_rules`、`wake_events` 是业务事件的收件箱：到期、外部变化、审批结果、人回复了，这些都是"工作上发生了什么"的持久记录。由谁去处理、开几个 Run、怎么恢复，是 Host 的事。WorkBench 只保证"同一个 workspace 同时只有一个后台 Run 在领事件"，这是业务一致性约束，不是 Agent 调度。

## 3. 读写方向

| 方向 | 怎么做 | 约束 |
|---|---|---|
| 人/Telegram → WorkBench | Host 先落入站（`/v1/intake/inbound`），再开 Run | 消息先持久化再给模型；重复 update 不建第二个任务 |
| WorkBench → Host | Host 轮询 `/v1/wake/claim` 领事件 | 超时变 `unknown`，按 event id 对账，不盲目重跑 |
| Agent → WorkBench | 只经工具门面 `/v1/tools/*`（读）和 `change_tasks`（写，带版本与证据） | **任务结果只能经这条正式接口修改** |
| Agent → MR-Mem | `work_memory_search` / `work_memory_write`，先经 WorkBench 授权 | 记忆是背景，不能断定任务状态 |
| WorkBench 看板 → Facetide | `GET /api/summary/v1`（服务 token） | 只读；只给 owner 看；不进 Host API、Agent 工具和记忆 |
| LiteMonitor → WorkBench | `GET /status/v1`（独立只读 token，可单独监听） | 只读；只返回计数、一行摘要和 Host 是否在线 |

## 4. LiteMonitor 适配插件

LiteMonitor 的插件是声明式 JSON：定时请求一个 HTTP 地址、按 JSON 路径取值、做正则/映射/阈值加工，最后在任务栏显示文字和颜色（0 绿 / 1 黄 / 2 红）。它**不能**响应点击、弹通知或操作进程，插件输出只有显示；要做这些得改 LiteMonitor 的 C# 源码。所以它在这套系统里只当指示灯。

- 插件：[`host/litemonitor/WorkBench.json`](../host/litemonitor/WorkBench.json)，使用说明见同目录 README。
- 接口：`GET /status/v1`，契约 `workbench.status.v1`：
  - `now`：NOW 焦点标题；
  - `approvals`、`human_actions`、`overdue`、`unknown`、`waiting`、`inbox`：计数；
  - `host_age_seconds`、`host_state`：Host 最后一次带 Host token 调用的时间，0 = 2 分钟内、1 = 10 分钟内、2 = 更久或从未；
  - `level`：总体颜色 = max(Host 状态, 有逾期/结果不明/待审批时为 1)；
  - `line`：任务栏一行，例如 `季度报告… · 审批1 · 逾期1`。
- 鉴权：独立的 `WORKBENCH_STATUS_TOKEN`（≥24 字符）。Host token（能写）和看板密码（能看全部，含生活视图）都不能用来读它，它也不能调用任何写接口。
- 网络：`WORKBENCH_STATUS_BIND=0.0.0.0:8791` 会另开一个**只服务 `GET /status/v1`** 的监听；主服务（Host API、看板）仍只在回环地址上。
- "监控进程"的落点就是 Host 在线状态：指示灯只负责让你看到 Host 掉线；重启、换 Agent 仍是 Host 的事。

## 5. 异常处理

| 情况 | 结果 |
|---|---|
| WorkBench 停了 | LiteMonitor 插件请求失败，任务栏显示错误；不影响其他任何组件 |
| Host 停了 | 唤醒事件留在 WorkBench；租约过期变 `unknown`；指示灯在 2/10 分钟后变黄/红；Host 重启后先对账、重放未完成的入站 |
| Facetide 停了 | 生活视图显示缓存并标注时间，其余四个视图不受影响 |
| MR-Mem 停了 | 记忆写入进重试队列（被服务拒收的不再重试）；任务读写不受影响 |
| 状态 token 泄露 | 只能看到计数和 NOW 标题；换 token 即可，不涉及写权限 |
