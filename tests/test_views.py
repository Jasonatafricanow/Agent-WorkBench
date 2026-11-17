"""WB-LIFE-01 P2: calendar / timeline / project views stay inside the caller's workspace and role."""
import sqlite3
import unittest
from contextlib import closing

from test_framework import NOW, Base

from workdesk import ActorContext
from workdesk.errors import Forbidden, NotFound
from workdesk import views


class ViewFixture(Base):
    def setUp(self):
        super().setUp()
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("INSERT INTO principals(id,display_name,kind,created_at) VALUES('li','Li','human',?)", (NOW,))
            c.execute("INSERT INTO memberships(workspace_id,principal_id,role) VALUES('ws','li','requester')")
            # A second workspace with its own owner and task: must never show up for ws callers.
            c.execute("INSERT INTO principals(id,display_name,kind,created_at) VALUES('other','Other','human',?)", (NOW,))
            c.execute("INSERT INTO workspaces(id,title,owner_id,timezone,created_at) VALUES('ws2','W2','other','Asia/Shanghai',?)", (NOW,))
            c.execute("INSERT INTO memberships(workspace_id,principal_id,role) VALUES('ws2','other','owner')")
            c.execute("INSERT INTO projects(id,workspace_id,title,created_at,updated_at) VALUES('p-ws2','ws2','Secret',?,?)", (NOW, NOW))
            c.execute("""INSERT INTO tasks(id,workspace_id,title,state,official_due_at,version,created_at,updated_at)
                         VALUES('t-ws2','ws2','Other workspace task','READY','2026-11-20T09:00:00Z',1,?,?)""", (NOW, NOW))
            c.execute("""INSERT INTO task_events(id,workspace_id,task_id,actor_id,kind,evidence_type,occurred_at,recorded_at)
                         VALUES('e-ws2','ws2','t-ws2','other','created','explicit_user',?,?)""", (NOW, NOW))
        self.gao = ActorContext("ws", "gao", "requester", "telegram")
        self.li = ActorContext("ws", "li", "requester", "telegram")
        self.project = self.apply(self.owner, "p", {"op": "create_project", "title": "Q4", "evidence_type": "explicit_user"})[0]["project_id"]
        self.mine = self.task("g", requester_id="gao", project_id=self.project, official_due_at="2026-11-20T09:00:00Z",
                              soft_due_at="2026-11-19T09:00:00Z")
        self.theirs = self.task("l", requester_id="li", official_due_at="2026-11-21T09:00:00Z")
        self.internal = self.task("i", soft_due_at="2026-11-22T09:00:00Z")
        self.apply(self.owner, "rem", {"op": "set_reminder", "task_id": self.internal, "expected_version": 1,
                                       "local_at": "2026-11-18T09:00", "kind": "followup", "recipient_id": "owner",
                                       "evidence_type": "explicit_user"})

    def ids(self, result, key="task_id"):
        return {i[key] for i in result["items"]}


class CalendarAndIsolation(ViewFixture):
    WEEK = dict(start="2026-11-17T00:00:00Z", end="2026-11-24T00:00:00Z")

    def test_owner_sees_both_due_kinds_follow_ups_and_nothing_from_another_workspace(self):
        cal = views.calendar(self.db, actor=self.owner, **self.WEEK)
        kinds = {(i["task_id"], i["type"]) for i in cal["items"]}
        self.assertIn((self.mine, "official_due"), kinds)
        self.assertIn((self.mine, "soft_due"), kinds)
        self.assertIn((self.internal, "follow_up"), kinds)
        self.assertNotIn("t-ws2", self.ids(cal))
        with self.assertRaises(NotFound):  # another workspace's project id is not a valid filter
            views.calendar(self.db, actor=self.owner, project_id="p-ws2", **self.WEEK)

    def test_requester_sees_only_own_official_dues(self):
        cal = views.calendar(self.db, actor=self.gao, **self.WEEK)
        self.assertEqual({(i["task_id"], i["type"]) for i in cal["items"]}, {(self.mine, "official_due")})
        # A filter naming someone else's requests cannot widen a requester's view.
        self.assertEqual(views.calendar(self.db, actor=self.gao, requester_id="li", **self.WEEK)["items"], [])

    def test_actor_from_another_workspace_or_role_is_refused(self):
        with self.assertRaises(Forbidden):
            views.calendar(self.db, actor=ActorContext("ws2", "owner", "owner", "dashboard"), **self.WEEK)
        with self.assertRaises(Forbidden):  # claimed role must match the membership
            views.projects(self.db, actor=ActorContext("ws", "gao", "owner", "dashboard"))


