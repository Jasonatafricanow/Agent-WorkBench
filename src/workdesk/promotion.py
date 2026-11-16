"""Evidence-based delegation review (ADR-0003 §6).

Evidence comes only from recorded outcomes, never from model confidence:
- a run      = a deliverable of this work class that reached SUBMITTED with a verified receipt;
- a success  = such a deliverable later ACCEPTED by the recipient's own message;
- no rework  = accepted on its first revision;
- rework     = a REVIEW_REQUIRED / REJECTED step;
- violations = owner overrides, revocations and demotions recorded for this executor and class.

When the profile's gate is met: if the owner pre-enabled auto-promotion, the level
rises one step (never above the profile ceiling) and the change is attributed to the
owner who approved that profile; otherwise one review request goes to the owner.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import asdict
from pathlib import Path

from .delegation import AutonomyLevel, PromotionEvidence, PromotionGate, assess_promotion
from .storage import write_transaction

LEVELS = tuple(level.name.lower() for level in AutonomyLevel)


def collect_evidence(conn, workspace_id: str, work_class: str, executor_id: str) -> PromotionEvidence:
    tasks = [r[0] for r in conn.execute(
        """SELECT DISTINCT task_id FROM responsibility_events WHERE workspace_id=? AND to_actor_id=?
           AND task_id IN (SELECT task_id FROM task_responsibilities WHERE workspace_id=? AND work_class=?)""",
        (workspace_id, executor_id, workspace_id, work_class))]
    if not tasks:
        return PromotionEvidence(0, 0, 0, 0, 0, False)
    marks = ",".join("?" * len(tasks))
    rows = conn.execute(
        f"""SELECT status, revision, submitted_at FROM deliverables WHERE workspace_id=? AND task_id IN ({marks})""",
        (workspace_id, *tasks)).fetchall()
    runs = sum(1 for r in rows if r["submitted_at"])
    accepted = [r for r in rows if r["status"] == "ACCEPTED"]
    rework = conn.execute(
        f"""SELECT COUNT(*) FROM task_events WHERE workspace_id=? AND task_id IN ({marks})
            AND kind IN ('deliverable_review_required','deliverable_rejected')""",
        (workspace_id, *tasks)).fetchone()[0]
    violations = conn.execute(
        f"""SELECT COUNT(*) FROM responsibility_events WHERE workspace_id=? AND task_id IN ({marks})
            AND from_actor_id=? AND event_type IN ('override','revoked','demoted')""",
        (workspace_id, *tasks, executor_id)).fetchone()[0]
    return PromotionEvidence(
        completed_runs=runs, verified_successes=min(len(accepted), runs),
        accepted_without_rework=min(sum(1 for r in accepted if r["revision"] == 1), len(accepted), runs),
        rework_count=min(rework, runs), policy_violations=violations, evidence_complete=runs > 0)


def review_delegations(database: str | Path, *, now: str) -> list[dict]:
    out = []
    with write_transaction(database) as conn:
        rows = conn.execute(
            """SELECT r.*, p.max_autonomy_level, p.promotion_policy_json, p.auto_promotion_enabled,
                      p.approved_by_principal_id, p.status AS profile_status, x.kind AS executor_kind
               FROM task_responsibilities r
               JOIN delegation_profiles p ON p.workspace_id=r.workspace_id AND p.id=r.profile_id
               JOIN principals x ON x.id=r.current_executor_id
               JOIN tasks t ON t.workspace_id=r.workspace_id AND t.id=r.task_id
               WHERE r.status='active' AND x.kind='agent' AND t.state NOT IN ('DONE','CANCELLED')""").fetchall()
        for r in rows:
            policy = json.loads(r["promotion_policy_json"])
            if r["profile_status"] != "active" or "min_verified_successes" not in policy:
                continue
            current = AutonomyLevel[r["autonomy_level"].upper()]
            ceiling = AutonomyLevel[r["max_autonomy_level"].upper()]
            if current >= ceiling:
                continue
            gate = PromotionGate(
                min_verified_successes=int(policy["min_verified_successes"]),
                min_acceptance_ratio=float(policy.get("min_acceptance_ratio", 1.0)),
                max_rework_ratio=float(policy.get("max_rework_ratio", 0.0)),
                max_policy_violations=int(policy.get("max_policy_violations", 0)),
                auto_promotion_enabled=bool(r["auto_promotion_enabled"]))
            evidence = collect_evidence(conn, r["workspace_id"], r["work_class"], r["current_executor_id"])
            target = AutonomyLevel(current + 1)
            result = assess_promotion(current_level=current, target_level=target, profile_max_level=ceiling,
                                      gate=gate, evidence=evidence)
            if not result.criteria_met:
                continue
            evidence_json = json.dumps(asdict(evidence), sort_keys=True)
            if result.can_apply:
                level = target.name.lower()
                conn.execute(
                    """UPDATE task_responsibilities SET autonomy_level=?, version=version+1, updated_by_principal_id=?,
                         updated_at=? WHERE workspace_id=? AND task_id=?""",
                    (level, r["approved_by_principal_id"], now, r["workspace_id"], r["task_id"]))
                conn.execute(
                    """INSERT INTO responsibility_events(id,workspace_id,task_id,event_type,from_actor_id,to_actor_id,
                         from_autonomy_level,to_autonomy_level,profile_id,profile_version,actor_id,reason,evidence_json,
                         occurred_at,recorded_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (f"resp-{uuid.uuid4().hex}", r["workspace_id"], r["task_id"], "promoted",
                     r["current_executor_id"], r["current_executor_id"], r["autonomy_level"], level,
                     r["profile_id"], r["profile_version"], r["approved_by_principal_id"],
                     f"auto-promotion pre-authorized in profile v{r['profile_version']}", evidence_json, now, now))
                out.append({"task_id": r["task_id"], "promoted_to": level})
                continue
            pending = conn.execute(
                """SELECT 1 FROM human_action_requests WHERE workspace_id=? AND task_id=? AND status='pending'
                   AND json_extract(request_json,'$.kind')='promotion_review'""",
                (r["workspace_id"], r["task_id"])).fetchone()
            if pending:
                continue
            owner = conn.execute("SELECT owner_id FROM workspaces WHERE id=?", (r["workspace_id"],)).fetchone()[0]
            conn.execute(
                """INSERT INTO human_action_requests(id,workspace_id,task_id,requested_by_principal_id,assignee_principal_id,
                     request_kind,title,request_json,urgency,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (f"hum-{uuid.uuid4().hex}", r["workspace_id"], r["task_id"], r["current_executor_id"], owner, "review",
                 f"委托升级建议：{r['work_class']} {r['autonomy_level']} → {target.name.lower()}",
                 json.dumps({"kind": "promotion_review", "target_level": target.name.lower(),
                             "evidence": json.loads(evidence_json)}, ensure_ascii=False), "batch", now, now))
            out.append({"task_id": r["task_id"], "review_requested": target.name.lower()})
    return out
