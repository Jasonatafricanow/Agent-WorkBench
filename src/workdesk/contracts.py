"""Host-facing contracts. None of these interfaces authenticate requests themselves.

The HTTP/Telegram transport authenticates requests; IdentityProvider must resolve
the principal from verified transport metadata before WorkbenchTools is constructed.
A model must never set its own actor_id, workspace_id, role, or access scope.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class ActorContext:
    workspace_id: str
    principal_id: str
    role: str
    source: str

    def __post_init__(self) -> None:
        if not all(isinstance(v, str) and v.strip() for v in (
            self.workspace_id, self.principal_id, self.role, self.source,
        )):
            raise ValueError("authenticated actor context fields are required")


class IdentityProvider(Protocol):
    def resolve_authenticated(
        self, *, provider: str, connection_id: str,
        external_user_id: str, workspace_id: str
    ) -> ActorContext: ...


class TaskService(Protocol):
    def overview(self, *, actor: ActorContext, limit: int) -> dict[str, Any]: ...
    def read(self, *, actor: ActorContext, task_id: str) -> dict[str, Any]: ...
    def apply(
        self, *, actor: ActorContext, idempotency_key: str,
        operations: list[dict[str, Any]],
    ) -> list[dict[str, Any]]: ...


@dataclass(frozen=True, slots=True)
class WorkbenchTools:
    """A narrow agent-facing facade; only host code may construct the actor."""

    actor: ActorContext
    tasks: TaskService

    def list_tasks(self, *, limit: int = 100) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("limit out of range")
        return self.tasks.overview(actor=self.actor, limit=limit)

    def get_task(self, *, task_id: str) -> dict[str, Any]:
        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError("task_id is required")
        return self.tasks.read(actor=self.actor, task_id=task_id)

    def change_tasks(
        self, *, idempotency_key: str, operations: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("idempotency_key required")
        if not isinstance(operations, list) or not operations or len(operations) > 40:
            raise ValueError("operations batch must contain 1 to 40 items")
        if any(not isinstance(op, dict) for op in operations):
            raise ValueError("operations must be objects")
        # ACL, event provenance and task version checks belong in TaskService.
        return self.tasks.apply(
            actor=self.actor, idempotency_key=idempotency_key,
            operations=operations,
        )
