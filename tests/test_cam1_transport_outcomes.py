# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import asyncio
import datetime as dt
import json
import unittest
from unittest import mock

from tools import cam1, cam1_transport
from tools.cam1lib import journal, state

if __package__:
    from .test_cam1_transport import (
        CLAUDE_SESSION,
        CODEX_THREAD,
        ProjectBoundTransportTestCase,
        build_first_contact,
        dirty_validator_override_used,
        live_validation_arguments,
    )
else:
    from test_cam1_transport import (
        CLAUDE_SESSION,
        CODEX_THREAD,
        ProjectBoundTransportTestCase,
        build_first_contact,
        dirty_validator_override_used,
        live_validation_arguments,
    )


class ProjectTransportOutcomeTests(ProjectBoundTransportTestCase):
    def test_legacy_agent_view_shape_reaches_project_preflight(self) -> None:
        self.add_claude_participant()
        claude_bin = self.fake_claude(
            returned={"success": True},
            agent_view_shape="legacy",
        )

        completed = self.run_transport(
            "claude-preflight",
            "--participant",
            "local-worker",
            "--session-id",
            CLAUDE_SESSION,
            "--to",
            "local-worker [abcdef]",
            claude_bin=claude_bin,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["identity"]["agent_view_id"], "00000000")
        self.assertFalse(payload["identity"]["process_backed"])

    def test_claude_list_reports_addressable_unavailable_and_nonlocal_buckets(
        self,
    ) -> None:
        listing = """Peer sessions (3):
  busy-worker [aaaaaa]  ·  interactive  ·  busy  ·  started now
  exited-worker [bbbbbb]  ·  interactive  ·  exited  ·  started earlier
  remote-worker [cccccc]  ·  interactive  ·  idle  ·  Remote Control
"""
        claude_bin = self.fake_claude(
            returned={"success": True},
            peer_listing=listing,
        )

        completed = self.run_transport("claude-list", claude_bin=claude_bin)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual([peer["name"] for peer in payload["agents"]], ["busy-worker"])
        self.assertEqual(
            [peer["name"] for peer in payload["excluded_local_unavailable"]],
            ["exited-worker"],
        )
        self.assertEqual(
            [peer["name"] for peer in payload["excluded_nonlocal_or_unknown"]],
            ["remote-worker"],
        )

    def test_known_acceptance_survives_post_attempt_lock_contention(self) -> None:
        self.add_codex_participant()
        self.add_claude_participant()
        raw = cam1.build_hello(
            sender_vendor="claude-code",
            sender_name="local-worker",
            sender_session=CLAUDE_SESSION,
            recipient_vendor="codex",
            recipient_name="example-coordinator",
            recipient_session=CODEX_THREAD,
            reply_transport="claude_send_message",
            reply_address=CLAUDE_SESSION,
        )
        envelope = self.private_envelope("accepted-lock.cam1.json", raw)

        def accepted_send(**arguments):
            validated = cam1_transport._validate_envelope(
                arguments["envelope_path"], arguments["against_path"]
            )
            arguments["before_send"](validated)
            return {
                "ok": True,
                "status": "transport_accepted",
                "application_ack": False,
                "target_thread": CODEX_THREAD,
                "message_id": validated.envelope["message_id"],
                "transport_receipt": {
                    "queue_id": "00000000-0000-4000-8000-000000000901"
                },
            }

        with (
            mock.patch.object(
                cam1_transport.project,
                "project_transaction",
                self._post_attempt_lock_failure_transaction(),
            ),
            mock.patch.object(
                cam1_transport,
                "_send_to_codex_queue",
                side_effect=accepted_send,
            ),
            self.assertRaises(cam1_transport.TransportError) as context,
        ):
            cam1_transport.send_project_codex(
                self.binding,
                codex_bin="/not/executed/codex",
                participant_selector="example-coordinator",
                thread_guard=CODEX_THREAD,
                envelope_path=str(envelope),
                against_path=None,
                renewal_of=None,
                retry_after_intent=None,
                timeout_seconds=1,
                **live_validation_arguments(),
            )

        self.assertEqual(context.exception.code, "transport.acceptance_unjournaled")
        self.assertEqual(context.exception.audit["delivery_state"], "accepted")
        self.assertEqual(
            context.exception.audit["transport_receipt_id"],
            "00000000-0000-4000-8000-000000000901",
        )

    def test_unknown_outcome_survives_post_attempt_lock_contention(self) -> None:
        self.add_codex_participant()
        self.add_claude_participant()
        raw = cam1.build_hello(
            sender_vendor="claude-code",
            sender_name="local-worker",
            sender_session=CLAUDE_SESSION,
            recipient_vendor="codex",
            recipient_name="example-coordinator",
            recipient_session=CODEX_THREAD,
            reply_transport="claude_send_message",
            reply_address=CLAUDE_SESSION,
        )
        envelope = self.private_envelope("unknown-lock.cam1.json", raw)

        def failed_send(**arguments):
            validated = cam1_transport._validate_envelope(
                arguments["envelope_path"], arguments["against_path"]
            )
            arguments["before_send"](validated)
            raise cam1_transport.TransportError(
                "codex.queue_failed", "injected ambiguous queue failure"
            )

        with (
            mock.patch.object(
                cam1_transport.project,
                "project_transaction",
                self._post_attempt_lock_failure_transaction(),
            ),
            mock.patch.object(
                cam1_transport,
                "_send_to_codex_queue",
                side_effect=failed_send,
            ),
            self.assertRaises(cam1_transport.TransportError) as context,
        ):
            cam1_transport.send_project_codex(
                self.binding,
                codex_bin="/not/executed/codex",
                participant_selector="example-coordinator",
                thread_guard=CODEX_THREAD,
                envelope_path=str(envelope),
                against_path=None,
                renewal_of=None,
                retry_after_intent=None,
                timeout_seconds=1,
                **live_validation_arguments(),
            )

        self.assertEqual(context.exception.code, "transport.outcome_unjournaled")
        self.assertEqual(context.exception.audit["delivery_state"], "unknown")
        self.assertEqual(
            context.exception.audit["transport_error_code"], "codex.queue_failed"
        )

    def test_tool_correlated_claude_route_sends_and_is_fully_audited(
        self,
    ) -> None:
        self.add_claude_participant()
        self.add_codex_participant()
        raw = build_first_contact()
        envelope = self.private_envelope("hello.cam1.json", raw)
        marker = self.base / "claude-send.called"
        claude_bin = self.fake_claude(
            returned={
                "success": True,
                "msg_id": "00000000-0000-4000-8000-000000000900",
            },
            expected_message=raw,
            marker=marker,
            peer_state="busy",
        )

        preflight = self.run_transport(
            "claude-preflight",
            "--participant",
            "local-worker",
            "--session-id",
            CLAUDE_SESSION,
            "--to",
            "local-worker [abcdef]",
            claude_bin=claude_bin,
        )
        self.assertEqual(preflight.returncode, 0, preflight.stderr)
        preflight_payload = json.loads(preflight.stdout)
        self.assertFalse(preflight_payload["operator_correlation_required"])
        self.assertIsNone(preflight_payload["operator_correlation_subject"])
        self.assertFalse(preflight_payload["operator_identity_confirmation_required"])
        self.assertFalse(preflight_payload["transient_route_confirmation_required"])
        self.assertEqual(
            preflight_payload["participant"]["route_status"], "tool_correlated"
        )
        self.assertEqual(preflight_payload["route"]["state"], "busy")
        preflight_records = journal.replay_records(self.binding)
        self.assertEqual(
            preflight_records[-1]["event_type"], state.PARTICIPANT_ROUTE_OBSERVED
        )
        route_evidence = preflight_records[-1]["attributes"]
        self.assertEqual(route_evidence["agent_view_kind"], "interactive")
        self.assertEqual(route_evidence["agent_view_started_at_ms"], 1_784_241_375_111)
        self.assertEqual(
            route_evidence["session_git_top_level"], str(self.binding.git_top_level)
        )
        self.assertEqual(
            route_evidence["session_git_common_dir"],
            str(self.binding.git_common_dir),
        )
        self.assertNotIn("uds:", json.dumps(preflight_records[-1]))

        sent = self.run_transport(
            "claude-send",
            "--participant",
            "local-worker",
            "--session-id",
            CLAUDE_SESSION,
            "--to",
            "local-worker [abcdef]",
            "--envelope",
            str(envelope),
            claude_bin=claude_bin,
        )
        self.assertEqual(sent.returncode, 0, sent.stderr)
        payload = json.loads(sent.stdout)
        self.assertEqual(payload["status"], "transport_accepted")
        self.assertFalse(payload["application_ack"])
        self.assertEqual(payload["lifecycle"]["state"], "pending")
        self.assertTrue(marker.exists())

        records = journal.replay_records(self.binding)
        event_types = [record["event_type"] for record in records]
        intent_index = event_types.index("message.outbound.intent")
        accepted_index = event_types.index("transport.accepted")
        lifecycle_index = event_types.index(state.LIFECYCLE_ROOT_REGISTERED)
        self.assertLess(intent_index, lifecycle_index)
        self.assertLess(lifecycle_index, accepted_index)
        self.assertEqual(journal.decode_exact_message(records[intent_index]), raw)
        intent_attributes = records[intent_index]["attributes"]
        self.assertTrue(intent_attributes["validation_profile"]["available"])
        self.assertEqual(
            intent_attributes["dirty_validator_override"],
            dirty_validator_override_used(),
        )
        self.assertTrue(payload["validation_profile"]["available"])
        self.assertEqual(journal.decode_exact_message(records[lifecycle_index]), raw)
        self.assertEqual(
            state.StateStore(self.binding)
            .snapshot()
            .lifecycle.entries[payload["message_id"]]
            .state.value,
            "pending",
        )

        repeated = self.run_transport(
            "claude-send",
            "--participant",
            "local-worker",
            "--envelope",
            str(envelope),
            claude_bin=claude_bin,
        )
        self.assertEqual(repeated.returncode, 2, repeated.stderr)
        self.assertEqual(
            json.loads(repeated.stderr)["error"]["code"],
            "transport.already_accepted",
        )
        self.assertEqual(marker.read_text(encoding="utf-8").splitlines(), ["called"])
        self.assertEqual(
            sum(
                record["event_type"] == "message.outbound.intent"
                for record in journal.replay_records(self.binding)
            ),
            1,
        )

    def test_fast_application_ack_is_ingested_while_transport_is_in_flight(
        self,
    ) -> None:
        self.add_claude_participant()
        self.add_codex_participant()
        raw = build_first_contact()
        envelope = self.private_envelope("fast-hello.cam1.json", raw)
        ack = cam1.build_ack(
            raw,
            sender_vendor="claude-code",
            sender_name="local-worker",
            sender_session=CLAUDE_SESSION,
            reply_transport="claude_send_message",
            reply_address=CLAUDE_SESSION,
            status_value="received",
        )
        ack_path = self.private_envelope("fast-ack.cam1.json", ack)
        ingest_command = self.project_command(
            "message",
            "ingest",
            "--message",
            str(ack_path),
            "--as-participant",
            "example-coordinator",
        )
        claude_bin = self.fake_claude(
            returned={
                "success": True,
                "msg_id": "00000000-0000-4000-8000-000000000900",
            },
            expected_message=raw,
            during_send_command=ingest_command,
        )
        self.preflight_tool_correlated_route(claude_bin)

        sent = self.run_transport(
            "claude-send",
            "--participant",
            "local-worker",
            "--envelope",
            str(envelope),
            claude_bin=claude_bin,
        )

        self.assertEqual(sent.returncode, 0, sent.stderr)
        payload = json.loads(sent.stdout)
        self.assertEqual(payload["lifecycle"]["state"], "handled")
        records = journal.replay_records(self.binding)
        event_types = [record["event_type"] for record in records]
        intent_index = event_types.index("message.outbound.intent")
        root_index = event_types.index(state.LIFECYCLE_ROOT_REGISTERED)
        inbound_index = event_types.index("message.inbound.validated")
        accepted_index = event_types.index("transport.accepted")
        self.assertLess(intent_index, root_index)
        self.assertLess(root_index, inbound_index)
        self.assertLess(inbound_index, accepted_index)
        self.assertEqual(
            sum(event == state.LIFECYCLE_ROOT_REGISTERED for event in event_types),
            1,
        )

        duplicate = self.run_project(
            "message",
            "ingest",
            "--message",
            str(ack_path),
            "--as-participant",
            "example-coordinator",
        )
        self.assertEqual(duplicate.returncode, 0, duplicate.stderr)
        self.assertEqual(json.loads(duplicate.stdout)["status"], "duplicate")

    def test_claude_product_failure_is_unknown_and_not_retriable(self) -> None:
        self.add_claude_participant()
        self.add_codex_participant()
        raw = build_first_contact()
        envelope = self.private_envelope("rejected.cam1.json", raw)
        marker = self.base / "rejected-send.called"
        claude_bin = self.fake_claude(
            returned={"success": False, "message": "refused"},
            expected_message=raw,
            marker=marker,
        )
        self.preflight_tool_correlated_route(claude_bin)

        completed = self.run_transport(
            "claude-send",
            "--participant",
            "local-worker",
            "--envelope",
            str(envelope),
            claude_bin=claude_bin,
        )

        self.assertEqual(completed.returncode, 2)
        payload = json.loads(completed.stderr)
        self.assertEqual(payload["error"]["code"], "claude.send_failed")
        self.assertEqual(payload["audit"]["delivery_state"], "unknown")
        records = journal.replay_records(self.binding)
        self.assertEqual(
            [record["event_type"] for record in records[-3:]],
            [
                "message.outbound.intent",
                state.LIFECYCLE_ROOT_REGISTERED,
                "transport.not_accepted",
            ],
        )
        self.assertEqual(journal.decode_exact_message(records[-3]), raw)
        self.assertEqual(records[-1]["attributes"]["delivery_state"], "unknown")
        lifecycle_entries = state.StateStore(self.binding).snapshot().lifecycle.entries
        self.assertEqual(len(lifecycle_entries), 1)
        self.assertEqual(next(iter(lifecycle_entries.values())).state.value, "pending")

        repeated = self.run_transport(
            "claude-send",
            "--participant",
            "local-worker",
            "--retry-after-intent",
            records[-3]["record_id"],
            "--envelope",
            str(envelope),
            claude_bin=claude_bin,
        )
        self.assertEqual(repeated.returncode, 2, repeated.stderr)
        self.assertEqual(
            json.loads(repeated.stderr)["error"]["code"], "transport.retry_unsafe"
        )
        self.assertEqual(marker.read_text(encoding="utf-8").splitlines(), ["called"])

    def test_root_expiring_during_discovery_never_reaches_send_message(self) -> None:
        self.add_claude_participant()
        self.add_codex_participant()
        marker = self.base / "expired-race.called"
        claude_bin = self.fake_claude(
            returned={
                "success": True,
                "msg_id": "00000000-0000-4000-8000-000000000900",
            },
            marker=marker,
        )
        self.preflight_tool_correlated_route(claude_bin)
        raw = cam1.build_hello(
            sender_vendor="codex",
            sender_name="example-coordinator",
            sender_session=CODEX_THREAD,
            recipient_vendor="claude-code",
            recipient_name="local-worker",
            recipient_session=CLAUDE_SESSION,
            reply_transport="codex_queue",
            reply_address=CODEX_THREAD,
            expires_in=60,
        )
        envelope = self.private_envelope("expiring.cam1.json", raw)
        expires_at = dt.datetime.fromisoformat(
            json.loads(raw)["expires_at"].replace("Z", "+00:00")
        )
        after_expiry = expires_at + dt.timedelta(seconds=1)
        after_expiry_text = after_expiry.isoformat().replace("+00:00", "Z")

        with (
            mock.patch.object(
                cam1_transport,
                "_utc_now",
                return_value=(after_expiry, after_expiry_text),
            ),
            self.assertRaises(cam1_transport.TransportError) as context,
        ):
            asyncio.run(
                cam1_transport.send_project_claude(
                    self.binding,
                    claude_bin=str(claude_bin),
                    participant_selector="local-worker",
                    session_id_guard=CLAUDE_SESSION,
                    target_guard="local-worker [abcdef]",
                    envelope_path=str(envelope),
                    against_path=None,
                    renewal_of=None,
                    retry_after_intent=None,
                    summary=None,
                    timeout_seconds=10,
                    **live_validation_arguments(),
                )
            )

        self.assertEqual(context.exception.code, "state.root_not_sendable")
        self.assertFalse(marker.exists())
        self.assertNotIn(
            "message.outbound.intent",
            [record["event_type"] for record in journal.replay_records(self.binding)],
        )
        self.assertEqual(
            state.StateStore(self.binding).snapshot().lifecycle.entries, {}
        )

    def test_root_expiring_during_intent_journaling_is_not_dispatched(self) -> None:
        self.add_claude_participant()
        self.add_codex_participant()
        marker = self.base / "expired-during-journal.called"
        claude_bin = self.fake_claude(
            returned={
                "success": True,
                "msg_id": "00000000-0000-4000-8000-000000000900",
            },
            marker=marker,
        )
        self.preflight_tool_correlated_route(claude_bin)
        raw = cam1.build_hello(
            sender_vendor="codex",
            sender_name="example-coordinator",
            sender_session=CODEX_THREAD,
            recipient_vendor="claude-code",
            recipient_name="local-worker",
            recipient_session=CLAUDE_SESSION,
            reply_transport="codex_queue",
            reply_address=CODEX_THREAD,
            expires_in=60,
        )
        envelope = self.private_envelope("expires-while-journaling.json", raw)
        expires_at = dt.datetime.fromisoformat(
            json.loads(raw)["expires_at"].replace("Z", "+00:00")
        )
        before_expiry = expires_at - dt.timedelta(seconds=1)
        after_expiry = expires_at + dt.timedelta(seconds=1)

        def observed(value: dt.datetime) -> tuple[dt.datetime, str]:
            return value, value.isoformat().replace("+00:00", "Z")

        with (
            mock.patch.object(
                cam1_transport,
                "_utc_now",
                side_effect=[
                    observed(before_expiry),
                    observed(before_expiry),
                    observed(before_expiry),
                    observed(after_expiry),
                    observed(after_expiry),
                ],
            ),
            self.assertRaises(cam1_transport.TransportError) as context,
        ):
            asyncio.run(
                cam1_transport.send_project_claude(
                    self.binding,
                    claude_bin=str(claude_bin),
                    participant_selector="local-worker",
                    session_id_guard=CLAUDE_SESSION,
                    target_guard="local-worker [abcdef]",
                    envelope_path=str(envelope),
                    against_path=None,
                    renewal_of=None,
                    retry_after_intent=None,
                    summary=None,
                    timeout_seconds=10,
                    **live_validation_arguments(),
                )
            )

        self.assertEqual(
            context.exception.code,
            "state.observation_expired",
        )
        self.assertFalse(marker.exists())
        records = journal.replay_records(self.binding)
        self.assertEqual(
            [record["event_type"] for record in records[-3:]],
            [
                "message.outbound.intent",
                state.LIFECYCLE_ROOT_REGISTERED,
                "transport.not_accepted",
            ],
        )
        self.assertEqual(records[-1]["attributes"]["delivery_state"], "not_attempted")

    def test_unknown_claude_outcome_keeps_provisional_root_and_blocks_retry(
        self,
    ) -> None:
        self.add_claude_participant()
        self.add_codex_participant()
        raw = build_first_contact()
        envelope = self.private_envelope("unknown.cam1.json", raw)
        marker = self.base / "unknown-send.called"
        claude_bin = self.fake_claude(
            returned={"success": True, "message": "sent"},
            expected_message=raw,
            marker=marker,
        )
        self.preflight_tool_correlated_route(claude_bin)

        completed = self.run_transport(
            "claude-send",
            "--participant",
            "local-worker",
            "--envelope",
            str(envelope),
            claude_bin=claude_bin,
        )

        self.assertEqual(completed.returncode, 2)
        payload = json.loads(completed.stderr)
        self.assertEqual(payload["error"]["code"], "claude.receipt_unrecognized")
        self.assertEqual(payload["audit"]["delivery_state"], "unknown")
        records = journal.replay_records(self.binding)
        self.assertEqual(records[-1]["event_type"], "transport.not_accepted")
        self.assertEqual(records[-1]["attributes"]["delivery_state"], "unknown")
        lifecycle_entries = state.StateStore(self.binding).snapshot().lifecycle.entries
        self.assertEqual(len(lifecycle_entries), 1)
        self.assertEqual(next(iter(lifecycle_entries.values())).state.value, "pending")

        intent_id = next(
            record["record_id"]
            for record in reversed(records)
            if record["event_type"] == "message.outbound.intent"
        )
        repeated = self.run_transport(
            "claude-send",
            "--participant",
            "local-worker",
            "--retry-after-intent",
            intent_id,
            "--envelope",
            str(envelope),
            claude_bin=claude_bin,
        )
        self.assertEqual(repeated.returncode, 2, repeated.stderr)
        self.assertEqual(
            json.loads(repeated.stderr)["error"]["code"], "transport.retry_unsafe"
        )
        self.assertEqual(marker.read_text(encoding="utf-8").splitlines(), ["called"])
        self.assertEqual(
            sum(
                record["event_type"] == "message.outbound.intent"
                for record in journal.replay_records(self.binding)
            ),
            1,
        )

        rewrapped = json.loads(raw)
        rewrapped["message_id"] = "00000000-0000-4000-8000-000000000998"
        rewrapped["nonce"] = "AAAAAAAAAAAAAAAAAAAAAA"
        rewrapped_path = self.private_envelope(
            "unknown-rewrapped.cam1.json", cam1.serialize_envelope(rewrapped)
        )
        bypass = self.run_transport(
            "claude-send",
            "--participant",
            "local-worker",
            "--envelope",
            str(rewrapped_path),
            claude_bin=claude_bin,
        )
        self.assertEqual(bypass.returncode, 2, bypass.stderr)
        self.assertEqual(
            json.loads(bypass.stderr)["error"]["code"],
            "lifecycle.idempotency_conflict",
        )
        self.assertEqual(marker.read_text(encoding="utf-8").splitlines(), ["called"])
        self.assertEqual(
            sum(
                record["event_type"] == "message.outbound.intent"
                for record in journal.replay_records(self.binding)
            ),
            1,
        )

    def test_orphaned_outbound_intent_blocks_automatic_rerun(self) -> None:
        self.add_claude_participant()
        self.add_codex_participant()
        raw = build_first_contact()
        envelope = self.private_envelope("orphaned.cam1.json", raw)
        marker = self.base / "orphaned-send.called"
        claude_bin = self.fake_claude(
            returned={
                "success": True,
                "msg_id": "00000000-0000-4000-8000-000000000900",
            },
            marker=marker,
        )
        self.preflight_tool_correlated_route(claude_bin)
        message_id = json.loads(raw)["message_id"]
        orphaned = journal.append_record(
            self.binding,
            event_type="message.outbound.intent",
            exact_message=raw,
            attributes={"message_id": message_id, "simulated_crash": True},
        )

        completed = self.run_transport(
            "claude-send",
            "--participant",
            "local-worker",
            "--retry-after-intent",
            orphaned["record_id"],
            "--envelope",
            str(envelope),
            claude_bin=claude_bin,
        )

        self.assertEqual(completed.returncode, 2, completed.stderr)
        self.assertEqual(
            json.loads(completed.stderr)["error"]["code"], "transport.retry_unsafe"
        )
        self.assertFalse(marker.exists())
        self.assertEqual(
            sum(
                record["event_type"] == "message.outbound.intent"
                for record in journal.replay_records(self.binding)
            ),
            1,
        )

        rewrapped = json.loads(raw)
        rewrapped["message_id"] = "00000000-0000-4000-8000-000000000998"
        rewrapped["nonce"] = "AAAAAAAAAAAAAAAAAAAAAA"
        rewrapped_path = self.private_envelope(
            "orphaned-rewrapped.cam1.json", cam1.serialize_envelope(rewrapped)
        )
        bypass = self.run_transport(
            "claude-send",
            "--participant",
            "local-worker",
            "--envelope",
            str(rewrapped_path),
            claude_bin=claude_bin,
        )
        self.assertEqual(bypass.returncode, 2, bypass.stderr)
        self.assertEqual(
            json.loads(bypass.stderr)["error"]["code"],
            "transport.retry_requires_identical_envelope",
        )
        self.assertFalse(marker.exists())
        self.assertEqual(
            sum(
                record["event_type"] == "message.outbound.intent"
                for record in journal.replay_records(self.binding)
            ),
            1,
        )

    def test_proven_not_attempted_send_requires_exact_retry_intent(self) -> None:
        self.add_claude_participant()
        self.add_codex_participant()
        raw = build_first_contact()
        envelope = self.private_envelope("not-attempted.cam1.json", raw)
        marker = self.base / "not-attempted-send.called"
        claude_bin = self.fake_claude(
            returned={
                "success": True,
                "msg_id": "00000000-0000-4000-8000-000000000900",
            },
            expected_message=raw,
            marker=marker,
        )
        self.preflight_tool_correlated_route(claude_bin)
        message_id = json.loads(raw)["message_id"]
        prior_intent = journal.append_record(
            self.binding,
            event_type="message.outbound.intent",
            exact_message=raw,
            attributes={"message_id": message_id, "simulated_pre_dispatch_stop": True},
        )
        journal.append_record(
            self.binding,
            event_type="transport.not_accepted",
            attributes={
                "intent_record_id": prior_intent["record_id"],
                "delivery_state": "not_attempted",
                "error_code": "transport.payload_too_large",
            },
        )

        unconfirmed = self.run_transport(
            "claude-send",
            "--participant",
            "local-worker",
            "--envelope",
            str(envelope),
            claude_bin=claude_bin,
        )
        self.assertEqual(unconfirmed.returncode, 2, unconfirmed.stderr)
        self.assertEqual(
            json.loads(unconfirmed.stderr)["error"]["code"],
            "transport.retry_confirmation_required",
        )
        self.assertFalse(marker.exists())

        retried = self.run_transport(
            "claude-send",
            "--participant",
            "local-worker",
            "--retry-after-intent",
            prior_intent["record_id"],
            "--envelope",
            str(envelope),
            claude_bin=claude_bin,
        )
        self.assertEqual(retried.returncode, 0, retried.stderr)
        self.assertEqual(marker.read_text(encoding="utf-8").splitlines(), ["called"])
        records = journal.replay_records(self.binding)
        retry_intents = [
            record
            for record in records
            if record["event_type"] == "message.outbound.intent"
        ]
        self.assertEqual(len(retry_intents), 2)
        self.assertEqual(
            retry_intents[-1]["attributes"]["retry_after_intent"],
            prior_intent["record_id"],
        )
        self.assertEqual(json.loads(retried.stdout)["lifecycle"]["state"], "pending")


if __name__ == "__main__":
    unittest.main()
