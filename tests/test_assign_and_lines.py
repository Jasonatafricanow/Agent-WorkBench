"""11-22: the owner hands a new task to someone like sending a mail (it waits on their answer, and the
message to them is queued in delivery_outbox); the timeline shows one task as one line."""
import json

import test_owner_console
from test_framework import Base

from workdesk import ActorContext, views
from workdesk.errors import Forbidden


class AssignTests(test_owner_console.OwnerConsoleTests):

    def assign(self, nonce="a1", to="gao", **extra):
        return self.post_form(self.form(nonce=nonce, action="create", title="充电宝售后", assign_to=to,
                                        ask="帮忙联系客服换货", **extra))

    def test_assigned_task_waits_on_the_person_and_queues_their_message(self):
        self.assertIn("指派给 Gao", self.get("/", self.owner_auth))
        status, location = self.assign()
        self.assertIn("ok=", location)
        (task, state), = self.q("SELECT id, state FROM tasks WHERE title='充电宝售后'")
        self.assertEqual(state, "WAITING")
        self.assertEqual(self.q("SELECT kind, responsible_principal_id, status FROM dependencies WHERE task_id=?", task),
                         [("external_response", "gao", "open")])
        (recipient, channel, status, payload), = self.q(
            "SELECT recipient_principal_id, channel, status, payload_json FROM delivery_outbox WHERE task_id=?", task)
        self.assertEqual((recipient, channel, status), ("gao", "telegram", "pending"))
        self.assertIn("充电宝售后", json.loads(payload)["text"])
        self.assertIn("帮忙联系客服换货", json.loads(payload)["text"])

        # Her answer (through the work bot, citing her own message) releases the wait.
        req, = self.q("SELECT id FROM human_action_requests WHERE task_id=?", task)
        gao = ActorContext("ws", "gao", "requester", "telegram")
        self.apply(gao, "answer", {"op": "respond_human_action", "human_action_id": req[0], "response": {"text": "好的"},
                                   "evidence_type": "explicit_user", "source_ref": self.inbox_from("gao")})
        self.assertEqual(self.q("SELECT status FROM dependencies WHERE task_id=?", task), [("resolved",)])
        self.assertEqual(self.q("SELECT state FROM tasks WHERE id=?", task), [("READY",)])

    def test_cannot_assign_to_someone_outside_the_workspace(self):
        status, location = self.assign(to="stranger")
        self.assertIn("err=", location)
        self.assertEqual(self.q("SELECT id FROM tasks WHERE title='充电宝售后'"), [])
        self.assertEqual(self.q("SELECT COUNT(*) FROM delivery_outbox")[0][0], 0)

    def test_owner_pages_say_owner_mode_viewer_pages_say_read_only(self):
        self.assertIn("主人模式", self.get("/timeline", self.owner_auth))
        self.assertIn("只读视图", self.get("/timeline", self.viewer_auth))


# Only the new tests: the inherited OwnerConsoleTests already run in their own module.
for _name in [n for n in dir(test_owner_console.OwnerConsoleTests) if n.startswith("test_")]:
    setattr(AssignTests, _name, None)


class OutsiderGate(Base):
    def test_the_agent_cannot_message_an_outsider_about_the_owners_own_task(self):
        task = self.task()
        with self.assertRaises(Forbidden):
            self.apply(self.agent, "ask", {"op": "request_human_action", "task_id": task, "expected_version": 1,
                                           "assignee_principal_id": "gao", "request_kind": "input", "title": "x",
                                           "urgency": "notify", "evidence_type": "inferred"})


class TaskLines(Base):
    def test_one_task_is_one_line_and_changes_made_together_are_one_step(self):
        task = self.task()  # created on 11-17
        self.apply(self.owner, "w", {"op": "add_dependency", "ref": "d", "task_id": task, "expected_version": 1,
                                     "kind": "other", "awaited_outcome": "x", "evidence_type": "explicit_user",
                                     "source_ref": "owner-web:1"},
                   {"op": "transition", "task_id": task, "to": "WAITING", "evidence_type": "explicit_user",
                    "source_ref": "owner-web:1"})
        self.clock.now = "2026-11-18T03:00:00Z"
        self.apply(self.owner, "p", {"op": "append_progress", "task_id": task, "expected_version": 2, "text": "催了",
                                     "evidence_type": "explicit_user"})
        tl = views.timeline(self.db, actor=self.owner, start="2026-11-17T00:00:00Z", end="2026-11-19T00:00:00Z")
        line, = tl["lines"]
        self.assertEqual(line["task_id"], task)
        self.assertEqual([d["date"] for d in line["days"]], ["2026-11-17", "2026-11-18"])
        first_day_steps = line["days"][0]["steps"]
        self.assertEqual([s["kind"] for s in first_day_steps[-1]["stages"]], ["created", "dependency_added", "state_changed"])
        self.assertEqual(first_day_steps[-1]["stages"][-1]["to_state"], "WAITING")
        later = views.timeline(self.db, actor=self.owner, start="2026-11-18T00:00:00Z", end="2026-11-19T00:00:00Z")
        self.assertTrue(later["lines"][0]["earlier"])


class WaitingInOneOp(Base):
    """11-21 Xiyue: '已交高老师处理，等她反馈' took three tries; it is one op now."""

    def test_create_task_waiting_on_someone(self):
        out = self.apply(self.owner, "c", {"op": "create_task", "title": "充电宝售后", "evidence_type": "explicit_user",
                                           "waiting_on": {"awaited_outcome": "高老师反馈", "responsible_principal_id": "gao"}})[0]
        self.assertEqual(self.q("SELECT state FROM tasks WHERE id=?", out["task_id"]), [("WAITING",)])
        self.assertEqual(self.q("SELECT kind, responsible_principal_id, status FROM dependencies WHERE task_id=?", out["task_id"]),
                         [("external_response", "gao", "open")])

    def test_transition_to_waiting_with_waiting_on(self):
        task = self.task()
        self.apply(self.owner, "w", {"op": "transition", "task_id": task, "expected_version": 1, "to": "WAITING",
                                     "waiting_on": {"awaited_outcome": "对方回复"}, "evidence_type": "explicit_user"})
        self.assertEqual(self.q("SELECT state FROM tasks WHERE id=?", task), [("WAITING",)])

    def test_waiting_without_saying_on_what_is_still_refused(self):
        from workdesk.errors import InvalidOperation
        with self.assertRaises(InvalidOperation):
            self.apply(self.owner, "c", {"op": "create_task", "title": "x", "state": "WAITING", "evidence_type": "explicit_user"})
