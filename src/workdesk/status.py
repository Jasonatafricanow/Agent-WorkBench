"""workbench.status.v1 — a tiny read-only status for a desktop indicator (the LiteMonitor plugin).

Why a separate endpoint: the indicator runs on another computer and only needs a few numbers and one
line of text. It gets its own read-only token (`WORKBENCH_STATUS_TOKEN`), never the Host token (which
can write) or the dashboard password (which can read everything, the Life view included), and it can be
served on its own listener so that only this route is reachable from the network.

"Process monitoring" lands here as Host liveness: the Agent Host polls WorkBench every few seconds, so
the time since its last authenticated call tells whether the Host process is alive. The indicator only
shows it; restarting or routing Agents stays with the Host.
"""
from __future__ import annotations

import time

CONTRACT = "workbench.status.v1"
HOST_OK_SECONDS = 120    # polled within 2 minutes: alive
HOST_LATE_SECONDS = 600  # silent for over 10 minutes: treated as down


def host_state(seen_at: float | None, now: float) -> tuple[int | None, int]:
    """(age in seconds or None, 0 ok / 1 late / 2 down-or-never-seen)."""
    if seen_at is None:
        return None, 2
    age = max(0, int(now - seen_at))
    return age, 0 if age <= HOST_OK_SECONDS else 1 if age <= HOST_LATE_SECONDS else 2


def build_status(tasks, actor, *, generated_at: str, host_seen_at: float | None,
                 now: float | None = None) -> dict:
    overview = tasks.overview(actor=actor, limit=200)
    now_task = overview["focus"].get("NOW")
    counts = {"approvals": len(overview["my_approvals"]), "human_actions": len(overview["my_human_actions"]),
              "overdue": len(overview["overdue"]), "unknown": len(overview["unknown_outcomes"]),
              "waiting": len(overview["waiting"]), "inbox": len(overview["inbox"])}
    age, host = host_state(host_seen_at, time.time() if now is None else now)
    attention = 1 if counts["overdue"] or counts["unknown"] or counts["approvals"] else 0
    title = (now_task or {}).get("title") or ""
    parts = [title[:14] + ("…" if len(title) > 14 else "") if title else "无焦点"]
    if counts["approvals"]:
        parts.append(f"审批{counts['approvals']}")
    if counts["overdue"]:
        parts.append(f"逾期{counts['overdue']}")
    if counts["human_actions"]:
        parts.append(f"待办{counts['human_actions']}")
    if host == 2:
        parts.append("Host离线")
    elif host == 1:
        parts.append("Host迟")
    return {"contract": CONTRACT, "generated_at": generated_at, "now": title, **counts,
            "host_age_seconds": age, "host_state": host, "level": max(host, attention),
            "line": " · ".join(parts)}
