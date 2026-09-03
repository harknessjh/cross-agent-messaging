# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Regressions for safety invariants that cross CAM/1 feature boundaries."""

from __future__ import annotations

import datetime as dt
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest import mock

from tools import (
    cam1_transport,
    cam1_transport_native,
    cam1_transport_products,
)
from tools.cam1lib import (
    builders,
    causal,
    compatibility,
    inbound,
    journal,
    project,
    protocol,
    state,
    state_projection,
    state_store,
    transport_audit,
)

if __package__:
    from .test_cam1_causal_integration import _activate_gate, _append_intent
    from .test_cam1_project import (
        CLAUDE_PARTICIPANT as PROJECT_CLAUDE_PARTICIPANT,
    )
    from .test_cam1_project import CLAUDE_SESSION as PROJECT_CLAUDE_SESSION
    from .test_cam1_project import CODEX_PARTICIPANT as PROJECT_CODEX_PARTICIPANT
    from .test_cam1_project import CODEX_SESSION as PROJECT_CODEX_SESSION
    from .test_cam1_project import ProjectTestCase
    from .test_cam1_transport import (
        CLAUDE_PARTICIPANT,
        CLAUDE_SESSION,
        CODEX_PARTICIPANT,
        CODEX_THREAD,
        ProjectBoundTransportTestCase,
    )
else:
    from test_cam1_causal_integration import _activate_gate, _append_intent
    from test_cam1_project import (
        CLAUDE_PARTICIPANT as PROJECT_CLAUDE_PARTICIPANT,
    )
    from test_cam1_project import CLAUDE_SESSION as PROJECT_CLAUDE_SESSION
    from test_cam1_project import CODEX_PARTICIPANT as PROJECT_CODEX_PARTICIPANT
    from test_cam1_project import CODEX_SESSION as PROJECT_CODEX_SESSION
    from test_cam1_project import ProjectTestCase
    from test_cam1_transport import (
        CLAUDE_PARTICIPANT,
        CLAUDE_SESSION,
        CODEX_PARTICIPANT,
        CODEX_THREAD,
        ProjectBoundTransportTestCase,
    )


def _utc_text(value: dt.datetime) -> str:
    return value.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")


def _bind_ingest_participants(binding: project.ProjectBinding) -> None:
    store = state.StateStore(binding)
    observed = dt.datetime.now(dt.UTC).replace(microsecond=0)
    for participant_id, common_name, display_name, vendor, session_id in (
        (
            PROJECT_CODEX_PARTICIPANT,
            "project-coordinator",
            "Project coordinator",
            "codex",
            PROJECT_CODEX_SESSION,
        ),
        (
            PROJECT_CLAUDE_PARTICIPANT,
            "bob-reviewer",
            "Bob reviewer",
            "claude-code",
            PROJECT_CLAUDE_SESSION,
        ),
    ):
        store.participant_add(
            participant_id=participant_id,
            common_name=common_name,
            display_name=display_name,
            role="CAM test participant",
            vendor=vendor,
            now=observed,
        )
        store.participant_bind(
            common_name,
            session_id=session_id,
            session_label=display_name,
            session_kind="interactive",
            operator_reference="test operator correlation",
            bound_at=_utc_text(observed),
            now=observed,
        )


@contextmanager
def _reader_supporting(capability: str) -> Iterator[None]:
    """Temporarily model a newer reader without changing production constants."""

    supported = frozenset({*compatibility.SUPPORTED_READER_CAPABILITIES, capability})
    real_require = compatibility.require_reader_support

    def require_with_capability(gate: compatibility.CompatibilityGate) -> None:
        real_require(gate, supported_capabilities=supported)

    with (
        mock.patch.object(
            compatibility,
            "SUPPORTED_READER_CAPABILITIES",
            supported,
        ),
        mock.patch.object(
            compatibility,
            "require_reader_support",
            side_effect=require_with_capability,
        ),
    ):
        yield


