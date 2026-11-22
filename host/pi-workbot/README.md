# Work bot (Agent Host on Pi)

A separate Pi process for work: external collaborators (e.g. Gao) and the owner talk
to it on Telegram. It is **not** the companion bot and shares none of its persona,
skills, sessions or memory scope.

| File | Role |
|---|---|
| `gateway.mjs` | Telegram intake → WorkBench inbox → one Pi Run per chat lane; polls wake events (timers, approvals, human replies, external changes); delivers notifications; local `ledger.jsonl` (event id → session, outcome) re-settles WorkBench after a restart |
| `tools.mjs` | Agent tools: `list_tasks`, `get_task`, `change_tasks`, `whats_new`, `work_memory_search`, `work_memory_write` (memory written by the Agent at wrap-up) |
| `agent/guard-extension.ts` | Pi `tool_call` hook: asks WorkBench how a guarded action routes; files an approval and ends the Run when needed; lets exactly one matching call through after approval |
| `agent/WORKBOT.md` | Working rules appended to the system prompt |
| `turn-context.mjs` | The verified identity of the current Run (never chosen by the model) |

Owner commands in Telegram: `/approve <id>`, `/deny <id>` (the approver is notified with the id).

Guarded tools (`WORKBOT_GUARDED_TOOLS`): `{"<tool>": {"action": "<business action>", "task_from": "task_id",
"conditions_from": "<input field that, when truthy, triggers an approval condition>"}}`. A guarded call
without a task id always needs approval.

Status: written against Pi 1.5.2 APIs used by the companion gateway (`createAgentSession`,
`SessionManager.open/continueRecent`, `tool_call` hook). The guard hook was exercised end to
end against a live WorkBench server; the Telegram gateway itself has not run with a real bot
token yet.

Setup (after WorkBench is running):
```
python -m workdesk --db /data/workbench.sqlite bootstrap --workspace work --title 工作 --owner owner --owner-name <name> --agent agent --agent-name 工作bot
python -m workdesk --db ... bind-identity --provider telegram --bot-id workbot --external-user-id <owner tg id> --principal owner
python -m workdesk --db ... bind-identity --provider host --bot-id workbot --external-user-id agent --principal agent
python -m workdesk --db ... add-member --workspace work --principal gao --name 高老师 --role requester
python -m workdesk --db ... bind-identity --provider telegram --bot-id workbot --external-user-id <gao tg id> --principal gao
# monthly export from the internal system (columns renamed to external_key / title / official_due_local)
python -m workdesk --db ... import-csv --workspace work --principal owner --source internal --period 2026-12 --file due.csv --map '{"案号":"external_key","名称":"title","官方期限":"official_due_local"}'
python -m workdesk --db ... backup --to /data/backups
python -m workdesk --db /data/backups/<file> verify
```

Owner preferences (`user_profiles.work_preferences_json` for the owner):
`{"digest_time": "09:00", "due_soon_days": 3, "quiet_hours": {"start": "23:00", "end": "08:00"},
"soft_due_rule": {"days_before": 3, "skip_weekends": true, "at": "18:00"}}`
