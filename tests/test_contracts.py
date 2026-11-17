"""Self-contained host-tool contract tests; no model or network."""
import unittest
from dataclasses import dataclass
from workdesk.contracts import ActorContext, WorkbenchTools


class FakeTasks:
    def __init__(self):
        self.calls = []
    def overview(self, *, actor, limit):
        self.calls.append(("overview", actor, limit))
        return {"tasks": []}
    def read(self, *, actor, task_id):
        self.calls.append(("read", actor, task_id))
        return {"id": task_id}
    def apply(self, *, actor, idempotency_key, operations):
        self.calls.append(("apply", actor, idempotency_key, operations))
        return [{"accepted": True}]


class HostToolTests(unittest.TestCase):
    def setUp(self):
        self.actor = ActorContext("workspace-1", "person-1", "requester", "telegram")
        self.svc = FakeTasks()
        self.tools = WorkbenchTools(self.actor, self.svc)

    def test_actor_is_pinned_for_every_call(self):
        self.tools.list_tasks(limit=12)
        self.tools.get_task(task_id="task-1")
        self.tools.change_tasks(idempotency_key="msg-1", operations=[{"op": "create_task"}])
        for call in self.svc.calls:
            self.assertEqual(call[1], self.actor)

    def test_invalid_host_context_fails_closed(self):
        with self.assertRaises(ValueError):
            ActorContext("", "person-1", "owner", "telegram")

    def test_invalid_agent_commands_rejected_before_business_layer(self):
        with self.assertRaises(ValueError):
            self.tools.change_tasks(idempotency_key="", operations=[{"op": "create_task"}])
        with self.assertRaises(ValueError):
            self.tools.list_tasks(limit=1000)
        self.assertEqual(self.svc.calls, [])


if __name__ == "__main__":
    unittest.main()
