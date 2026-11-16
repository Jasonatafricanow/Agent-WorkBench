"""WorkBench responsibility and progressive-delegation policy.

This module decides who should perform a business step and whether a human
review/decision is required. It is NOT a tool permission system: the Agent
Host must independently authorize every actual tool invocation and side effect.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, IntEnum
from typing import Iterable


class AutonomyLevel(IntEnum):
    """Per-work-class delegation stage, not a global Agent capability score."""

    HUMAN_EXECUTES = 0
    AGENT_PREPARES = 1
    AGENT_SUPERVISED = 2
    AGENT_BOUNDED = 3
    AGENT_EXCEPTION_ONLY = 4


class ActionRoute(str, Enum):
    OUT_OF_SCOPE = "out_of_scope"
    HUMAN_EXECUTION_REQUIRED = "human_execution_required"
    AGENT_PREPARE_ONLY = "agent_prepare_only"
    AGENT_EXECUTE_WITH_REVIEW = "agent_execute_with_review"
    AGENT_EXECUTE = "agent_execute"
    HUMAN_APPROVAL_REQUIRED = "human_approval_required"


@dataclass(frozen=True, slots=True)
class DelegationEnvelope:
    """Effective task envelope, already narrowed against its class profile."""

    work_class: str
    autonomy_level: AutonomyLevel
    profile_max_level: AutonomyLevel
    allowed_actions: frozenset[str]
    reversible_actions: frozenset[str]
    approval_conditions: frozenset[str]
    policy_version: int

    def __post_init__(self) -> None:
        if not self.work_class.strip():
            raise ValueError("work_class is required")
        if self.autonomy_level > self.profile_max_level:
            raise ValueError("task autonomy cannot exceed the approved work-class profile")
        if not self.reversible_actions.issubset(self.allowed_actions):
            raise ValueError("reversible actions must be within the allowed action set")
        if self.policy_version < 1:
            raise ValueError("policy_version must be positive")
        if any(not item.strip() for item in self.allowed_actions | self.approval_conditions):
            raise ValueError("action and condition identifiers must be nonempty")


def route_action(
    envelope: DelegationEnvelope,
    *,
    action: str,
    triggered_conditions: Iterable[str] = (),
) -> ActionRoute:
    """Route a proposed business action; Host tool authorization remains separate."""
    if not isinstance(action, str) or not action.strip():
        raise ValueError("action is required")
    if action not in envelope.allowed_actions:
        return ActionRoute.OUT_OF_SCOPE
    if envelope.approval_conditions.intersection(set(triggered_conditions)):
        return ActionRoute.HUMAN_APPROVAL_REQUIRED

    level = envelope.autonomy_level
    if level == AutonomyLevel.HUMAN_EXECUTES:
        return ActionRoute.HUMAN_EXECUTION_REQUIRED
    if level == AutonomyLevel.AGENT_PREPARES:
        return ActionRoute.AGENT_PREPARE_ONLY
    if level == AutonomyLevel.AGENT_SUPERVISED:
        if action not in envelope.reversible_actions:
            return ActionRoute.HUMAN_APPROVAL_REQUIRED
        return ActionRoute.AGENT_EXECUTE_WITH_REVIEW
    return ActionRoute.AGENT_EXECUTE


@dataclass(frozen=True, slots=True)
class PromotionGate:
    """Explicit, human-approved criteria stored in a versioned class profile."""

    min_verified_successes: int
    min_acceptance_ratio: float
    max_rework_ratio: float
    max_policy_violations: int = 0
    auto_promotion_enabled: bool = False

    def __post_init__(self) -> None:
        if self.min_verified_successes < 1 or self.max_policy_violations < 0:
            raise ValueError("promotion counts are invalid")
        if not 0.0 <= self.min_acceptance_ratio <= 1.0:
            raise ValueError("min_acceptance_ratio must be between 0 and 1")
        if not 0.0 <= self.max_rework_ratio <= 1.0:
            raise ValueError("max_rework_ratio must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class PromotionEvidence:
    completed_runs: int
    verified_successes: int
    accepted_without_rework: int
    rework_count: int
    policy_violations: int
    evidence_complete: bool


@dataclass(frozen=True, slots=True)
class PromotionAssessment:
    criteria_met: bool
    can_apply: bool
    requires_human_approval: bool
    reasons: tuple[str, ...]


def assess_promotion(
    *,
    current_level: AutonomyLevel,
    target_level: AutonomyLevel,
    profile_max_level: AutonomyLevel,
    gate: PromotionGate,
    evidence: PromotionEvidence,
    human_approved: bool = False,
) -> PromotionAssessment:
    """Assess promotion from verified outcomes; never use model confidence."""
    reasons: list[str] = []
    if target_level <= current_level:
        reasons.append("target level must be higher than current level")
    if target_level > profile_max_level:
        reasons.append("target level exceeds the approved work-class ceiling")
    if not evidence.evidence_complete:
        reasons.append("required execution evidence is incomplete")
    if evidence.completed_runs < 1:
        reasons.append("no completed runs to evaluate")
    if evidence.verified_successes < gate.min_verified_successes:
        reasons.append("verified success count is below the configured threshold")
    if evidence.accepted_without_rework > evidence.verified_successes:
        reasons.append("accepted outcomes exceed verified successes")
    if evidence.verified_successes > 0:
        acceptance = evidence.accepted_without_rework / evidence.verified_successes
        if acceptance < gate.min_acceptance_ratio:
            reasons.append("verified acceptance ratio is below the configured threshold")
    if evidence.completed_runs > 0:
        rework = evidence.rework_count / evidence.completed_runs
        if rework > gate.max_rework_ratio:
            reasons.append("rework ratio exceeds the configured maximum")
    if evidence.policy_violations > gate.max_policy_violations:
        reasons.append("policy violations exceed the configured maximum")

    criteria_met = not reasons
    requires_human = criteria_met and not gate.auto_promotion_enabled and not human_approved
    can_apply = criteria_met and (human_approved or gate.auto_promotion_enabled)
    return PromotionAssessment(
        criteria_met=criteria_met,
        can_apply=can_apply,
        requires_human_approval=requires_human,
        reasons=tuple(reasons),
    )