class PlannedStarts(ViewFixture):
    def test_planned_start_is_a_calendar_item_for_members_only(self):
        t = self.task("plan", requester_id="gao", planned_start_at="2026-11-20T07:00:00Z")
        week = dict(start="2026-11-17T00:00:00Z", end="2026-11-24T00:00:00Z")
        owner = {(i["task_id"], i["type"]) for i in views.calendar(self.db, actor=self.owner, **week)["items"]}
        self.assertIn((t, "planned"), owner)
        gao = {(i["task_id"], i["type"]) for i in views.calendar(self.db, actor=self.gao, **week)["items"]}
        self.assertNotIn((t, "planned"), gao)  # an internal plan, not an outward date


class TimelineFilters(ViewFixture):
    def test_date_window_is_exact(self):
        self.clock.now = "2026-11-18T03:00:00Z"
        self.apply(self.owner, "late", {"op": "append_progress", "task_id": self.internal, "expected_version": 2,
                                        "text": "next day", "evidence_type": "explicit_user"})
        day1 = views.timeline(self.db, actor=self.owner, start="2026-11-17T00:00:00Z", end="2026-11-18T00:00:00Z")
        day2 = views.timeline(self.db, actor=self.owner, start="2026-11-18T00:00:00Z", end="2026-11-19T00:00:00Z")
        self.assertTrue(all("2026-11-17" <= i["at"] < "2026-11-18" for i in day1["items"]))
        self.assertEqual([(i["task_id"], i["kind"]) for i in day2["items"]], [(self.internal, "progress")])
        self.assertNotIn("t-ws2", self.ids(day1))

    def test_requester_timeline_is_own_milestones_without_sources(self):
        tl = views.timeline(self.db, actor=self.gao, start="2026-11-01T00:00:00Z", end="2026-12-01T00:00:00Z")
        self.assertEqual(self.ids(tl), {self.mine})
        self.assertTrue(all("source_ref" not in i and "evidence_type" not in i for i in tl["items"]))
        self.assertFalse(any(i["kind"] == "reminder_set" for i in tl["items"]))

    def test_project_filter_narrows(self):
        tl = views.timeline(self.db, actor=self.owner, start="2026-11-01T00:00:00Z", end="2026-12-01T00:00:00Z",
                            project_id=self.project)
        self.assertEqual(self.ids(tl), {self.mine})


class ProjectPool(ViewFixture):
    def test_owner_pool_groups_tasks_with_executor_and_progress(self):
        pool = {p["id"]: p for p in views.projects(self.db, actor=self.owner)["projects"]}
        q4 = pool[self.project]
        self.assertEqual([t["id"] for t in q4["tasks"]], [self.mine])
        self.assertEqual(q4["tasks"][0]["executor_id"], "owner")
        self.assertEqual(q4["progress"], {"done": 0, "total": 1})
        self.assertEqual({t["id"] for t in pool[None]["tasks"]}, {self.theirs, self.internal})
        self.assertNotIn("p-ws2", pool)

    def test_requester_pool_has_only_own_tasks_and_no_internal_fields(self):
        pool = views.projects(self.db, actor=self.gao)["projects"]
        self.assertEqual([t["id"] for p in pool for t in p["tasks"]], [self.mine])
        task = pool[0]["tasks"][0]
        self.assertNotIn("soft_due_at", task)
        self.assertNotIn("executor_id", task)

    def test_requester_filter(self):
        pool = views.projects(self.db, actor=self.owner, requester_id="li")["projects"]
        self.assertEqual([t["id"] for p in pool for t in p["tasks"]], [self.theirs])


if __name__ == "__main__":
    unittest.main()
