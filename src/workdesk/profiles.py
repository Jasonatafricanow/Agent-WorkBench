"""Owner-only administration of versioned delegation profiles.

Deliberately NOT exposed through `WorkbenchTools`: no model-facing tool may set
an autonomy ceiling, an action allowlist, approval conditions or auto-promotion.
Call this only from trusted owner surfaces (CLI, authenticated dashboard form).
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Callable

from .contracts import ActorContext
from .delegation import AutonomyLevel
from .errors import Forbidden, InvalidOperation
from .storage import utc_now, write_transaction

LEVELS = tuple(level.name.lower() for level in AutonomyLevel)


def _actions(value, field: str) -> list[str]:
    value = value or []
    if not isinstance(value, list) or any(not isinstance(v, str) or not v.strip() for v in value):
        raise InvalidOperation(f"{field} must be a list of nonempty strings", field=field)
    return sorted(set(value))


class DelegationProfileAdmin:
    def __init__(self, database: str | Path, *, clock: Callable[[], str] = utc_now):
        self._database = str(database)
        self._clock = clock

    def publish(self, *, owner: ActorContext, work_class: str, max_autonomy_level: str,
                allowed_actions: list[str], reversible_actions: list[str] | None = None,
                review_actions: list[str] | None = None, approval_conditions: list[str] | None = None,
                success_criteria: list[str] | None = None, evidence_requirements: list[str] | None = None,
                escalation_policy: dict | None = None, promotion_policy: dict | None = None,
                auto_promotion_enabled: bool = False) -> dict:
        """Publish a new immutable version and retire the previous active one."""
        if not isinstance(work_class, str) or not work_class.strip():
            raise InvalidOperation("work_class is required")
        if max_autonomy_level not in LEVELS:
            raise InvalidOperation("unsupported max_autonomy_level", allowed=list(LEVELS))
        allowed = _actions(allowed_actions, "allowed_actions")
        reversible = _actions(reversible_actions, "reversible_actions")
        review = _actions(review_actions, "review_actions")
        if not set(reversible) <= set(allowed) or not set(review) <= set(allowed):
            raise InvalidOperation("reversible and review actions must be within allowed_actions")
        now = self._clock()
        with write_transaction(self._database) as conn:
            row = conn.execute(
                """SELECT m.role, p.kind FROM memberships m JOIN principals p ON p.id=m.principal_id
                   WHERE m.workspace_id=? AND m.principal_id=? AND m.active=1 AND p.active=1""",
                (owner.workspace_id, owner.principal_id)).fetchone()
            if row is None or row["role"] != "owner" or row["kind"] != "human":
                raise Forbidden("only the human workspace owner may publish delegation profiles")
            version = conn.execute(
                "SELECT COALESCE(MAX(version),0)+1 FROM delegation_profiles WHERE workspace_id=? AND work_class=?",
                (owner.workspace_id, work_class)).fetchone()[0]
            conn.execute(
                """UPDATE delegation_profiles SET status='retired', updated_at=?
                   WHERE workspace_id=? AND work_class=? AND status='active'""",
                (now, owner.workspace_id, work_class))
            profile_id = f"profile-{uuid.uuid4().hex}"
            dumps = lambda v: json.dumps(v, ensure_ascii=False, sort_keys=True)
            conn.execute(
                """INSERT INTO delegation_profiles(id,workspace_id,work_class,version,max_autonomy_level,
                     allowed_actions_json,reversible_actions_json,review_actions_json,
                     mandatory_approval_conditions_json,success_criteria_json,evidence_requirements_json,
                     escalation_policy_json,promotion_policy_json,auto_promotion_enabled,
                     created_by_principal_id,approved_by_principal_id,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (profile_id, owner.workspace_id, work_class, version, max_autonomy_level,
                 dumps(allowed), dumps(reversible), dumps(review),
                 dumps(_actions(approval_conditions, "approval_conditions")),
                 dumps(_actions(success_criteria, "success_criteria")),
                 dumps(_actions(evidence_requirements, "evidence_requirements")),
                 dumps(escalation_policy or {}), dumps(promotion_policy or {}),
                 1 if auto_promotion_enabled else 0, owner.principal_id, owner.principal_id, now, now))
            return {"profile_id": profile_id, "work_class": work_class, "version": version}
