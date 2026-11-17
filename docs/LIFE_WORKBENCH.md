# 统一工作与生活看板（WB-LIFE-01）

## 权威划分（不变）

| 事实 | 唯一权威 | WorkBench 里怎么用 |
|---|---|---|
| 任务、期限、提醒、执行、委托 | WorkBench SQL | 直接读，经 TaskService / `workdesk.views`（按 workspace 与角色过滤） |
| 人生轮评分、反思、长期目标 | Facetide | 只通过 `facetide.summary.v1` 只读接口读；只缓存最后一次成功结果用于展示 |
| 语义记忆 | MR-Mem | 看板不读 |

看板只读：所有页面都是 GET，筛选是普通 GET 表单，没有任何写入口；看板密码不是 Host 凭据，调不了写接口。

## 五个视图（`/`、`/calendar`、`/timeline`、`/projects`、`/life`）

- **工作台** `/`：NOW/NEXT、进行中/可做、等待/阻塞、Human Inbox、审批、结果不明、逾期、最近完成、最近变化、停滞、排期过载与 Soft Due 建议。
- **日历** `/calendar?range=week|month`：官方 DDL、Soft Due、计划（planned_start）、跟进提醒、待人处理，各自独立条目；每天显示工作量（见下）。
- **时间线** `/timeline?range=today|week|month`：任务事件（创建、修改、延期、交付、完成……，含来源与操作人）和 Agent 执行记录。
- **项目池** `/projects`：项目及其任务、执行人（有效委托时为当前执行 Agent）、进度、最近官方期限、阻塞数。
- **生活** `/life`：**只给 workspace owner**。最近确认的人生轮、七领域评分（没打分显示 —）、与上一期比较、成长/自我实现（用户原文）、长期目标。

日历、时间线、项目池都支持 `?project=` 与 `?requester=`，只能缩小范围；另一个 workspace 的项目 id 返回 404。

## 双期限与工作量（P0）

- 官方 DDL 与 Soft Due 是两个独立日历条目；同一天也保留两个标记。
- 工作量每个任务只算一次：有 Soft Due 记在 Soft Due 那天，否则记在官方 DDL 那天。
- 排期建议只移动 Soft Due（只往前），从不改官方 DDL，也从不自动写入。

## Host API（只读，需 Host token + 身份）

```
POST /v1/views/calendar  {identity, start, end, project_id?, requester_id?}
POST /v1/views/timeline  {identity, start, end, project_id?, requester_id?, limit?}
POST /v1/views/projects  {identity, project_id?, requester_id?}
```

`start`/`end` 为 UTC `YYYY-MM-DDTHH:MM:SSZ`，区间左闭右开。请求人只看到自己请求的对外部分：官方期限与状态里程碑，看不到 Soft Due、计划、提醒、执行人、来源与 Agent 运行。生活数据**不在** Host API 上，Agent 工具和工作记忆都拿不到。

## Facetide 接入（P3）

`LifeStateAdapter`（`workdesk/life.py`）配置：

| 变量 | 说明 |
|---|---|
| `FACETIDE_SUMMARY_URL` | 例如 `http://127.0.0.1:8765/api/summary/v1` |
| `FACETIDE_SUMMARY_TOKEN` | 与 Facetide 端 `FACETIDE_SUMMARY_TOKEN` 相同的服务 token |
| `FACETIDE_TIMEOUT_SECONDS` | 默认 3 |
| `FACETIDE_CACHE_FILE` | 默认与数据库同目录的 `facetide-summary-cache.json` |

- 成功：显示并写缓存（含 `fetched_at`）。
- Facetide 停机/超时/鉴权失败/契约不符：显示缓存并标注"显示某时间的数据"；没有缓存就显示"暂时连不上"。其他四个视图不受影响。
- 60 秒内的重复页面加载复用上次结果，不反复打 Facetide。
- 只经 HTTP 接口读取，不碰 Facetide 的 SQLite。

接口契约见 facetide-mcp 仓库 `docs/summary-v1.md`。

## 部署前提

Facetide 的 Web 服务目前只接受回环地址（`127.0.0.1`/`localhost`）。WorkBench 与 Facetide 在同一台机器时可直接用；WorkBench 上 NAS 后要么 Facetide 也在 NAS，要么另外设计受限的网络暴露方式（本次未做）。
