# LiteMonitor 插件：工作台状态

在 [LiteMonitor](https://github.com/Diorser/LiteMonitor) 的任务栏上显示工作台的当前焦点、待审批、逾期，以及 Agent Host 是否在线。插件只读，不做任何写入或进程操作。

## WorkBench 端

在 WorkBench 的环境里设置：

```
WORKBENCH_STATUS_TOKEN=<随机生成，至少 24 个字符>
WORKBENCH_STATUS_BIND=0.0.0.0:8791      # 只开放 GET /status/v1；不需要跨机器时用 127.0.0.1:8791
WORKBENCH_DASHBOARD_WORKSPACE=work       # 状态按这个 workspace 的 owner 视角统计
WORKBENCH_DASHBOARD_PRINCIPAL=owner
```

生成 token：`python -c "import secrets;print(secrets.token_urlsafe(32))"`。这个 token 只能读状态，和 Host token、看板密码都不通用。

## LiteMonitor 端

1. 把 `WorkBench.json` 放进 LiteMonitor 的 `resources/plugins/` 目录。
2. 在设置里点"重载插件"，添加"工作台状态"。
3. 填两项：
   - **WorkBench 状态地址**：`http://<工作台所在机器的局域网或 Tailscale 地址>:8791`
   - **只读状态 Token**：上面的 `WORKBENCH_STATUS_TOKEN`
4. 任务栏会出现两项：
   - `WB`：一行摘要，如 `季度报告… · 审批1 · 逾期1`。颜色：绿 = 一切正常，黄 = 有逾期/待审批/结果不明或 Host 迟到，红 = Host 离线。
   - `Host`：在线 / 迟到 / 离线（最后一次轮询在 2 分钟内 / 10 分钟内 / 更久）。

每 30 秒刷新一次（最少 15 秒）。

## 已验证与未验证

- 已验证：插件按 LiteMonitor 插件引擎的规则（请求头 `{{}}` 替换、JSON 路径提取、`map` 加工、输出模板）在测试里被逐步执行（`tests/test_status.py`）；本机实测 `/status/v1` 返回真实数据，Host 启动后状态从"离线"变为"在线"。
- 未验证：还没有在装了 LiteMonitor 的那台电脑上实际加载这个插件。
