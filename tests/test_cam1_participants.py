# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import unittest

from tools.cam1lib.participants import (
    ParticipantRoster,
    ParticipantStatus,
    RouteStatus,
)
from tools.cam1lib.protocol import CamUsageError

PROJECT_ID = "00000000-0000-4000-8000-000000000001"
REVIEWER_ID = "00000000-0000-4000-8000-000000000101"
ANALYST_ID = "00000000-0000-4000-8000-000000000102"
REVIEWER_SESSION = "00000000-0000-4000-8000-000000000201"
ANALYST_SESSION = "00000000-0000-4000-8000-000000000202"
OBSERVED_AT = "2026-08-27T17:00:00Z"


def roster_with_reviewer() -> ParticipantRoster:
    roster = ParticipantRoster(PROJECT_ID)
    roster.add(
        participant_id=REVIEWER_ID,
        common_name="reviewer",
        display_name="Example Reviewer",
        role="code review",
        vendor="claude-code",
    )
    return roster


def bind_reviewer(roster: ParticipantRoster) -> None:
    roster.bind(
        "reviewer",
        session_id=REVIEWER_SESSION,
        session_label="example-review-session",
        session_kind="background",
        operator_reference="direct local confirmation",
        bound_at=OBSERVED_AT,
    )


class ParticipantRosterTests(unittest.TestCase):
    def test_common_name_is_project_scoped_and_unique(self) -> None:
        first = roster_with_reviewer()
        second = roster_with_reviewer()
        self.assertEqual(first.project_id, second.project_id)

        with self.assertRaises(CamUsageError) as context:
            first.add(
                common_name="reviewer",
                display_name="Another",
                role="different session",
                vendor="claude-code",
            )
        self.assertEqual(context.exception.code, "roster.name_conflict")

    def test_common_names_and_participant_ids_cannot_cross_collide(self) -> None:
        roster = ParticipantRoster(PROJECT_ID)
        roster.add(
            participant_id=REVIEWER_ID,
            common_name="reviewer",
            display_name="Example Reviewer",
            role="code review",
            vendor="claude-code",
        )
        with self.assertRaises(CamUsageError) as name_collision:
            roster.add(
                participant_id=ANALYST_ID,
                common_name=REVIEWER_ID,
                display_name="Ambiguous",
                role="must be rejected",
                vendor="codex",
            )
        self.assertEqual(name_collision.exception.code, "roster.name_conflict")

        uuid_named = ParticipantRoster(PROJECT_ID)
        uuid_named.add(
            participant_id=ANALYST_ID,
            common_name=REVIEWER_ID,
            display_name="UUID-shaped name",
            role="temporary test participant",
            vendor="codex",
        )
        with self.assertRaises(CamUsageError) as identifier_collision:
            uuid_named.add(
                participant_id=REVIEWER_ID,
                common_name="reviewer",
                display_name="Ambiguous",
                role="must be rejected",
                vendor="claude-code",
            )
        self.assertEqual(
            identifier_collision.exception.code,
            "roster.identifier_conflict",
        )

    def test_duplicate_product_session_labels_are_allowed(self) -> None:
        roster = roster_with_reviewer()
        roster.add(
            participant_id=ANALYST_ID,
            common_name="analyst",
            display_name="Example Analyst",
            role="analysis review",
            vendor="claude-code",
        )
        bind_reviewer(roster)
        analyst = roster.bind(
            "analyst",
            session_id=ANALYST_SESSION,
            session_label="example-review-session",
            session_kind="background",
            operator_reference="direct local confirmation",
            bound_at=OBSERVED_AT,
        )
        self.assertEqual(
            analyst.binding.session_label,
            "example-review-session",
        )

    def test_retired_common_name_is_not_silently_reused(self) -> None:
        roster = roster_with_reviewer()
        roster.retire("reviewer", reason="session was intentionally retired")

        with self.assertRaises(CamUsageError) as context:
            roster.add(
                common_name="reviewer",
                display_name="Replacement",
                role="new review session",
                vendor="claude-code",
            )
        self.assertEqual(context.exception.code, "roster.name_conflict")

    def test_one_session_cannot_bind_two_active_participants(self) -> None:
        roster = roster_with_reviewer()
        roster.add(
            participant_id=ANALYST_ID,
            common_name="analyst",
            display_name="Example Analyst",
            role="analysis review",
            vendor="claude-code",
        )
        bind_reviewer(roster)

        with self.assertRaises(CamUsageError) as context:
            roster.bind(
                "analyst",
                session_id=REVIEWER_SESSION,
                session_label="example-analysis-session",
                session_kind="background",
                operator_reference="direct local confirmation",
                bound_at=OBSERVED_AT,
            )
        self.assertEqual(context.exception.code, "roster.session_conflict")

    def test_uds_and_unknown_transports_are_rejected(self) -> None:
        roster = roster_with_reviewer()
        bind_reviewer(roster)

        with self.assertRaises(CamUsageError) as context:
            roster.observe_route(
                "reviewer",
                transport="uds",
                address="uds:/tmp/cc-socks/123.sock",
                source="status",
                observed_at=OBSERVED_AT,
            )
        self.assertEqual(context.exception.code, "roster.transport")

        with self.assertRaises(CamUsageError) as context:
            roster.observe_route(
                "reviewer",
                transport="codex_queue",
                address=REVIEWER_SESSION,
                source="manual",
                observed_at=OBSERVED_AT,
            )
        self.assertEqual(context.exception.code, "roster.transport")

    def test_observed_route_requires_explicit_operator_correlation(self) -> None:
        roster = roster_with_reviewer()
        bind_reviewer(roster)
        observed = roster.observe_route(
            "reviewer",
            transport="claude_send_message",
            address="example-review-session [abcdef]",
            source="ListAgents",
            observed_at=OBSERVED_AT,
            agent_view_id="00000000",
            list_agents_name="example-review-session",
            list_agents_ref="abcdef",
            product_state="idle",
        )
        self.assertEqual(observed.route.status, RouteStatus.CANDIDATE)
        with self.assertRaises(CamUsageError) as context:
            roster.require_correlated_route("reviewer")
        self.assertEqual(context.exception.code, "roster.route_not_ready")

        confirmed = roster.confirm_route(
            "reviewer",
            expected_address="example-review-session [abcdef]",
            operator_reference="operator matched full session metadata",
            confirmed_at=OBSERVED_AT,
        )
        self.assertEqual(confirmed.route.status, RouteStatus.OPERATOR_CORRELATED)
        route = roster.require_correlated_route("reviewer")
        self.assertEqual(route.address, "example-review-session [abcdef]")

    def test_complete_internal_discovery_is_tool_correlated(self) -> None:
        roster = roster_with_reviewer()
        bind_reviewer(roster)

        observed = roster.observe_route(
            "reviewer",
            transport="claude_send_message",
            address="example-review-session [abcdef]",
            source="claude_agent_view_and_list_agents",
            observed_at=OBSERVED_AT,
            agent_view_id=None,
            list_agents_name="example-review-session",
            list_agents_ref="abcdef",
            product_state="busy",
            agent_view_kind="interactive",
            agent_view_started_at_ms=1_784_241_375_111,
            session_git_top_level="/example/project",
            session_git_common_dir="/example/project/.git",
            tool_correlated=True,
        )

        self.assertEqual(observed.route.status, RouteStatus.TOOL_CORRELATED)
        self.assertIsNone(observed.route.operator_reference)
        self.assertIsNone(observed.route.confirmed_at)
        self.assertEqual(
            roster.require_correlated_route("reviewer").address,
            "example-review-session [abcdef]",
        )

    def test_claimed_internal_source_without_complete_evidence_is_candidate(
        self,
    ) -> None:
        roster = roster_with_reviewer()
        bind_reviewer(roster)

        observed = roster.observe_route(
            "reviewer",
            transport="claude_send_message",
            address="example-review-session [abcdef]",
            source="claude_agent_view_and_list_agents",
            observed_at=OBSERVED_AT,
            agent_view_id=None,
            list_agents_name="example-review-session",
            list_agents_ref="abcdef",
            product_state="idle",
        )

        self.assertEqual(observed.route.status, RouteStatus.CANDIDATE)
        with self.assertRaises(CamUsageError) as context:
            roster.require_correlated_route("reviewer")
        self.assertEqual(context.exception.code, "roster.route_not_ready")

    def test_interactive_route_does_not_invent_missing_agent_view_id(self) -> None:
        roster = roster_with_reviewer()
        bind_reviewer(roster)

        observed = roster.observe_route(
            "reviewer",
            transport="claude_send_message",
            address="example-review-session [abcdef]",
            source="Agent View plus ListAgents",
            observed_at=OBSERVED_AT,
            agent_view_id=None,
            list_agents_name="example-review-session",
            list_agents_ref="abcdef",
            product_state="busy",
        )

        self.assertIsNone(observed.route.agent_view_id)
        self.assertIsNone(observed.as_dict()["route"]["agent_view_id"])

    def test_route_cannot_be_shared_by_two_participants(self) -> None:
        roster = roster_with_reviewer()
        roster.add(
            participant_id=ANALYST_ID,
            common_name="analyst",
            display_name="Example Analyst",
            role="analysis review",
            vendor="claude-code",
        )
        bind_reviewer(roster)
        roster.bind(
            "analyst",
            session_id=ANALYST_SESSION,
            session_label="example-analysis-session",
            session_kind="background",
            operator_reference="direct local confirmation",
            bound_at=OBSERVED_AT,
        )
        roster.observe_route(
            "reviewer",
            transport="claude_send_message",
            address="worker [abcdef]",
            source="ListAgents",
            observed_at=OBSERVED_AT,
            agent_view_id="00000000",
            list_agents_name="worker",
            list_agents_ref="abcdef",
            product_state="idle",
        )

        with self.assertRaises(CamUsageError) as context:
            roster.observe_route(
                "analyst",
                transport="claude_send_message",
                address="worker [abcdef]",
                source="ListAgents",
                observed_at=OBSERVED_AT,
                agent_view_id="00000000",
                list_agents_name="worker",
                list_agents_ref="abcdef",
            )
        self.assertEqual(context.exception.code, "roster.route_conflict")

    def test_rebinding_increments_generation_and_discards_route(self) -> None:
        roster = roster_with_reviewer()
        bind_reviewer(roster)
        roster.observe_route(
            "reviewer",
            transport="claude_send_message",
            address="worker [abcdef]",
            source="ListAgents",
            observed_at=OBSERVED_AT,
            agent_view_id="00000000",
            list_agents_name="worker",
            list_agents_ref="abcdef",
        )
        rebound = roster.bind(
            "reviewer",
            session_id=ANALYST_SESSION,
            session_label="replacement session",
            session_kind="interactive",
            operator_reference="operator replaced session",
            bound_at="2026-08-27T17:05:00Z",
        )
        self.assertEqual(rebound.binding.generation, 2)
        self.assertIsNone(rebound.route)

    def test_invalidation_keeps_history_hint_but_prevents_use(self) -> None:
        roster = roster_with_reviewer()
        bind_reviewer(roster)
        roster.observe_route(
            "reviewer",
            transport="claude_send_message",
            address="worker [abcdef]",
            source="ListAgents",
            observed_at=OBSERVED_AT,
            agent_view_id="00000000",
            list_agents_name="worker",
            list_agents_ref="abcdef",
        )
        roster.confirm_route(
            "reviewer",
            expected_address="worker [abcdef]",
            operator_reference="operator confirmation",
            confirmed_at=OBSERVED_AT,
        )
        stale = roster.invalidate("reviewer", reason="session restarted")
        self.assertEqual(stale.status, ParticipantStatus.STALE)
        self.assertEqual(stale.route.status, RouteStatus.STALE)
        with self.assertRaises(CamUsageError):
            roster.require_correlated_route("reviewer")

        with self.assertRaises(CamUsageError) as stale_confirmation:
            roster.confirm_route(
                "reviewer",
                expected_address="worker [abcdef]",
                operator_reference="must not revive stale route",
                confirmed_at=OBSERVED_AT,
            )
        self.assertEqual(
            stale_confirmation.exception.code,
            "roster.route_not_candidate",
        )

        observed = roster.observe_route(
            "reviewer",
            transport="claude_send_message",
            address="worker-renamed [fedcba]",
            source="Agent View plus ListAgents",
            observed_at="2026-08-27T17:01:00Z",
            agent_view_id="00000000",
            list_agents_name="worker-renamed",
            list_agents_ref="fedcba",
            product_state="idle",
        )
        self.assertEqual(observed.route.status, RouteStatus.CANDIDATE)
        reconfirmed = roster.confirm_route(
            "reviewer",
            expected_address="worker-renamed [fedcba]",
            operator_reference="operator reconfirmed the restarted route",
            confirmed_at="2026-08-27T17:01:01Z",
        )
        self.assertEqual(
            reconfirmed.route.status,
            RouteStatus.OPERATOR_CORRELATED,
        )

    def test_untrusted_route_churn_requires_operator_reconfirmation(self) -> None:
        roster = roster_with_reviewer()
        bind_reviewer(roster)
        roster.observe_route(
            "reviewer",
            transport="claude_send_message",
            address="worker [abcdef]",
            source="Agent View plus ListAgents",
            observed_at=OBSERVED_AT,
            agent_view_id="00000000",
            list_agents_name="worker",
            list_agents_ref="abcdef",
            product_state="idle",
        )
        roster.confirm_route(
            "reviewer",
            expected_address="worker [abcdef]",
            operator_reference="operator confirmed the bound session",
            confirmed_at=OBSERVED_AT,
        )

        refreshed = roster.observe_route(
            "reviewer",
            transport="claude_send_message",
            address="worker-renamed [fedcba]",
            source="Agent View plus ListAgents",
            observed_at="2026-08-27T17:01:00Z",
            agent_view_id="00000000",
            list_agents_name="worker-renamed",
            list_agents_ref="fedcba",
            product_state="running",
        )

        self.assertEqual(refreshed.route.status, RouteStatus.CANDIDATE)
        with self.assertRaises(CamUsageError) as context:
            roster.require_correlated_route("reviewer")
        self.assertEqual(context.exception.code, "roster.route_not_ready")

    def test_identical_fresh_route_preserves_operator_correlation(self) -> None:
        roster = roster_with_reviewer()
        bind_reviewer(roster)
        roster.observe_route(
            "reviewer",
            transport="claude_send_message",
            address="worker [abcdef]",
            source="Agent View plus ListAgents",
            observed_at=OBSERVED_AT,
            agent_view_id="00000000",
            list_agents_name="worker",
            list_agents_ref="abcdef",
            product_state="idle",
        )
        roster.confirm_route(
            "reviewer",
            expected_address="worker [abcdef]",
            operator_reference="operator confirmed the bound session",
            confirmed_at=OBSERVED_AT,
        )

        refreshed = roster.observe_route(
            "reviewer",
            transport="claude_send_message",
            address="worker [abcdef]",
            source="fresh Agent View plus ListAgents",
            observed_at="2026-08-27T17:01:00Z",
            agent_view_id="00000000",
            list_agents_name="worker",
            list_agents_ref="abcdef",
            product_state="running",
        )

        self.assertEqual(refreshed.route.status, RouteStatus.OPERATOR_CORRELATED)
        self.assertEqual(
            roster.require_correlated_route("reviewer").address,
            "worker [abcdef]",
        )

    def test_changed_agent_view_identity_evidence_requires_reconfirmation(self) -> None:
        roster = roster_with_reviewer()
        bind_reviewer(roster)
        evidence = {
            "agent_view_kind": "background",
            "agent_view_started_at_ms": 1_784_241_375_111,
            "session_git_top_level": "/example/project",
            "session_git_common_dir": "/example/project/.git",
        }
        roster.observe_route(
            "reviewer",
            transport="claude_send_message",
            address="worker [abcdef]",
            source="Agent View plus ListAgents",
            observed_at=OBSERVED_AT,
            agent_view_id="00000000",
            list_agents_name="worker",
            list_agents_ref="abcdef",
            product_state="idle",
            **evidence,
        )
        roster.confirm_route(
            "reviewer",
            expected_address="worker [abcdef]",
            operator_reference="operator confirmed exact Agent View evidence",
            confirmed_at=OBSERVED_AT,
        )

        changed = roster.observe_route(
            "reviewer",
            transport="claude_send_message",
            address="worker [abcdef]",
            source="fresh Agent View plus ListAgents",
            observed_at="2026-08-27T17:01:00Z",
            agent_view_id="00000000",
            list_agents_name="worker",
            list_agents_ref="abcdef",
            product_state="running",
            **(evidence | {"agent_view_started_at_ms": 1_784_241_375_112}),
        )

        self.assertEqual(changed.route.status, RouteStatus.CANDIDATE)
        self.assertEqual(changed.route.session_git_top_level, "/example/project")

    def test_retired_participant_cannot_be_resurrected(self) -> None:
        roster = roster_with_reviewer()
        bind_reviewer(roster)
        roster.observe_route(
            "reviewer",
            transport="claude_send_message",
            address="worker [abcdef]",
            source="Agent View plus ListAgents",
            observed_at=OBSERVED_AT,
            agent_view_id="00000000",
            list_agents_name="worker",
            list_agents_ref="abcdef",
        )
        roster.retire("reviewer", reason="workstream ended")

        with self.assertRaises(CamUsageError) as confirmation:
            roster.confirm_route(
                "reviewer",
                expected_address="worker [abcdef]",
                operator_reference="must not revive retired participant",
                confirmed_at=OBSERVED_AT,
            )
        self.assertEqual(confirmation.exception.code, "roster.participant_retired")

        with self.assertRaises(CamUsageError) as invalidation:
            roster.invalidate("reviewer", reason="must remain retired")
        self.assertEqual(invalidation.exception.code, "roster.participant_retired")
        self.assertEqual(
            roster.participants[REVIEWER_ID].status,
            ParticipantStatus.RETIRED,
        )

    def test_redacted_view_hides_session_and_route_capabilities(self) -> None:
        roster = roster_with_reviewer()
        bind_reviewer(roster)
        roster.observe_route(
            "reviewer",
            transport="claude_send_message",
            address="worker [abcdef]",
            source="ListAgents",
            observed_at=OBSERVED_AT,
            agent_view_id="00000000",
            list_agents_name="worker",
            list_agents_ref="abcdef",
            product_state="idle",
        )
        rendered = roster.as_dict(redact=True)
        participant = rendered["participants"][0]
        self.assertEqual(participant["route"]["address"], "redacted")
        self.assertEqual(participant["binding"]["session_id"], "redacted")
        self.assertEqual(participant["binding"]["session_label"], "redacted")
        self.assertEqual(participant["route"]["source"], "redacted")
        self.assertEqual(participant["route"]["agent_view_id"], "redacted")
        self.assertEqual(participant["route"]["list_agents_name"], "redacted")
        self.assertEqual(participant["route"]["list_agents_ref"], "redacted")
        self.assertNotIn("uds", str(rendered).lower())


if __name__ == "__main__":
    unittest.main()
