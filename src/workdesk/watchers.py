"""External watch adapters (polling). Each observation goes through
`scheduler.observe_watch`, which wakes the Host once on a match.

Supported `source_kind`:
- `github_ci`: target `owner/repo@ref` -> `pending` | `success` | `failure` (check runs of that ref)
- `github_pr`: target `owner/repo#123` -> `open` | `closed` | `merged`
`GITHUB_TOKEN` is optional (public repos work without it, at a lower rate limit).
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
from contextlib import closing
from pathlib import Path
from typing import Callable

from .scheduler import observe_watch
from .storage import connect

API = "https://api.github.com"
Fetch = Callable[[str], dict]


def github_get(path: str) -> dict:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "agent-workbench"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(urllib.request.Request(API + path, headers=headers), timeout=15) as response:
        return json.loads(response.read())


def ci_state(check_runs: list[dict]) -> str:
    if not check_runs or any(run.get("status") != "completed" for run in check_runs):
        return "pending"
    ok = {"success", "neutral", "skipped"}
    return "success" if all(run.get("conclusion") in ok for run in check_runs) else "failure"


def observe(source_kind: str, target_ref: str, fetch: Fetch = github_get) -> tuple[str, str] | None:
    """Return (observed_value, source_ref) or None for unsupported/invalid targets."""
    if source_kind == "github_ci":
        m = re.fullmatch(r"([\w.-]+/[\w.-]+)@([\w./-]+)", target_ref)
        if not m:
            return None
        data = fetch(f"/repos/{m[1]}/commits/{m[2]}/check-runs?per_page=100")
        return ci_state(data.get("check_runs", [])), f"https://github.com/{m[1]}/commit/{m[2]}/checks"
    if source_kind == "github_pr":
        m = re.fullmatch(r"([\w.-]+/[\w.-]+)#(\d+)", target_ref)
        if not m:
            return None
        pr = fetch(f"/repos/{m[1]}/pulls/{m[2]}")
        state = "merged" if pr.get("merged") else pr.get("state", "open")
        return state, f"https://github.com/{m[1]}/pull/{m[2]}"
    return None


def poll_watches(database: str | Path, *, now: str, fetch: Fetch = github_get) -> list[dict]:
    with closing(connect(database)) as conn:
        watches = conn.execute(
            "SELECT workspace_id,id,source_kind,target_ref FROM external_watches WHERE status='watching'").fetchall()
    results = []
    for w in watches:
        try:
            seen = observe(w["source_kind"], w["target_ref"], fetch)
        except Exception as exc:  # noqa: BLE001 - one bad target must not stop the others
            results.append({"watch_id": w["id"], "error": str(exc)[:120]})
            continue
        if seen is None:
            continue
        value, source_ref = seen
        outcome = observe_watch(database, workspace_id=w["workspace_id"], watch_id=w["id"],
                                observed_value=value, source_ref=source_ref, now=now)
        results.append({"watch_id": w["id"], "observed": value, **outcome})
    return results