class ProductApprovalCausalOutcomeTests(ProjectBoundTransportTestCase):
    """Keep approval failures and causal dispatch evidence consistent."""

    def setUp(self) -> None:
        super().setUp()
        self.add_claude_participant()
        self.add_codex_participant()
        self.store = state.StateStore(self.binding)
        gate_time = dt.datetime.now(dt.UTC).replace(microsecond=0)
        _activate_gate(
            self.binding,
            self.store,
            feature_id=compatibility.COMPATIBILITY_KERNEL_FEATURE_ID,
            now=gate_time,
        )
        _activate_gate(
            self.binding,
            self.store,
            feature_id=causal.CAUSAL_FEATURE_ID,
            now=gate_time + dt.timedelta(seconds=5),
        )

    def _approval_failure_assessment(
        self, *, dispatch_started: bool
    ) -> tuple[
        cam1_transport.TransportError,
        causal.CausalAssessment,
        str,
        dict[str, Any],
    ]:
        now = dt.datetime.now(dt.UTC).replace(microsecond=0) - dt.timedelta(minutes=1)
        root = builders.build_request(
            sender_vendor="codex",
            sender_name="example-coordinator",
            sender_session=CODEX_THREAD,
            recipient_vendor="claude-code",
            recipient_name="local-worker",
            recipient_session=CLAUDE_SESSION,
            reply_transport="codex_queue",
            reply_address=CODEX_THREAD,
            risk_class="informational",
            operation="review_structure",
            intent="Request one local structure review",
            body="Review the project structure without making changes.",
            authorization_basis="none",
            now=now,
        )
        root_id = protocol.parse_exact_bytes(root)["message_id"]
        _append_intent(
            self.binding,
            root,
            sender_participant_id=CODEX_PARTICIPANT,
            recipient_participant_id=CLAUDE_PARTICIPANT,
            context=causal.CausalContext(root_id, (), (), ()),
            now=now,
            accepted=True,
        )
        self.store.lifecycle_root(root, now=now)
        acknowledgement = builders.build_ack(
            root,
            sender_vendor="claude-code",
            sender_name="local-worker",
            sender_session=CLAUDE_SESSION,
            reply_transport="claude_send_message",
            reply_address=CLAUDE_SESSION,
            status_value="received",
            now=now + dt.timedelta(seconds=1),
        )
        acknowledgement_id = protocol.parse_exact_bytes(acknowledgement)["message_id"]
        root_path = self.private_envelope("causal-root.cam1.json", root)
        acknowledgement_path = self.private_envelope(
            "approval-failure-ack.cam1.json", acknowledgement
        )

        def fail_after_intent(**arguments: Any) -> dict[str, object]:
            validated = cam1_transport._validate_envelope(
                str(arguments["envelope_path"]), str(arguments["against_path"])
            )
            arguments["before_send"](validated)
            if dispatch_started:
                arguments["before_dispatch"]()
            raise cam1_transport.TransportError(
                "product_approval.drift",
                "synthetic approval drift after the durable outbound intent",
            )

        with (
            mock.patch.object(
                cam1_transport,
                "_require_live_validation_profile",
                return_value=({"available": True}, False),
            ),
            mock.patch.object(cam1_transport, "_require_approved_product_executable"),
            mock.patch.object(cam1_transport, "_require_current_product_approval"),
            mock.patch.object(
                cam1_transport,
                "_send_to_codex_queue",
                side_effect=fail_after_intent,
            ),
            self.assertRaises(cam1_transport.TransportError) as raised,
        ):
            cam1_transport.send_project_codex(
                self.binding,
                codex_bin=str(self.approved_codex_bin),
                participant_selector="example-coordinator",
                thread_guard=CODEX_THREAD,
                envelope_path=str(acknowledgement_path),
                against_path=str(root_path),
                renewal_of=None,
                retry_after_intent=None,
                timeout_seconds=10,
            )

        renewal = builders.build_request(
            sender_vendor="codex",
            sender_name="example-coordinator",
            sender_session=CODEX_THREAD,
            recipient_vendor="claude-code",
            recipient_name="local-worker",
            recipient_session=CLAUDE_SESSION,
            reply_transport="codex_queue",
            reply_address=CODEX_THREAD,
            risk_class="informational",
            operation="review_structure",
            intent="Renew the same local structure review",
            body="Review the project structure without making changes.",
            authorization_basis="none",
            idempotency_key=protocol.parse_exact_bytes(root)["action"][
                "idempotency_key"
            ],
            now=now + dt.timedelta(seconds=2),
        )
        _append_intent(
            self.binding,
            renewal,
            sender_participant_id=CODEX_PARTICIPANT,
            recipient_participant_id=CLAUDE_PARTICIPANT,
            context=causal.CausalContext(
                root_id,
                (),
                (root_id,),
                (),
            ),
            renewal_of=root_id,
            now=now + dt.timedelta(seconds=2),
        )
        records = journal.replay_records(
            self.binding,
            event_types=causal.CAUSAL_JOURNAL_EVENT_TYPES,
        )
        assessment = causal.assess_inbound_order(
            records,
            renewal,
            local_participant_id=CLAUDE_PARTICIPANT,
            sender_participant_id=CODEX_PARTICIPANT,
        )
        self.assertEqual(assessment.conversation_id, root_id)
        outcome = next(
            record
            for record in reversed(records)
            if record["event_type"] == "transport.not_accepted"
            and record["attributes"]["message_id"] == acknowledgement_id
        )
        return raised.exception, assessment, acknowledgement_id, outcome

    def test_pre_dispatch_approval_drift_is_not_attempted_and_leaves_no_frontier(
        self,
    ) -> None:
        error, assessment, _, outcome = self._approval_failure_assessment(
            dispatch_started=False
        )

        self.assertEqual(error.code, "product_approval.drift")
        self.assertEqual(error.audit["delivery_state"], "not_attempted")
        self.assertEqual(outcome["attributes"]["delivery_state"], "not_attempted")
        self.assertFalse(assessment.held)
        self.assertEqual(assessment.required_frontier_count, 0)
        self.assertEqual(assessment.missing_frontier, ())

    def test_post_dispatch_approval_error_is_unknown_and_remains_in_frontier(
        self,
    ) -> None:
        error, assessment, acknowledgement_id, outcome = (
            self._approval_failure_assessment(dispatch_started=True)
        )

        self.assertEqual(error.code, "product_approval.drift")
        self.assertEqual(error.audit["delivery_state"], "unknown")
        self.assertEqual(outcome["attributes"]["delivery_state"], "unknown")
        self.assertTrue(assessment.held)
        self.assertEqual(assessment.required_frontier_count, 1)
        self.assertEqual(assessment.missing_frontier, (acknowledgement_id,))


