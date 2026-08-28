# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import datetime as dt
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.cam1lib import builders, journal, project, state
from tools.cam1lib.lifecycle import LifecycleState
from tools.cam1lib.participants import ParticipantStatus, RouteStatus
from tools.cam1lib.protocol import CamUsageError, CamValidationError, serialize_envelope

NOW = dt.datetime(2026, 8, 27, 18, 0, tzinfo=dt.UTC)
REVIEWER_ID = "00000000-0000-4000-8000-000000000101"
CODEX_SESSION = "00000000-0000-4000-8000-000000000201"
CLAUDE_SESSION = "00000000-0000-4000-8000-000000000202"


def request_bytes(
    *,
    now: dt.datetime = NOW,
    idempotency_key: str | None = None,
) -> bytes:
    return builders.build_request(
        sender_vendor="codex",
        sender_name="example coordinator",
        sender_session=CODEX_SESSION,
        recipient_vendor="claude-code",
        recipient_name="reviewer",
        recipient_session=CLAUDE_SESSION,
        reply_transport="codex_queue",
        reply_address=CODEX_SESSION,
        risk_class="informational",
        operation="request_review",
        intent="Request one harmless review",
        body="Review one synthetic artifact without making changes.",
        authorization_basis="none",
        idempotency_key=idempotency_key,
        now=now,
    )


def received_ack(root: bytes, *, now: dt.datetime) -> bytes:
    return builders.build_ack(
        root,
        sender_vendor="claude-code",
        sender_name="reviewer",
        sender_session=CLAUDE_SESSION,
        reply_transport="claude_send_message",
        reply_address=CLAUDE_SESSION,
        status_value="received",
        now=now,
    )


def cancel_bytes(root: bytes) -> bytes:
    return builders.build_cancel(
        root,
        sender_vendor="codex",
        sender_name="example coordinator",
        sender_session=CODEX_SESSION,
        reply_transport="codex_queue",
        reply_address=CODEX_SESSION,
        authority="Example Operator",
        authorization_reference="direct cancellation request",
        authorization_verified_at="2026-08-27T18:01:00Z",
        authorization_expires_at="2026-08-27T18:10:00Z",
        now=NOW + dt.timedelta(minutes=1),
    )


class JournalBackedStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name).resolve()
        self.repo = self.base / "example-project"
        self.repo.mkdir(mode=0o700)
        subprocess.run(
            [project.DEFAULT_GIT_BIN, "-C", str(self.repo), "init", "--quiet"],
            check=True,
        )
        self.binding = project.initialize_project(
            self.repo,
            state_root=self.base / "state",
            now=NOW,
        )
        self.store = state.StateStore(self.binding)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def add_reviewer(self) -> None:
        self.store.participant_add(
            participant_id=REVIEWER_ID,
            common_name="reviewer",
            display_name="Example Reviewer",
            role="code review",
            vendor="claude-code",
            now=NOW,
        )

    def bind_reviewer(self) -> None:
        self.store.participant_bind(
            "reviewer",
            session_id=CLAUDE_SESSION,
            session_label="example-review-session",
            session_kind="interactive",
            operator_reference="operator matched the local session",
            bound_at="2026-08-27T18:00:01Z",
            now=NOW + dt.timedelta(seconds=1),
        )

    def observe_reviewer(self) -> None:
        self.store.participant_observe_route(
            "reviewer",
            transport="claude_send_message",
            address="example-review-session [abcdef]",
            source="ListAgents",
            observed_at="2026-08-27T18:00:02Z",
            agent_view_id="00000000",
            list_agents_name="example-review-session",
            list_agents_ref="abcdef",
            product_state="idle",
            agent_view_kind="interactive",
            agent_view_started_at_ms=1_784_241_375_111,
            session_git_top_level="/example/project",
            session_git_common_dir="/example/project/.git",
            now=NOW + dt.timedelta(seconds=2),
        )

    def test_participant_events_rebuild_current_projection(self) -> None:
        self.add_reviewer()
        self.bind_reviewer()
        self.observe_reviewer()
        confirmed = self.store.participant_confirm_route(
            "reviewer",
            expected_address="example-review-session [abcdef]",
            operator_reference="operator correlated full session metadata",
            confirmed_at="2026-08-27T18:00:03Z",
            now=NOW + dt.timedelta(seconds=3),
        )

        snapshot = self.store.rebuild()
        restored = snapshot.roster.participants[confirmed.participant_id]
        self.assertEqual(restored.route.status, RouteStatus.OPERATOR_CORRELATED)
        self.assertEqual(restored.route.agent_view_kind, "interactive")
        self.assertEqual(restored.route.agent_view_started_at_ms, 1_784_241_375_111)
        self.assertEqual(restored.route.session_git_top_level, "/example/project")
        self.assertEqual(restored.binding.session_id, CLAUDE_SESSION)
        self.assertEqual(snapshot.journal_sequence, 4)

        projection = project.read_private_json(
            state.state_projection_path(self.binding)
        )
        self.assertEqual(projection["format"], "CAM-STATE/1")
        self.assertEqual(projection["journal_position"]["sequence"], 4)
        self.assertEqual(projection["participants"][0]["common_name"], "reviewer")

    def test_invalidate_and_retire_are_journaled_domain_events(self) -> None:
        self.add_reviewer()
        self.bind_reviewer()
        self.observe_reviewer()
        stale = self.store.participant_invalidate(
            "reviewer",
            reason="session restarted",
            now=NOW + dt.timedelta(seconds=3),
        )
        self.assertEqual(stale.status, ParticipantStatus.STALE)
        retired = self.store.participant_retire(
            "reviewer",
            reason="workstream closed",
            now=NOW + dt.timedelta(seconds=4),
        )
        self.assertEqual(retired.status, ParticipantStatus.RETIRED)

        event_types = [
            record["event_type"] for record in journal.replay_records(self.binding)
        ]
        self.assertEqual(
            event_types[-2:], [state.PARTICIPANT_INVALIDATED, state.PARTICIPANT_RETIRED]
        )
        rebuilt = self.store.rebuild()
        self.assertEqual(
            rebuilt.roster.participants[REVIEWER_ID].status,
            ParticipantStatus.RETIRED,
        )

    def test_prospective_failure_does_not_append_or_replace_projection(self) -> None:
        self.add_reviewer()
        before_journal = self.binding.journal_path.read_bytes()
        projection_path = state.state_projection_path(self.binding)
        before_projection = projection_path.read_bytes()

        with self.assertRaises(CamUsageError) as context:
            self.store.participant_add(
                common_name="reviewer",
                display_name="Duplicate",
                role="duplicate",
                vendor="claude-code",
                now=NOW + dt.timedelta(seconds=1),
            )

        self.assertEqual(context.exception.code, "roster.name_conflict")
        self.assertEqual(self.binding.journal_path.read_bytes(), before_journal)
        self.assertEqual(projection_path.read_bytes(), before_projection)

    def test_rebuild_ignores_and_replaces_disposable_cache_contents(self) -> None:
        self.add_reviewer()
        projection_path = state.state_projection_path(self.binding)
        project.replace_private_json(projection_path, {"stale": True})

        snapshot = self.store.rebuild()

        self.assertIn(REVIEWER_ID, snapshot.roster.participants)
        self.assertEqual(
            project.read_private_json(projection_path)["format"],
            "CAM-STATE/1",
        )

    def test_lifecycle_messages_preserve_exact_bytes_and_replay(self) -> None:
        root = request_bytes()
        ack = received_ack(root, now=NOW + dt.timedelta(seconds=30))
        root_entry = self.store.lifecycle_root(root, now=NOW)
        reply_entry = self.store.lifecycle_reply(
            ack,
            now=NOW + dt.timedelta(seconds=30),
        )

        self.assertEqual(reply_entry.state, LifecycleState.RECEIVED)
        records = journal.replay_records(self.binding)
        self.assertEqual(journal.decode_exact_message(records[0]), root)
        self.assertEqual(journal.decode_exact_message(records[1]), ack)
        self.assertEqual(
            records[0]["attributes"]["root_message_id"],
            root_entry.root_message_id,
        )

        rebuilt = self.store.rebuild()
        restored = rebuilt.lifecycle.entries[root_entry.root_message_id]
        self.assertEqual(restored.state, LifecycleState.RECEIVED)
        lifecycle_document = project.read_private_json(
            state.state_projection_path(self.binding)
        )
        self.assertNotIn(
            "Review one synthetic artifact", json.dumps(lifecycle_document)
        )

    def test_reply_must_be_fresh_when_it_is_observed(self) -> None:
        root = request_bytes()
        ack = received_ack(root, now=NOW + dt.timedelta(seconds=30))
        self.store.lifecycle_root(root, now=NOW)

        with self.assertRaises(CamValidationError):
            self.store.lifecycle_reply(ack, now=NOW + dt.timedelta(minutes=11))

        self.assertEqual(journal.verify_journal(self.binding).record_count, 1)

    def test_delayed_pre_expiry_ack_cannot_revive_expired_root(self) -> None:
        root = request_bytes()
        ack = received_ack(root, now=NOW + dt.timedelta(minutes=9))
        self.store.lifecycle_root(root, now=NOW)

        with self.assertRaises(CamUsageError) as context:
            self.store.lifecycle_reply(ack, now=NOW + dt.timedelta(minutes=11))

        self.assertEqual(
            context.exception.code,
            "lifecycle.root_expired_before_reply",
        )
        self.assertEqual(journal.verify_journal(self.binding).record_count, 1)

    def test_future_dated_reply_cannot_advance_state(self) -> None:
        root = request_bytes()
        ack = received_ack(root, now=NOW + dt.timedelta(minutes=9))
        self.store.lifecycle_root(root, now=NOW)

        with self.assertRaises(CamValidationError):
            self.store.lifecycle_reply(ack, now=NOW + dt.timedelta(minutes=1))

        self.assertEqual(journal.verify_journal(self.binding).record_count, 1)

    def test_observation_time_preserves_microseconds(self) -> None:
        root = request_bytes()
        observed = NOW + dt.timedelta(microseconds=123456)

        self.store.lifecycle_root(root, now=observed)

        record = journal.replay_records(self.binding)[0]
        self.assertEqual(
            record["attributes"]["observed_at"],
            "2026-08-27T18:00:00.123456Z",
        )

    def test_preserved_handling_time_does_not_backdate_journal_provenance(self) -> None:
        root = request_bytes()
        self.store.lifecycle_root(root, now=NOW)
        reply = received_ack(root, now=NOW + dt.timedelta(seconds=30))
        recorded_at = NOW + dt.timedelta(seconds=45)

        with project.project_transaction(self.binding) as transaction:
            plan = self.store.prepare_lifecycle(
                reply,
                now=NOW + dt.timedelta(seconds=30),
                transaction=transaction,
            )
            with mock.patch.object(
                state,
                "_current_utc_time",
                return_value=recorded_at,
            ):
                self.store.commit_lifecycle(
                    plan,
                    transaction=transaction,
                    preserve_prepared_observation=True,
                )

        record = journal.replay_records(self.binding)[-1]
        self.assertEqual(record["recorded_at"], "2026-08-27T18:00:45Z")
        self.assertEqual(record["provenance"]["captured_at"], record["recorded_at"])
        self.assertEqual(
            record["attributes"]["observed_at"],
            "2026-08-27T18:00:30Z",
        )

    def test_nonce_cannot_be_reused_by_another_root(self) -> None:
        first = request_bytes()
        second = json.loads(request_bytes(now=NOW + dt.timedelta(minutes=1)))
        second["nonce"] = json.loads(first)["nonce"]
        self.store.lifecycle_root(first, now=NOW)

        with self.assertRaises(CamUsageError) as context:
            self.store.lifecycle_root(
                serialize_envelope(second),
                now=NOW + dt.timedelta(minutes=1),
            )

        self.assertEqual(context.exception.code, "state.nonce_reuse")
        self.assertEqual(journal.verify_journal(self.binding).record_count, 1)

    def test_root_nonce_can_be_echoed_by_only_one_reply(self) -> None:
        root = request_bytes()
        first = received_ack(root, now=NOW + dt.timedelta(seconds=30))
        second = received_ack(root, now=NOW + dt.timedelta(seconds=45))
        self.store.lifecycle_root(root, now=NOW)
        self.store.lifecycle_reply(first, now=NOW + dt.timedelta(seconds=30))

        with self.assertRaises(CamUsageError) as context:
            self.store.lifecycle_reply(second, now=NOW + dt.timedelta(seconds=45))

        self.assertEqual(context.exception.code, "state.nonce_reuse")
        self.assertEqual(journal.verify_journal(self.binding).record_count, 2)

    def test_uppercase_uuid_spellings_replay_with_canonical_state_keys(self) -> None:
        root_value = json.loads(request_bytes())
        uppercase_id = "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"
        root_value["message_id"] = uppercase_id
        root_value["action"]["idempotency_key"] = uppercase_id
        root = serialize_envelope(root_value)
        ack = received_ack(root, now=NOW + dt.timedelta(seconds=30))

        registered = self.store.lifecycle_root(root, now=NOW)
        received = self.store.lifecycle_reply(
            ack,
            now=NOW + dt.timedelta(seconds=30),
        )

        canonical_id = uppercase_id.lower()
        self.assertEqual(registered.root_message_id, canonical_id)
        self.assertEqual(received.root_message_id, canonical_id)
        self.assertEqual(
            self.store.rebuild().lifecycle.entries[canonical_id].state,
            LifecycleState.RECEIVED,
        )

    def test_stateful_lifecycle_failure_is_not_journaled(self) -> None:
        root = request_bytes()
        result = builders.build_result(
            root,
            sender_vendor="claude-code",
            sender_name="reviewer",
            sender_session=CLAUDE_SESSION,
            reply_transport="claude_send_message",
            reply_address=CLAUDE_SESSION,
            body="Review completed.",
            now=NOW + dt.timedelta(seconds=30),
        )
        self.store.lifecycle_root(root, now=NOW)

        with self.assertRaises(CamUsageError) as context:
            self.store.lifecycle_reply(result, now=NOW + dt.timedelta(seconds=30))

        self.assertEqual(context.exception.code, "lifecycle.transition")
        self.assertEqual(journal.verify_journal(self.binding).record_count, 1)

    def test_acknowledged_request_can_complete_after_root_expiry(self) -> None:
        root = request_bytes()
        ack = received_ack(root, now=NOW + dt.timedelta(minutes=1))
        accepted = builders.build_status(
            root,
            sender_vendor="claude-code",
            sender_name="reviewer",
            sender_session=CLAUDE_SESSION,
            reply_transport="claude_send_message",
            reply_address=CLAUDE_SESSION,
            status_value="accepted",
            body="The request is accepted for processing.",
            previous_responses=(ack,),
            now=NOW + dt.timedelta(minutes=2),
        )
        result = builders.build_result(
            root,
            sender_vendor="claude-code",
            sender_name="reviewer",
            sender_session=CLAUDE_SESSION,
            reply_transport="claude_send_message",
            reply_address=CLAUDE_SESSION,
            body="Review completed after a long-running analysis.",
            previous_responses=(ack, accepted),
            now=NOW + dt.timedelta(minutes=9),
        )
        late_result = json.loads(result)
        late_result["sent_at"] = "2026-08-27T18:20:00Z"
        late_result["expires_at"] = "2026-08-27T18:30:00Z"
        result = serialize_envelope(late_result)
        request_entry = self.store.lifecycle_root(root, now=NOW)
        self.store.lifecycle_reply(ack, now=NOW + dt.timedelta(minutes=1))
        self.store.lifecycle_reply(accepted, now=NOW + dt.timedelta(minutes=2))

        completed = self.store.lifecycle_reply(
            result,
            now=NOW + dt.timedelta(minutes=20),
        )

        self.assertEqual(completed.state, LifecycleState.COMPLETED)
        self.assertEqual(
            self.store.rebuild().lifecycle.entries[request_entry.root_message_id].state,
            LifecycleState.COMPLETED,
        )

    def test_reply_claimed_time_cannot_regress_behind_prior_reply(self) -> None:
        root = request_bytes()
        ack = received_ack(root, now=NOW + dt.timedelta(minutes=5))
        accepted = builders.build_status(
            root,
            sender_vendor="claude-code",
            sender_name="reviewer",
            sender_session=CLAUDE_SESSION,
            reply_transport="claude_send_message",
            reply_address=CLAUDE_SESSION,
            status_value="accepted",
            body="The request is accepted.",
            previous_responses=(ack,),
            now=NOW + dt.timedelta(minutes=6),
        )
        accepted_value = json.loads(accepted)
        accepted_value["sent_at"] = "2026-08-27T18:04:00Z"
        accepted = serialize_envelope(accepted_value)
        self.store.lifecycle_root(root, now=NOW)
        self.store.lifecycle_reply(ack, now=NOW + dt.timedelta(minutes=5))

        with self.assertRaises(CamUsageError) as context:
            self.store.lifecycle_reply(
                accepted,
                now=NOW + dt.timedelta(minutes=6),
            )

        self.assertEqual(context.exception.code, "lifecycle.reply_chronology")
        self.assertEqual(journal.verify_journal(self.binding).record_count, 2)

    def test_accepted_cancel_terminalizes_original_request_in_replay(self) -> None:
        root = request_bytes()
        cancel = cancel_bytes(root)
        accepted = builders.build_ack(
            cancel,
            sender_vendor="claude-code",
            sender_name="reviewer",
            sender_session=CLAUDE_SESSION,
            reply_transport="claude_send_message",
            reply_address=CLAUDE_SESSION,
            status_value="accepted",
            now=NOW + dt.timedelta(minutes=2),
        )
        request_entry = self.store.lifecycle_root(root, now=NOW)
        cancel_entry = self.store.lifecycle_root(
            cancel,
            now=NOW + dt.timedelta(minutes=1),
        )
        self.store.lifecycle_reply(accepted, now=NOW + dt.timedelta(minutes=2))

        rebuilt = self.store.rebuild().lifecycle
        original = rebuilt.entries[request_entry.root_message_id]
        self.assertEqual(original.state, LifecycleState.CANCELLED)
        self.assertTrue(original.terminal)
        self.assertEqual(original.cancelled_by_root_id, cancel_entry.root_message_id)

    def test_cancel_must_preserve_identity_scope_and_constraints(self) -> None:
        mutations = {
            "sender": lambda value: value["claimed_sender"].update(
                {"session_id": "00000000-0000-4000-8000-000000000888"}
            ),
            "recipient": lambda value: value["recipient"].update(
                {"session_id": "00000000-0000-4000-8000-000000000889"}
            ),
            "scope": lambda value: value["action"]["scope"]["repositories"].append(
                "/another/project"
            ),
            "constraints": lambda value: value["constraints"].update(
                {"no_repository_changes": False}
            ),
            "authorization": lambda value: value["authorization"].update(
                {"basis": "receiver_policy"}
            ),
        }
        for index, (name, mutate) in enumerate(mutations.items(), start=1):
            with self.subTest(name=name):
                binding = self.initialize_separate_project(f"cancel-{index}")
                store = state.StateStore(binding)
                root = request_bytes()
                store.lifecycle_root(root, now=NOW)
                cancel = json.loads(cancel_bytes(root))
                mutate(cancel)

                with self.assertRaises((CamUsageError, CamValidationError)):
                    store.lifecycle_root(
                        serialize_envelope(cancel),
                        now=NOW + dt.timedelta(minutes=1),
                    )
                self.assertEqual(journal.verify_journal(binding).record_count, 1)

    def test_cancel_allows_name_churn_when_stable_sessions_match(self) -> None:
        root = request_bytes()
        self.store.lifecycle_root(root, now=NOW)
        cancel = json.loads(cancel_bytes(root))
        cancel["claimed_sender"]["agent_name"] = "renamed coordinator"
        cancel["recipient"]["agent_name"] = "renamed worker"

        registered = self.store.lifecycle_root(
            serialize_envelope(cancel),
            now=NOW + dt.timedelta(minutes=1),
        )

        self.assertEqual(registered.cancels_root_id, json.loads(root)["message_id"])

    def test_expired_operation_can_be_renewed_from_journal_state(self) -> None:
        idempotency_key = "00000000-0000-4000-8000-000000000999"
        first = request_bytes(idempotency_key=idempotency_key)
        first_entry = self.store.lifecycle_root(first, now=NOW)
        self.store.lifecycle_expired(
            first_entry.root_message_id,
            now=NOW + dt.timedelta(minutes=11),
        )
        second = request_bytes(
            now=NOW + dt.timedelta(minutes=12),
            idempotency_key=idempotency_key,
        )

        renewed = self.store.lifecycle_root(
            second,
            renewal_of=first_entry.root_message_id,
            now=NOW + dt.timedelta(minutes=12),
        )

        self.assertEqual(renewed.renewal_of, first_entry.root_message_id)
        self.assertEqual(
            self.store.rebuild().lifecycle.entries[renewed.root_message_id], renewed
        )

    def test_explicit_renewal_atomically_ages_due_pending_predecessor(self) -> None:
        idempotency_key = "00000000-0000-4000-8000-000000000998"
        first = request_bytes(idempotency_key=idempotency_key)
        first_entry = self.store.lifecycle_root(first, now=NOW)
        second = request_bytes(
            now=NOW + dt.timedelta(minutes=12),
            idempotency_key=idempotency_key,
        )

        renewed = self.store.lifecycle_root(
            second,
            renewal_of=first_entry.root_message_id,
            now=NOW + dt.timedelta(minutes=12),
        )

        rebuilt = self.store.rebuild().lifecycle.entries
        self.assertEqual(renewed.renewal_of, first_entry.root_message_id)
        self.assertEqual(
            rebuilt[first_entry.root_message_id].state,
            LifecycleState.EXPIRED_UNCONFIRMED,
        )

    def test_explicit_renewal_atomically_ages_due_pending_cancel(self) -> None:
        idempotency_key = "00000000-0000-4000-8000-000000000996"
        first = request_bytes(idempotency_key=idempotency_key)
        first_entry = self.store.lifecycle_root(first, now=NOW)
        cancel = cancel_bytes(first)
        cancel_entry = self.store.lifecycle_root(
            cancel,
            now=NOW + dt.timedelta(minutes=1),
        )
        renewal = request_bytes(
            now=NOW + dt.timedelta(minutes=12),
            idempotency_key=idempotency_key,
        )

        renewed = self.store.lifecycle_root(
            renewal,
            renewal_of=first_entry.root_message_id,
            now=NOW + dt.timedelta(minutes=12),
        )

        rebuilt = self.store.rebuild().lifecycle.entries
        self.assertEqual(renewed.renewal_of, first_entry.root_message_id)
        self.assertEqual(
            rebuilt[first_entry.root_message_id].state,
            LifecycleState.EXPIRED_UNCONFIRMED,
        )
        self.assertEqual(
            rebuilt[cancel_entry.root_message_id].state,
            LifecycleState.EXPIRED_UNCONFIRMED,
        )

    def test_renewal_cannot_claim_send_before_predecessor_expiry(self) -> None:
        idempotency_key = "00000000-0000-4000-8000-000000000998"
        first = request_bytes(idempotency_key=idempotency_key)
        first_entry = self.store.lifecycle_root(first, now=NOW)
        self.store.lifecycle_expired(
            first_entry.root_message_id,
            now=NOW + dt.timedelta(minutes=11),
        )
        second = json.loads(
            request_bytes(
                now=NOW + dt.timedelta(minutes=12),
                idempotency_key=idempotency_key,
            )
        )
        second["sent_at"] = "2026-08-27T18:09:00Z"

        with self.assertRaises(CamUsageError) as context:
            self.store.lifecycle_root(
                serialize_envelope(second),
                renewal_of=first_entry.root_message_id,
                now=NOW + dt.timedelta(minutes=12),
            )

        self.assertEqual(context.exception.code, "lifecycle.renewal_chronology")

    def test_uppercase_renewal_reference_is_canonicalized(self) -> None:
        idempotency_key = "00000000-0000-4000-8000-000000000997"
        first = request_bytes(idempotency_key=idempotency_key)
        first_entry = self.store.lifecycle_root(first, now=NOW)
        self.store.lifecycle_expired(
            first_entry.root_message_id,
            now=NOW + dt.timedelta(minutes=11),
        )
        second = request_bytes(
            now=NOW + dt.timedelta(minutes=12),
            idempotency_key=idempotency_key,
        )

        renewed = self.store.lifecycle_root(
            second,
            renewal_of=first_entry.root_message_id.upper(),
            now=NOW + dt.timedelta(minutes=12),
        )

        self.assertEqual(renewed.renewal_of, first_entry.root_message_id)

    def test_receiver_requires_explicit_renewal_root(self) -> None:
        idempotency_key = "00000000-0000-4000-8000-000000000996"
        first = request_bytes(idempotency_key=idempotency_key)
        first_entry = self.store.lifecycle_root(first, now=NOW)
        self.store.lifecycle_expired(
            first_entry.root_message_id,
            now=NOW + dt.timedelta(minutes=11),
        )
        received_replacement = request_bytes(
            now=NOW + dt.timedelta(minutes=12),
            idempotency_key=idempotency_key,
        )

        with self.assertRaises(CamUsageError) as context:
            self.store.lifecycle_root(
                received_replacement,
                now=NOW + dt.timedelta(minutes=12),
            )
        self.assertEqual(context.exception.code, "lifecycle.idempotency_conflict")

        renewed = self.store.lifecycle_root(
            received_replacement,
            renewal_of=first_entry.root_message_id,
            now=NOW + dt.timedelta(minutes=12),
        )

        self.assertEqual(renewed.renewal_of, first_entry.root_message_id)
        rebuilt = self.store.rebuild().lifecycle.entries[renewed.root_message_id]
        self.assertEqual(rebuilt.renewal_of, first_entry.root_message_id)

    def test_exact_message_id_reuse_with_different_bytes_fails_before_append(
        self,
    ) -> None:
        root = request_bytes()
        self.store.lifecycle_root(root, now=NOW)
        reserialized = b" " + root

        with self.assertRaises(CamUsageError) as context:
            self.store.lifecycle_root(reserialized, now=NOW)

        self.assertEqual(context.exception.code, "state.message_conflict")
        self.assertEqual(journal.verify_journal(self.binding).record_count, 1)

    def test_invalid_and_unknown_state_events_fail_closed_on_rebuild(self) -> None:
        journal.append_record(
            self.binding,
            event_type=state.PARTICIPANT_ADDED,
            attributes={"participant_id": REVIEWER_ID},
            now=NOW,
        )
        with self.assertRaises(state.StateError) as malformed:
            self.store.rebuild()
        self.assertEqual(malformed.exception.code, "state.event_invalid")

        second = self.initialize_separate_project("unknown-state-event")
        journal.append_record(
            second,
            event_type="state.future_extension",
            attributes={},
            now=NOW,
        )
        with self.assertRaises(state.StateError) as unknown:
            state.rebuild_state(second)
        self.assertEqual(unknown.exception.code, "state.event_type")

    def test_hash_chain_valid_but_uncorrelated_reply_cannot_advance_state(self) -> None:
        root = request_bytes()
        root_entry = self.store.lifecycle_root(root, now=NOW)
        ack = received_ack(root, now=NOW + dt.timedelta(seconds=30))
        malformed = json.loads(ack)
        malformed["in_reply_to"] = "00000000-0000-4000-8000-000000000777"
        exact_malformed = serialize_envelope(malformed)
        journal.append_record(
            self.binding,
            event_type=state.LIFECYCLE_REPLY_APPLIED,
            exact_message=exact_malformed,
            attributes={
                "message_id": malformed["message_id"],
                "root_message_id": root_entry.root_message_id,
                "message_type": "ack",
                "observed_at": "2026-08-27T18:00:30Z",
            },
            now=NOW + dt.timedelta(seconds=30),
        )

        with self.assertRaises(state.StateError) as context:
            self.store.rebuild()

        self.assertEqual(context.exception.code, "state.event_invalid")
        prior_cache = project.read_private_json(
            state.state_projection_path(self.binding)
        )
        self.assertEqual(prior_cache["lifecycle"][0]["state"], "pending")

    def test_non_state_events_are_ignored_but_count_in_projection_position(
        self,
    ) -> None:
        journal.append_record(
            self.binding,
            event_type="transport.accepted",
            exact_message=b"opaque transport receipt",
            now=NOW,
        )

        snapshot = self.store.rebuild()

        self.assertEqual(snapshot.journal_sequence, 1)
        self.assertEqual(snapshot.roster.participants, {})
        self.assertEqual(
            project.read_private_json(state.state_projection_path(self.binding))[
                "journal_position"
            ]["sequence"],
            1,
        )

    def test_projection_failure_leaves_journal_rebuildable(self) -> None:
        with (
            mock.patch.object(
                state,
                "replace_private_json",
                side_effect=project.ProjectError("state.replace", "injected failure"),
            ),
            self.assertRaises(state.ProjectionRefreshError) as context,
        ):
            self.store.participant_add(
                participant_id=REVIEWER_ID,
                common_name="reviewer",
                display_name="Example Reviewer",
                role="code review",
                vendor="claude-code",
                now=NOW,
            )

        self.assertEqual(context.exception.code, "state.projection_refresh")
        self.assertEqual(context.exception.sequence, 1)
        self.assertEqual(journal.verify_journal(self.binding).record_count, 1)
        rebuilt = self.store.rebuild()
        self.assertIn(REVIEWER_ID, rebuilt.roster.participants)

    def test_existing_project_transaction_can_compose_multiple_mutations(self) -> None:
        with project.project_transaction(self.binding) as transaction:
            self.store.participant_add(
                participant_id=REVIEWER_ID,
                common_name="reviewer",
                display_name="Example Reviewer",
                role="code review",
                vendor="claude-code",
                now=NOW,
                transaction=transaction,
            )
            self.store.participant_bind(
                "reviewer",
                session_id=CLAUDE_SESSION,
                session_label="example-review-session",
                session_kind="interactive",
                operator_reference="operator matched the local session",
                bound_at="2026-08-27T18:00:01Z",
                now=NOW + dt.timedelta(seconds=1),
                transaction=transaction,
            )

        self.assertEqual(journal.verify_journal(self.binding).record_count, 2)

    def test_transaction_replays_state_once_and_advances_snapshot_cache(self) -> None:
        original_verify = journal._verify_records
        original_empty = state._empty_snapshot
        original_deepcopy = state.deepcopy

        with (
            mock.patch.object(
                journal, "_verify_records", wraps=original_verify
            ) as verify_records,
            mock.patch.object(
                state, "_empty_snapshot", wraps=original_empty
            ) as empty_snapshot,
            mock.patch.object(
                state, "deepcopy", wraps=original_deepcopy
            ) as copy_snapshot,
            project.project_transaction(self.binding) as transaction,
        ):
            self.assertEqual(
                self.store.snapshot(transaction=transaction).journal_sequence,
                0,
            )
            self.assertEqual(copy_snapshot.call_count, 1)
            self.assertIsNone(
                self.store.preserved_message(
                    "00000000-0000-4000-8000-000000000999",
                    transaction=transaction,
                )
            )
            self.assertEqual(copy_snapshot.call_count, 1)
            self.store.participant_add(
                participant_id=REVIEWER_ID,
                common_name="reviewer",
                display_name="Example Reviewer",
                role="code review",
                vendor="claude-code",
                now=NOW,
                transaction=transaction,
            )
            self.assertEqual(copy_snapshot.call_count, 2)
            self.store.participant_bind(
                "reviewer",
                session_id=CLAUDE_SESSION,
                session_label="example-review-session",
                session_kind="interactive",
                operator_reference="operator matched the local session",
                bound_at="2026-08-27T18:00:01Z",
                now=NOW + dt.timedelta(seconds=1),
                transaction=transaction,
            )
            self.assertEqual(copy_snapshot.call_count, 3)
            journal.append_record(
                self.binding,
                event_type="note.cache-observation",
                now=NOW + dt.timedelta(seconds=2),
                transaction=transaction,
            )
            snapshot = self.store.snapshot(transaction=transaction)

            self.assertEqual(snapshot.journal_sequence, 3)
            self.assertEqual(
                snapshot.roster.participants[REVIEWER_ID].status,
                ParticipantStatus.BOUND,
            )
            snapshot.roster.participants.clear()
            self.assertIn(
                REVIEWER_ID,
                self.store.snapshot(transaction=transaction).roster.participants,
            )
            self.assertEqual(copy_snapshot.call_count, 6)
            self.assertEqual(verify_records.call_count, 1)
            self.assertEqual(empty_snapshot.call_count, 1)

    def initialize_separate_project(self, name: str) -> project.ProjectBinding:
        repo = self.base / name
        repo.mkdir(mode=0o700)
        subprocess.run(
            [project.DEFAULT_GIT_BIN, "-C", str(repo), "init", "--quiet"],
            check=True,
        )
        return project.initialize_project(
            repo,
            state_root=self.base / "other-state",
            now=NOW,
        )


if __name__ == "__main__":
    unittest.main()
