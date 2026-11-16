"""Progressive delegation policy tests: no model, network or credentials."""
import unittest

from workdesk.delegation import (
    ActionRoute,
    AutonomyLevel,
    DelegationEnvelope,
    PromotionEvidence,
    PromotionGate,
    assess_promotion,
    route_action,
)


def envelope(level, *, max_level=AutonomyLevel.AGENT_EXCEPTION_ONLY):
    return DelegationEnvelope(
        work_class="routine_reporting",
        autonomy_level=level,
        profile_max_level=max_level,
        profile_allowed_actions=frozenset({
            "read_data", "draft_report", "send_internal_update", "send_external",
        }),
        profile_reversible_actions=frozenset({"read_data", "draft_report"}),
        profile_approval_conditions=frozenset({
            "new_recipient", "external_commitment", "financial_action",
        }),
        allowed_actions=frozenset({"read_data", "draft_report", "send_internal_update"}),
        reversible_actions=frozenset({"read_data", "draft_report"}),
        approval_conditions=frozenset({
            "new_recipient", "external_commitment", "financial_action",
        }),
        review_actions=frozenset({"send_internal_update"}),
        policy_version=3,
    )


class DelegationPolicyTests(unittest.TestCase):
    def test_human_execution_stage_never_executes_for_agent(self):
        self.assertEqual(
            route_action(envelope(AutonomyLevel.HUMAN_EXECUTES), action="draft_report"),
            ActionRoute.HUMAN_EXECUTION_REQUIRED,
        )

    def test_agent_prepares_but_human_commits(self):
        self.assertEqual(
            route_action(envelope(AutonomyLevel.AGENT_PREPARES), action="send_internal_update"),
            ActionRoute.AGENT_PREPARE_ONLY,
        )

    def test_supervised_agent_can_only_execute_reversible_actions(self):
        policy = envelope(AutonomyLevel.AGENT_SUPERVISED)
        self.assertEqual(route_action(policy, action="draft_report"),
                         ActionRoute.AGENT_EXECUTE_WITH_REVIEW)
        self.assertEqual(route_action(policy, action="send_internal_update"),
                         ActionRoute.HUMAN_APPROVAL_REQUIRED)

    def test_bounded_and_exception_only_levels_stay_inside_allowlist(self):
        for level in (AutonomyLevel.AGENT_BOUNDED, AutonomyLevel.AGENT_EXCEPTION_ONLY):
            self.assertEqual(route_action(envelope(level), action="draft_report"),
                             ActionRoute.AGENT_EXECUTE)
            self.assertEqual(route_action(envelope(level), action="send_to_press"),
                             ActionRoute.OUT_OF_SCOPE)

    def test_triggered_approval_condition_overrides_autonomy_level(self):
        result = route_action(envelope(AutonomyLevel.AGENT_EXCEPTION_ONLY),
                              action="send_internal_update",
                              triggered_conditions={"external_commitment"})
        self.assertEqual(result, ActionRoute.HUMAN_APPROVAL_REQUIRED)

    def test_task_cannot_broaden_profile_actions_or_remove_approval_conditions(self):
        args = dict(
            work_class="routine_reporting",
            autonomy_level=AutonomyLevel.AGENT_BOUNDED,
            profile_max_level=AutonomyLevel.AGENT_EXCEPTION_ONLY,
            profile_allowed_actions=frozenset({"read_data"}),
            profile_reversible_actions=frozenset({"read_data"}),
            profile_approval_conditions=frozenset({"external_commitment"}),
            allowed_actions=frozenset({"read_data", "send_external"}),
            reversible_actions=frozenset({"read_data"}),
            approval_conditions=frozenset(),
            review_actions=frozenset(),
            policy_version=1,
        )
        with self.assertRaises(ValueError):
            DelegationEnvelope(**args)

    def test_bounded_level_can_require_post_execution_review_for_selected_actions(self):
        self.assertEqual(
            route_action(envelope(AutonomyLevel.AGENT_BOUNDED), action="send_internal_update"),
            ActionRoute.AGENT_EXECUTE_WITH_REVIEW,
        )

    def test_task_level_cannot_exceed_profile_ceiling(self):
        with self.assertRaises(ValueError):
            envelope(AutonomyLevel.AGENT_EXCEPTION_ONLY,
                     max_level=AutonomyLevel.AGENT_BOUNDED)

    def test_promotion_requires_complete_verified_evidence_and_human_by_default(self):
        gate = PromotionGate(5, 0.9, 0.1)
        weak = PromotionEvidence(6, 4, 4, 0, 0, True)
        self.assertFalse(assess_promotion(
            current_level=AutonomyLevel.AGENT_PREPARES,
            target_level=AutonomyLevel.AGENT_SUPERVISED,
            profile_max_level=AutonomyLevel.AGENT_EXCEPTION_ONLY,
            gate=gate, evidence=weak,
        ).criteria_met)
        strong = PromotionEvidence(6, 6, 6, 0, 0, True)
        result = assess_promotion(
            current_level=AutonomyLevel.AGENT_PREPARES,
            target_level=AutonomyLevel.AGENT_SUPERVISED,
            profile_max_level=AutonomyLevel.AGENT_EXCEPTION_ONLY,
            gate=gate, evidence=strong,
        )
        self.assertTrue(result.criteria_met)
        self.assertTrue(result.requires_human_approval)
        self.assertFalse(result.can_apply)

    def test_promotion_never_uses_unverified_or_violating_runs(self):
        gate = PromotionGate(5, 0.9, 0.1)
        evidence = PromotionEvidence(6, 6, 6, 0, 1, True)
        result = assess_promotion(
            current_level=AutonomyLevel.AGENT_PREPARES,
            target_level=AutonomyLevel.AGENT_SUPERVISED,
            profile_max_level=AutonomyLevel.AGENT_EXCEPTION_ONLY,
            gate=gate, evidence=evidence, human_approved=True,
        )
        self.assertFalse(result.can_apply)
        self.assertTrue(any("policy violations" in x for x in result.reasons))

    def test_invalid_negative_evidence_is_rejected(self):
        with self.assertRaises(ValueError):
            PromotionEvidence(3, 2, 2, -1, 0, True)

    def test_auto_promotion_requires_explicit_profile_opt_in(self):
        evidence = PromotionEvidence(6, 6, 6, 0, 0, True)
        args = dict(
            current_level=AutonomyLevel.AGENT_PREPARES,
            target_level=AutonomyLevel.AGENT_SUPERVISED,
            profile_max_level=AutonomyLevel.AGENT_EXCEPTION_ONLY,
            evidence=evidence,
        )
        default = assess_promotion(gate=PromotionGate(5, 0.9, 0.1), **args)
        opted_in = assess_promotion(
            gate=PromotionGate(5, 0.9, 0.1, auto_promotion_enabled=True), **args
        )
        self.assertFalse(default.can_apply)
        self.assertTrue(opted_in.can_apply)
        self.assertFalse(opted_in.requires_human_approval)


if __name__ == "__main__":
    unittest.main()