class CompatibilityInboundRecoveryTests(ProjectTestCase):
    """Unsupported gates retain bytes without prematurely mutating lifecycle."""

    def test_upgraded_reader_reingests_observed_message_once_semantically(self) -> None:
        binding = self.initialize()
        store = state.StateStore(binding)
        _bind_ingest_participants(binding)
        gate_time = dt.datetime.now(dt.UTC).replace(microsecond=0)
        _activate_gate(
            binding,
            store,
            feature_id=compatibility.COMPATIBILITY_KERNEL_FEATURE_ID,
            now=gate_time,
        )
        future_feature = "future.inbound-recovery"
        future_capability = f"{future_feature}/1"
        with _reader_supporting(future_capability):
            _activate_gate(
                binding,
                store,
                feature_id=future_feature,
                now=gate_time + dt.timedelta(seconds=5),
            )

        raw = builders.build_request(
            sender_vendor="codex",
            sender_name="project-coordinator",
            sender_session=PROJECT_CODEX_SESSION,
            recipient_vendor="claude-code",
            recipient_name="bob-reviewer",
            recipient_session=PROJECT_CLAUDE_SESSION,
            reply_transport="codex_queue",
            reply_address=PROJECT_CODEX_SESSION,
            risk_class="informational",
            operation="review_structure",
            intent="Request one local structure review",
            body="Review the project structure without making changes.",
            authorization_basis="none",
            now=dt.datetime.now(dt.UTC),
        )

        before = journal.verify_journal(binding).record_count
        with self.assertRaises(compatibility.CompatibilityUpgradeRequired):
            inbound.ingest_message(
                binding,
                message_path=None,
                exact_message=raw,
                observed_source="test_exact_bytes",
                as_participant="bob-reviewer",
                renewal_of=None,
            )

        first_delta = journal.replay_records(binding)[before:]
        self.assertEqual(
            [record["event_type"] for record in first_delta],
            ["message.inbound.observed"],
        )
        self.assertEqual(journal.decode_exact_message(first_delta[0]), raw)

        with _reader_supporting(future_capability):
            return_code, payload = inbound.ingest_message(
                binding,
                message_path=None,
                exact_message=raw,
                observed_source="test_exact_bytes",
                as_participant="bob-reviewer",
                renewal_of=None,
            )

        self.assertEqual(return_code, 0, payload)
        self.assertEqual(payload["status"], "validated")
        self.assertFalse(payload["action_authorized"])
        delta = journal.replay_records(binding)[before:]
        self.assertEqual(
            [record["event_type"] for record in delta],
            [
                "message.inbound.observed",
                "message.inbound.observed",
                state.LIFECYCLE_ROOT_REGISTERED,
                "message.inbound.validated",
            ],
        )
        self.assertEqual(
            sum(
                record["event_type"] == state.LIFECYCLE_ROOT_REGISTERED
                for record in delta
            ),
            1,
        )
        self.assertEqual(
            sum(
                record["event_type"] == "message.inbound.validated" for record in delta
            ),
            1,
        )
        observations = [
            record
            for record in delta
            if record["event_type"] == "message.inbound.observed"
        ]
        self.assertTrue(
            all(journal.decode_exact_message(record) == raw for record in observations)
        )


