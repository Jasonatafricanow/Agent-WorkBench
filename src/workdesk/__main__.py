"""Operator CLI: `python -m workdesk <command>`.

Trusted bootstrap only (identities, owner profiles); the Agent never reaches it.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from .contracts import ActorContext
from .identity import bind_identity
from .profiles import DelegationProfileAdmin
from .scheduler import fire_due
from .storage import migrate, utc_now, write_transaction


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="workdesk")
    p.add_argument("--db", default=os.environ.get("WORKBENCH_DB", "workbench.sqlite"))
    sub = p.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("migrate")
    m.add_argument("--backup")
    b = sub.add_parser("bootstrap", help="create workspace, owner and agent principals")
    b.add_argument("--workspace", required=True)
    b.add_argument("--title", required=True)
    b.add_argument("--timezone", default="Asia/Shanghai")
    b.add_argument("--owner", required=True)
    b.add_argument("--owner-name", required=True)
    b.add_argument("--agent", required=True)
    b.add_argument("--agent-name", required=True)
    mem = sub.add_parser("add-member")
    mem.add_argument("--workspace", required=True)
    mem.add_argument("--principal", required=True)
    mem.add_argument("--name", required=True)
    mem.add_argument("--kind", choices=["human", "agent", "service"], default="human")
    mem.add_argument("--role", choices=["owner", "editor", "requester", "viewer", "scheduler"], required=True)
    i = sub.add_parser("bind-identity")
    for flag in ("--provider", "--bot-id", "--external-user-id", "--principal"):
        i.add_argument(flag, required=True)
    pr = sub.add_parser("publish-profile", help="owner publishes a delegation profile from a JSON file")
    pr.add_argument("--workspace", required=True)
    pr.add_argument("--owner", required=True)
    pr.add_argument("--file", required=True)
    sub.add_parser("fire-timers")
    s = sub.add_parser("serve")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8790)
    a = p.parse_args(argv)

    if a.cmd == "migrate":
        print(json.dumps({"applied": migrate(a.db, backup_to=a.backup)}))
    elif a.cmd == "bootstrap":
        migrate(a.db)
        now = utc_now()
        with write_transaction(a.db) as c:
            c.execute("INSERT OR IGNORE INTO principals(id,display_name,kind,created_at) VALUES(?,?,?,?)",
                      (a.owner, a.owner_name, "human", now))
            c.execute("INSERT OR IGNORE INTO principals(id,display_name,kind,created_at) VALUES(?,?,?,?)",
                      (a.agent, a.agent_name, "agent", now))
            c.execute("INSERT OR IGNORE INTO workspaces(id,title,owner_id,timezone,created_at) VALUES(?,?,?,?,?)",
                      (a.workspace, a.title, a.owner, a.timezone, now))
            c.executemany("INSERT OR IGNORE INTO memberships(workspace_id,principal_id,role) VALUES(?,?,?)",
                          [(a.workspace, a.owner, "owner"), (a.workspace, a.agent, "editor")])
        print("ok")
    elif a.cmd == "add-member":
        with write_transaction(a.db) as c:
            c.execute("INSERT OR IGNORE INTO principals(id,display_name,kind,created_at) VALUES(?,?,?,?)",
                      (a.principal, a.name, a.kind, utc_now()))
            c.execute("INSERT OR REPLACE INTO memberships(workspace_id,principal_id,role,active) VALUES(?,?,?,1)",
                      (a.workspace, a.principal, a.role))
        print("ok")
    elif a.cmd == "bind-identity":
        bind_identity(a.db, provider=a.provider, bot_id=a.bot_id, external_user_id=a.external_user_id,
                      principal_id=a.principal)
        print("ok")
    elif a.cmd == "publish-profile":
        spec = json.loads(open(a.file, encoding="utf-8").read())
        owner = ActorContext(a.workspace, a.owner, "owner", "cli")
        print(json.dumps(DelegationProfileAdmin(a.db).publish(owner=owner, **spec), ensure_ascii=False))
    elif a.cmd == "fire-timers":
        print(json.dumps(fire_due(a.db, now=utc_now()), ensure_ascii=False))
    elif a.cmd == "serve":
        from .http_api import WorkbenchApp, serve
        migrate(a.db)
        app = WorkbenchApp(a.db, host_token=os.environ.get("WORKBENCH_HOST_TOKEN", ""),
                           dashboard_token=os.environ.get("WORKBENCH_DASHBOARD_TOKEN"),
                           dashboard_workspace=os.environ.get("WORKBENCH_DASHBOARD_WORKSPACE"),
                           dashboard_principal=os.environ.get("WORKBENCH_DASHBOARD_PRINCIPAL"))
        serve(app, host=a.host, port=a.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
