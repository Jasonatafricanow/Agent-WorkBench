"""WB-LIFE-01 P0: official and soft dues are separate calendar markers; load counts a task once."""
import unittest

from test_framework import NOW, Base

from workdesk.insights import schedule_load


class TwoDeadlines(Base):
    def load(self, **kw):
        return schedule_load(str(self.db), actor=self.owner, now=NOW, **kw)

    def markers(self, load, task):
        return sorted((d["date"], i["deadline"], i["counts"]) for d in load["days"] for i in d["items"] if i.get("id") == task)

    def test_both_deadlines_are_on_the_calendar_and_the_task_counts_once(self):
        t = self.task(official_due_at="2026-11-25T09:00:00Z", soft_due_at="2026-11-20T09:00:00Z")
        load = self.load()
        self.assertEqual(self.markers(load, t), [("2026-11-20", "soft", True), ("2026-11-25", "official", False)])
        self.assertEqual(sum(d["load"] for d in load["days"]), 1)

    def test_same_day_keeps_both_markers_without_double_load(self):
        t = self.task(official_due_at="2026-11-20T10:00:00Z", soft_due_at="2026-11-20T02:00:00Z")
        load = self.load()
        self.assertEqual(self.markers(load, t), [("2026-11-20", "official", False), ("2026-11-20", "soft", True)])
        self.assertEqual([d["load"] for d in load["days"]], [1])

    def test_official_only_task_counts_on_its_official_day_and_is_never_moved(self):
        for i in range(4):
            self.task(key=f"o{i}", official_due_at="2026-11-20T09:00:00Z")
        load = self.load(max_per_day=3)
        self.assertEqual(load["overloaded_days"], ["2026-11-20"])
        self.assertEqual(load["suggestions"], [])  # nothing movable: official dues are fixed

    def test_suggestions_move_soft_due_only_even_when_task_also_has_official(self):
        ids = [self.task(key=f"s{i}", official_due_at="2026-11-27T09:00:00Z", soft_due_at="2026-11-20T09:00:00Z")
               for i in range(4)]
        load = self.load(max_per_day=3)
        self.assertEqual(load["overloaded_days"], ["2026-11-20"])
        self.assertEqual(len(load["suggestions"]), 1)  # previously 0: having an official due blocked every suggestion
        s = load["suggestions"][0]
        self.assertIn(s["task_id"], ids)
        self.assertEqual(s["from"], "2026-11-20")
        self.assertLess(s["to"], "2026-11-20")
        # Suggestions are never written: both dues are unchanged.
        row = self.q("SELECT official_due_at, soft_due_at FROM tasks WHERE id=?", s["task_id"])[0]
        self.assertEqual(row, ("2026-11-27T09:00:00Z", "2026-11-20T09:00:00Z"))

    def test_human_action_load_is_kept(self):
        t = self.task(soft_due_at="2026-11-20T09:00:00Z")
        self.apply(self.agent, "ha", {"op": "request_human_action", "task_id": t, "expected_version": 1,
                                      "request_kind": "decision", "title": "pick", "urgency": "batch",
                                      "due_at": "2026-11-20T08:00:00Z", "evidence_type": "system"})
        day = [d for d in self.load()["days"] if d["date"] == "2026-11-20"][0]
        self.assertEqual(day["load"], 2)
        self.assertEqual(sorted(i["kind"] for i in day["items"]), ["human_action", "task"])


if __name__ == "__main__":
    unittest.main()