class CompatibilityProjectionCausalReplayTests(ProjectTestCase):
    """Causal activation remains authoritative when projection refresh fails."""

    def test_projection_failure_rebuilds_causal_gate_from_journal(self) -> None:
        binding = self.initialize()
        store = state.StateStore(binding)
        gate_time = dt.datetime.now(dt.UTC).replace(microsecond=0)
        _activate_gate(
            binding,
            store,
            feature_id=compatibility.COMPATIBILITY_KERNEL_FEATURE_ID,
            now=gate_time,
        )

        with (
            mock.patch.object(
                state_projection,
                "replace_private_json",
                side_effect=state.ProjectError(
                    "state.replace", "synthetic projection refresh failure"
                ),
            ),
            self.assertRaises(state.ProjectionRefreshError) as raised,
        ):
            _activate_gate(
                binding,
                store,
                feature_id=causal.CAUSAL_FEATURE_ID,
                now=gate_time + dt.timedelta(seconds=5),
            )

        records = journal.replay_records(
            binding,
            event_types=causal.CAUSAL_JOURNAL_EVENT_TYPES,
        )
        causal_activations = [
            record
            for record in records
            if record["event_type"] == compatibility.COMPATIBILITY_GATE_ACTIVATED_EVENT
            and record["attributes"]["feature_id"] == causal.CAUSAL_FEATURE_ID
        ]
        self.assertEqual(len(causal_activations), 1)
        self.assertEqual(raised.exception.record_id, causal_activations[0]["record_id"])

        raw = builders.build_request(
            sender_vendor="codex",
            sender_name="project-coordinator",
            sender_session=PROJECT_CODEX_SESSION,
            recipient_vendor="claude-code",
            recipient_name="bob-reviewer",
            recipient_session=PROJECT_CLAUDE_SESSION,
            reply_transport="codex_queue",
            reply_address=PROJECT_CODEX_SESSION,
            risk_class="informational",
            operation="review_structure",
            intent="Request one local structure review",
            body="Review the project structure without making changes.",
            authorization_basis="none",
            now=dt.datetime.now(dt.UTC),
        )
        envelope = protocol.parse_exact_bytes(raw)
        before_rebuild = causal.build_outbound_context(
            records,
            envelope,
            sender_participant_id=PROJECT_CODEX_PARTICIPANT,
            recipient_participant_id=PROJECT_CLAUDE_PARTICIPANT,
            renewal_of=None,
            retry_after_intent=None,
        )
        self.assertIsNotNone(before_rebuild)
        self.assertEqual(before_rebuild.conversation_id, envelope["message_id"])

        rebuilt = store.rebuild()
        active = rebuilt.compatibility.active_gate(causal.CAUSAL_FEATURE_ID)
        self.assertIsNotNone(active)
        self.assertEqual(active.plan_id, causal_activations[0]["attributes"]["plan_id"])
        after_rebuild = causal.build_outbound_context(
            journal.replay_records(
                binding,
                event_types=causal.CAUSAL_JOURNAL_EVENT_TYPES,
            ),
            envelope,
            sender_participant_id=PROJECT_CODEX_PARTICIPANT,
            recipient_participant_id=PROJECT_CLAUDE_PARTICIPANT,
            renewal_of=None,
            retry_after_intent=None,
        )
        self.assertEqual(after_rebuild, before_rebuild)


class ExtractedFacadeCompatibilityTests(unittest.TestCase):
    """Keep historical import seams stable across transport/state extraction."""

    def test_transport_and_state_facades_reexport_extracted_symbols(self) -> None:
        aliases = (
            (cam1_transport.TransportError, cam1_transport_native.TransportError),
            (
                cam1_transport._send_to_claude,
                cam1_transport_native._send_to_claude,
            ),
            (
                cam1_transport._send_to_codex_queue,
                cam1_transport_native._send_to_codex_queue,
            ),
            (
                cam1_transport.resolve_product_binary,
                cam1_transport_products.resolve_product_binary,
            ),
            (
                cam1_transport._prepare_and_journal_intent,
                transport_audit._prepare_and_journal_intent,
            ),
            (cam1_transport._SendAttempt, transport_audit._SendAttempt),
            (state.StateStore, state_store.StateStore),
            (state.StateSnapshot, state_projection.StateSnapshot),
            (state.LifecyclePlan, state_projection.LifecyclePlan),
            (state.ProjectionRefreshError, state_projection.ProjectionRefreshError),
        )
        for facade_symbol, extracted_symbol in aliases:
            with self.subTest(symbol=extracted_symbol.__name__):
                self.assertIs(facade_symbol, extracted_symbol)


if __name__ == "__main__":
    unittest.main()
