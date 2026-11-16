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
    g = sub.add_parser("grant-memory", help="owner grants a member read/write on the workspace's work memory")
    g.add_argument("--workspace", required=True)
    g.add_argument("--principal", required=True)
    g.add_argument("--by", required=True, help="granting owner principal")
    g.add_argument("--capability", choices=["memory.read", "memory.write"], action="append", required=True)
    sub.add_parser("fire-timers")
    ic = sub.add_parser("import-csv", help="import a monthly export (CSV) and reconcile it with the task pool")
    ic.add_argument("--workspace", required=True)
    ic.add_argument("--principal", required=True)
    ic.add_argument("--source", required=True)
    ic.add_argument("--period", required=True)
    ic.add_argument("--file", required=True)
    ic.add_argument("--map", default="{}", help='column renames, e.g. {"案号":"external_key","官方期限":"official_due_local"}')
    bk = sub.add_parser("backup")
    bk.add_argument("--to", required=True)
    sub.add_parser("verify", help="integrity/restore check of a database file")
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
            # Owner and the work Agent may use the workspace's work memory; nobody else by default.
            for who in (a.owner, a.agent):
                for cap in ("memory.read", "memory.write"):
                    c.execute("""INSERT OR IGNORE INTO memory_context_grants(workspace_id,actor_principal_id,memory_user_id,
                                   capability,granted_by_principal_id,grant_source_ref,created_at) VALUES(?,?,?,?,?,?,?)""",
                              (a.workspace, who, f"workbench:{a.workspace}", cap, a.owner, "bootstrap", now))
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
    elif a.cmd == "grant-memory":
        with write_transaction(a.db) as c:
            role = c.execute("SELECT role FROM memberships WHERE workspace_id=? AND principal_id=? AND active=1",
                             (a.workspace, a.by)).fetchone()
            if role is None or role[0] != "owner":
                print("only the workspace owner may grant memory access", file=sys.stderr)
                return 2
            for cap in a.capability:
                c.execute("""INSERT OR REPLACE INTO memory_context_grants(workspace_id,actor_principal_id,memory_user_id,
                               capability,granted_by_principal_id,grant_source_ref,created_at,revoked_at)
                             VALUES(?,?,?,?,?,?,?,NULL)""",
                          (a.workspace, a.principal, f"workbench:{a.workspace}", cap, a.by, "cli", utc_now()))
        print("ok")
    elif a.cmd == "import-csv":
        from .imports import import_rows, rows_from_csv
        rows = rows_from_csv(open(a.file, encoding="utf-8-sig").read(), json.loads(a.map))
        actor = ActorContext(a.workspace, a.principal, "owner", "cli")
        print(json.dumps(import_rows(a.db, actor=actor, source_name=a.source, period=a.period, rows=rows),
                         ensure_ascii=False, indent=1))
    elif a.cmd == "backup":
        from .storage import backup
        print(backup(a.db, a.to))
    elif a.cmd == "verify":
        from .storage import verify
        print(json.dumps(verify(a.db), ensure_ascii=False, indent=1))
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
