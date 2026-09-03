# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import datetime as dt
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

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
)

SOURCE_ROOT = Path(__file__).resolve().parents[1]

if __package__:
    from .test_cam1_project import (
        CLAUDE_PARTICIPANT,
        CLAUDE_SESSION,
        CODEX_PARTICIPANT,
        CODEX_SESSION,
        ProjectTestCase,
    )
else:
    from test_cam1_project import (
        CLAUDE_PARTICIPANT,
        CLAUDE_SESSION,
        CODEX_PARTICIPANT,
        CODEX_SESSION,
        ProjectTestCase,
    )

if __package__:
    from .test_cam1_transport import (
        CLAUDE_PARTICIPANT as TRANSPORT_CLAUDE_PARTICIPANT,
    )
    from .test_cam1_transport import (
        CLAUDE_SESSION as TRANSPORT_CLAUDE_SESSION,
    )
    from .test_cam1_transport import (
        CODEX_PARTICIPANT as TRANSPORT_CODEX_PARTICIPANT,
    )
    from .test_cam1_transport import (
        CODEX_THREAD as TRANSPORT_CODEX_SESSION,
    )
    from .test_cam1_transport import (
        ProjectBoundTransportTestCase,
    )
else:
    from test_cam1_transport import (
        CLAUDE_PARTICIPANT as TRANSPORT_CLAUDE_PARTICIPANT,
    )
    from test_cam1_transport import (
        CLAUDE_SESSION as TRANSPORT_CLAUDE_SESSION,
    )
    from test_cam1_transport import (
        CODEX_PARTICIPANT as TRANSPORT_CODEX_PARTICIPANT,
    )
    from test_cam1_transport import (
        CODEX_THREAD as TRANSPORT_CODEX_SESSION,
    )
    from test_cam1_transport import (
        ProjectBoundTransportTestCase,
    )


def _utc_text(value: dt.datetime) -> str:
    return value.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")


def _activate_gate(
    binding: project.ProjectBinding,
    store: state.StateStore,
    *,
    feature_id: str,
    now: dt.datetime,
) -> None:
    snapshot = store.snapshot()
    frozen = [
        {
            "participant_id": participant.participant_id,
            "binding_generation": participant.binding.generation,
        }
        for participant in snapshot.roster.participants.values()
        if participant.binding is not None
    ]
    frozen.sort(key=lambda item: item["participant_id"])
    required_capabilities = sorted(
        {
            compatibility.COMPATIBILITY_KERNEL_CAPABILITY,
            f"{feature_id}/1",
        }
    )
    plan = compatibility.validate_plan(
        {
            "format": compatibility.COMPATIBILITY_FORMAT,
            "plan_id": str(uuid.uuid4()),
            "feature_id": feature_id,
            "feature_version": 1,
            "feature_config": {},
            "required_reader_epoch": compatibility.CURRENT_READER_EPOCH,
            "required_capabilities": required_capabilities,
            "validation_profile_sha256": "a" * 64,
            "frozen_participants": frozen,
            "expires_at": _utc_text(now + dt.timedelta(hours=1)),
            "operator_reference": "test operator approved causal compatibility",
        }
    ).as_dict()
    plan_record = journal.append_record(
        binding,
        event_type=compatibility.COMPATIBILITY_PLAN_EVENT,
        attributes=plan,
        now=now,
    )
    readiness_records: list[tuple[dict[str, object], dict[str, object]]] = []
    for offset, frozen_participant in enumerate(frozen, start=1):
        ready_at = now + dt.timedelta(seconds=offset)
        readiness = compatibility.validate_readiness(
            {
                "format": compatibility.COMPATIBILITY_FORMAT,
                "plan_id": plan["plan_id"],
                "plan_record_id": plan_record["record_id"],
                "plan_record_sha256": plan_record["record_sha256"],
                "participant_id": frozen_participant["participant_id"],
                "binding_generation": frozen_participant["binding_generation"],
                "reader_epoch": compatibility.CURRENT_READER_EPOCH,
                "capabilities": sorted(compatibility.SUPPORTED_READER_CAPABILITIES),
                "validation_profile_sha256": plan["validation_profile_sha256"],
                "ready_at": _utc_text(ready_at),
                "operator_reference": "test operator confirmed reader readiness",
            }
        ).as_dict()
        record = journal.append_record(
            binding,
            event_type=compatibility.COMPATIBILITY_READINESS_EVENT,
            attributes=readiness,
            now=ready_at,
        )
        readiness_records.append((readiness, record))
    activated_at = now + dt.timedelta(seconds=len(frozen) + 1)
    activation = compatibility.validate_activation(
        {
            "format": compatibility.COMPATIBILITY_FORMAT,
            "plan_id": plan["plan_id"],
            "plan_record_id": plan_record["record_id"],
            "plan_record_sha256": plan_record["record_sha256"],
            "feature_id": feature_id,
            "feature_version": 1,
            "required_reader_epoch": plan["required_reader_epoch"],
            "required_capabilities": plan["required_capabilities"],
            "validation_profile_sha256": plan["validation_profile_sha256"],
            "readiness": [
                {
                    "participant_id": readiness["participant_id"],
                    "record_id": record["record_id"],
                    "record_sha256": record["record_sha256"],
                }
                for readiness, record in readiness_records
            ],
            "activated_at": _utc_text(activated_at),
            "operator_reference": "test operator activated staged compatibility",
        }
    ).as_dict()
    store.compatibility_activate(activation, now=activated_at)


def _request(
    *,
    now: dt.datetime,
    idempotency_key: str | None = None,
) -> bytes:
    return builders.build_request(
        sender_vendor="codex",
        sender_name="project-coordinator",
        sender_session=CODEX_SESSION,
        recipient_vendor="claude-code",
        recipient_name="bob-reviewer",
        recipient_session=CLAUDE_SESSION,
        reply_transport="codex_queue",
        reply_address=CODEX_SESSION,
        risk_class="informational",
        operation="review_structure",
        intent="Request one local structure review",
        body="Review the project structure without making changes.",
        authorization_basis="none",
        idempotency_key=idempotency_key,
        now=now,
    )


def _append_intent(
    binding: project.ProjectBinding,
    raw: bytes,
    *,
    sender_participant_id: str,
    recipient_participant_id: str,
    context: causal.CausalContext | None,
    now: dt.datetime,
    renewal_of: str | None = None,
    accepted: bool = False,
) -> dict[str, object]:
    envelope = protocol.parse_exact_bytes(raw)
    intent = journal.append_record(
        binding,
        event_type="message.outbound.intent",
        exact_message=raw,
        attributes={
            "participant_id": recipient_participant_id,
            "sender_participant_id": sender_participant_id,
            "recipient_participant_id": recipient_participant_id,
            "message_id": envelope["message_id"],
            "renewal_of": renewal_of,
            "causal_context": context.as_dict() if context is not None else None,
        },
        now=now,
    )
    if accepted:
        journal.append_record(
            binding,
            event_type="transport.accepted",
            attributes={
                "intent_record_id": intent["record_id"],
                "message_id": envelope["message_id"],
            },
            now=now + dt.timedelta(microseconds=1),
        )
    return intent


class CausalIngestIntegrationTests(ProjectTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.binding = self.initialize()
        self.store = state.StateStore(self.binding)
        self.base_time = dt.datetime.now(dt.UTC).replace(microsecond=0) - dt.timedelta(
            minutes=12
        )
        _activate_gate(
            self.binding,
            self.store,
            feature_id=compatibility.COMPATIBILITY_KERNEL_FEATURE_ID,
            now=self.base_time - dt.timedelta(hours=1),
        )
        for participant_id, common_name, display_name, vendor, session_id in (
            (
                CODEX_PARTICIPANT,
                "project-coordinator",
                "Project coordinator",
                "codex",
                CODEX_SESSION,
            ),
            (
                CLAUDE_PARTICIPANT,
                "bob-reviewer",
                "Bob reviewer",
                "claude-code",
                CLAUDE_SESSION,
            ),
        ):
            participant_now = self.base_time - dt.timedelta(minutes=50)
            self.store.participant_add(
                participant_id=participant_id,
                common_name=common_name,
                display_name=display_name,
                role="CAM test participant",
                vendor=vendor,
                now=participant_now,
            )
            self.store.participant_bind(
                common_name,
                session_id=session_id,
                session_label=display_name,
                session_kind="interactive",
                operator_reference="test operator correlation",
                bound_at=_utc_text(participant_now + dt.timedelta(seconds=1)),
                now=participant_now + dt.timedelta(seconds=1),
            )
        _activate_gate(
            self.binding,
            self.store,
            feature_id=causal.CAUSAL_FEATURE_ID,
            now=self.base_time - dt.timedelta(minutes=30),
        )

    def _fixed_ingest(
        self,
        raw: bytes,
        *,
        as_participant: str,
        observed_at: dt.datetime,
        renewal_of: str | None = None,
    ) -> tuple[int, dict[str, object]]:
        time_text = _utc_text(observed_at)
        with (
            mock.patch.object(
                inbound,
                "_utc_now",
                return_value=(observed_at, time_text),
            ),
            mock.patch.object(
                state_projection,
                "_current_utc_time",
                return_value=observed_at,
            ),
        ):
            return inbound.ingest_message(
                self.binding,
                message_path=None,
                exact_message=raw,
                observed_source="test_exact_bytes",
                as_participant=as_participant,
                renewal_of=renewal_of,
            )

    def _stale_renewal(self) -> tuple[bytes, str, str]:
        root = _request(now=self.base_time)
        root_id = protocol.parse_exact_bytes(root)["message_id"]
        _append_intent(
            self.binding,
            root,
            sender_participant_id=CODEX_PARTICIPANT,
            recipient_participant_id=CLAUDE_PARTICIPANT,
            context=causal.CausalContext(root_id, (), (), ()),
            now=self.base_time,
            accepted=True,
        )
        code, payload = self._fixed_ingest(
            root,
            as_participant="bob-reviewer",
            observed_at=self.base_time + dt.timedelta(seconds=1),
        )
        self.assertEqual(code, 0, payload)

        ack = builders.build_ack(
            root,
            sender_vendor="claude-code",
            sender_name="bob-reviewer",
            sender_session=CLAUDE_SESSION,
            reply_transport="claude_send_message",
            reply_address=CLAUDE_SESSION,
            status_value="received",
            now=self.base_time + dt.timedelta(seconds=2),
        )
        ack_id = protocol.parse_exact_bytes(ack)["message_id"]
        renewal_time = self.base_time + dt.timedelta(minutes=12)
        renewal = _request(
            now=renewal_time,
            idempotency_key=protocol.parse_exact_bytes(root)["action"][
                "idempotency_key"
            ],
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
            now=renewal_time,
        )
        # Model a transport-delayed renewal: its sender-side intent predates
        # receiver work that exists by the time the renewal is ingested.
        _append_intent(
            self.binding,
            ack,
            sender_participant_id=CLAUDE_PARTICIPANT,
            recipient_participant_id=CODEX_PARTICIPANT,
            context=causal.CausalContext(
                root_id,
                (root_id,),
                (),
                (root_id,),
            ),
            now=renewal_time + dt.timedelta(seconds=1),
            accepted=True,
        )
        return renewal, ack_id, root_id

    def test_prior_validation_decodes_only_a_matching_observation(self) -> None:
        unrelated = _request(now=self.base_time)
        target = _request(now=self.base_time + dt.timedelta(seconds=1))
        target_id = protocol.parse_exact_bytes(target)["message_id"]
        for raw, recipient_id in (
            (unrelated, CLAUDE_PARTICIPANT),
            (target, CLAUDE_PARTICIPANT),
        ):
            observed = journal.append_record(
                self.binding,
                event_type="message.inbound.observed",
                exact_message=raw,
                attributes={"source": "test_exact_bytes"},
            )
            journal.append_record(
                self.binding,
                event_type="message.inbound.validated",
                attributes={
                    "observed_record_id": observed["record_id"],
                    "message_id": protocol.parse_exact_bytes(raw)["message_id"],
                    "recipient_participant_id": recipient_id,
                },
            )

        with project.project_transaction(self.binding):
            # Populate the transaction's verified journal cache before
            # measuring only the candidate-comparison decode work.
            journal.replay_records(self.binding)
            with (
                mock.patch.object(
                    journal,
                    "decode_exact_message",
                    wraps=journal.decode_exact_message,
                ) as decode,
                mock.patch.object(
                    journal,
                    "replay_records",
                    wraps=journal.replay_records,
                ) as replay,
            ):
                validation_record = inbound.prior_inbound_validation(
                    self.binding,
                    raw=target,
                    message_id=target_id,
                    recipient_participant_id=CLAUDE_PARTICIPANT,
                )

        self.assertIsNotNone(validation_record)
        self.assertEqual(decode.call_count, 1)
        self.assertEqual(replay.call_count, 2)
        validation_call, observation_call = replay.call_args_list
        self.assertEqual(
            validation_call.kwargs,
            {"event_types": {"message.inbound.validated"}},
        )
        self.assertEqual(
            observation_call.kwargs["event_types"],
            {"message.inbound.observed"},
        )
        self.assertEqual(len(observation_call.kwargs["record_ids"]), 1)

    def test_stale_request_is_held_once_and_exact_redelivery_stays_held(self) -> None:
        renewal, missing_id, root_id = self._stale_renewal()
        path = self.base / "stale-renewal.cam1.json"
        path.write_bytes(renewal)
        path.chmod(0o600)
        command = self.tool_command(
            "message",
            "ingest",
            "--message",
            str(path),
            "--as-participant",
            "bob-reviewer",
            "--renewal-of",
            root_id,
        )

        first = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        second = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        first_stdout, first_stderr = first.communicate(timeout=30)
        second_stdout, second_stderr = second.communicate(timeout=30)

        self.assertEqual(first.returncode, 4, first_stderr or first_stdout)
        self.assertEqual(second.returncode, 4, second_stderr or second_stdout)
        payloads = [
            json.loads(first_stderr or first_stdout),
            json.loads(second_stderr or second_stdout),
        ]
        self.assertEqual(
            {payload["status"] for payload in payloads}, {"held_for_clarification"}
        )
        self.assertEqual(
            {payload["error"]["code"] for payload in payloads},
            {"causal.stale_instruction"},
        )
        self.assertTrue(any(payload["duplicate"] for payload in payloads))
        self.assertTrue(any(not payload["duplicate"] for payload in payloads))
        records = journal.replay_records(self.binding)
        held_records = [
            record
            for record in records
            if record["event_type"] == "message.inbound.validated"
            and record["attributes"].get("assessment") == "held_for_clarification"
        ]
        self.assertEqual(len(held_records), 1)
        self.assertEqual(
            held_records[0]["attributes"]["causal_assessment"]["missing_frontier"],
            [missing_id],
        )
        renewal_id = protocol.parse_exact_bytes(renewal)["message_id"]
        self.assertNotIn(renewal_id, self.store.snapshot().lifecycle.entries)
        self.assertFalse(held_records[0]["attributes"]["lifecycle_committed"])
        self.assertFalse(held_records[0]["attributes"]["action_authorized"])

        repeated = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(repeated.returncode, 4, repeated.stderr or repeated.stdout)
        self.assertEqual(
            len(
                [
                    record
                    for record in journal.replay_records(self.binding)
                    if record["event_type"] == "message.inbound.validated"
                    and record["attributes"].get("assessment")
                    == "held_for_clarification"
                ]
            ),
            1,
        )

    def test_expired_stale_request_registers_expiry_before_causal_hold(self) -> None:
        renewal, missing_id, root_id = self._stale_renewal()
        assessment = causal.assess_inbound_order(
            journal.replay_records(
                self.binding,
                event_types=causal.CAUSAL_JOURNAL_EVENT_TYPES,
            ),
            renewal,
            local_participant_id=CLAUDE_PARTICIPANT,
            sender_participant_id=CODEX_PARTICIPANT,
        )
        self.assertTrue(assessment.held)
        self.assertEqual(assessment.missing_frontier, (missing_id,))

        envelope = protocol.parse_exact_bytes(renewal)
        expiry = dt.datetime.fromisoformat(envelope["expires_at"][:-1] + "+00:00")
        code, payload = self._fixed_ingest(
            renewal,
            as_participant="bob-reviewer",
            renewal_of=root_id,
            observed_at=expiry + dt.timedelta(seconds=1),
        )

        self.assertEqual(code, 2, payload)
        self.assertEqual(payload["error"]["code"], "state.root_expired")
        renewal_id = envelope["message_id"]
        entry = self.store.snapshot().lifecycle.entries[renewal_id]
        self.assertEqual(entry.state.value, "expired_unconfirmed")
        self.assertNotIn(
            renewal_id,
            {
                record["attributes"].get("message_id")
                for record in journal.replay_records(
                    self.binding,
                    event_types={"message.inbound.validated"},
                )
            },
        )

    def test_post_activation_instruction_without_intent_is_distinctly_held(
        self,
    ) -> None:
        raw = _request(now=dt.datetime.now(dt.UTC).replace(microsecond=0))

        code, payload = inbound.ingest_message(
            self.binding,
            message_path=None,
            exact_message=raw,
            observed_source="test_exact_bytes",
            as_participant="bob-reviewer",
            renewal_of=None,
        )

        self.assertEqual(code, 4, payload)
        self.assertEqual(payload["status"], "held_for_clarification")
        self.assertEqual(payload["error"]["code"], "causal.intent_missing")
        self.assertFalse(payload["action_authorized"])

    def test_malformed_journal_context_has_distinct_fail_closed_diagnostic(
        self,
    ) -> None:
        raw = _request(now=dt.datetime.now(dt.UTC).replace(microsecond=0))
        envelope = protocol.parse_exact_bytes(raw)
        journal.append_record(
            self.binding,
            event_type="message.outbound.intent",
            exact_message=raw,
            attributes={
                "participant_id": CLAUDE_PARTICIPANT,
                "sender_participant_id": CODEX_PARTICIPANT,
                "recipient_participant_id": CLAUDE_PARTICIPANT,
                "message_id": envelope["message_id"],
                "renewal_of": None,
                "causal_context": {
                    "format": causal.CAUSAL_FORMAT,
                    "conversation_id": envelope["message_id"],
                    "depends_on": [envelope["message_id"], envelope["message_id"]],
                    "supersedes": [],
                    "recipient_frontier": [],
                },
            },
        )

        code, payload = inbound.ingest_message(
            self.binding,
            message_path=None,
            exact_message=raw,
            observed_source="test_exact_bytes",
            as_participant="bob-reviewer",
            renewal_of=None,
        )

        self.assertEqual(code, 4, payload)
        self.assertEqual(payload["status"], "held_for_clarification")
        self.assertEqual(payload["error"]["code"], "causal.context")
        self.assertNotEqual(payload["error"]["code"], "causal.stale_instruction")
        self.assertFalse(payload["action_authorized"])

    def test_held_cancel_does_not_block_fresh_causally_current_cancel(self) -> None:
        now = dt.datetime.now(dt.UTC).replace(microsecond=0)
        root = _request(now=now)
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
        root_code, root_payload = self._fixed_ingest(
            root,
            as_participant="bob-reviewer",
            observed_at=now + dt.timedelta(seconds=1),
        )
        self.assertEqual(root_code, 0, root_payload)
        ack = builders.build_ack(
            root,
            sender_vendor="claude-code",
            sender_name="bob-reviewer",
            sender_session=CLAUDE_SESSION,
            reply_transport="claude_send_message",
            reply_address=CLAUDE_SESSION,
            status_value="received",
            now=now + dt.timedelta(seconds=2),
        )
        ack_id = protocol.parse_exact_bytes(ack)["message_id"]
        _append_intent(
            self.binding,
            ack,
            sender_participant_id=CLAUDE_PARTICIPANT,
            recipient_participant_id=CODEX_PARTICIPANT,
            context=causal.CausalContext(
                root_id,
                (root_id,),
                (),
                (root_id,),
            ),
            now=now + dt.timedelta(seconds=2),
            accepted=True,
        )

        def cancel(at: dt.datetime) -> bytes:
            return builders.build_cancel(
                root,
                sender_vendor="codex",
                sender_name="project-coordinator",
                sender_session=CODEX_SESSION,
                reply_transport="codex_queue",
                reply_address=CODEX_SESSION,
                authority="Test operator",
                authorization_reference="operator requested cancellation",
                authorization_verified_at=_utc_text(at),
                authorization_expires_at=_utc_text(at + dt.timedelta(minutes=10)),
                now=at,
            )

        stale_cancel = cancel(now + dt.timedelta(seconds=3))
        stale_id = protocol.parse_exact_bytes(stale_cancel)["message_id"]
        _append_intent(
            self.binding,
            stale_cancel,
            sender_participant_id=CODEX_PARTICIPANT,
            recipient_participant_id=CLAUDE_PARTICIPANT,
            context=causal.CausalContext(root_id, (root_id,), (), ()),
            now=now + dt.timedelta(seconds=3),
        )
        stale_code, stale_payload = self._fixed_ingest(
            stale_cancel,
            as_participant="bob-reviewer",
            observed_at=now + dt.timedelta(seconds=4),
        )
        self.assertEqual(stale_code, 4, stale_payload)
        self.assertFalse(stale_payload["lifecycle_committed"])
        self.assertNotIn(stale_id, self.store.snapshot().lifecycle.entries)

        repeated_code, repeated_payload = self._fixed_ingest(
            stale_cancel,
            as_participant="bob-reviewer",
            observed_at=now + dt.timedelta(seconds=5),
        )
        self.assertEqual(repeated_code, 4, repeated_payload)
        self.assertTrue(repeated_payload["duplicate"])
        self.assertFalse(repeated_payload["lifecycle_committed"])
        self.assertNotIn(stale_id, self.store.snapshot().lifecycle.entries)

        fresh_cancel = cancel(now + dt.timedelta(seconds=6))
        fresh_id = protocol.parse_exact_bytes(fresh_cancel)["message_id"]
        _append_intent(
            self.binding,
            fresh_cancel,
            sender_participant_id=CODEX_PARTICIPANT,
            recipient_participant_id=CLAUDE_PARTICIPANT,
            context=causal.CausalContext(
                root_id,
                (root_id,),
                (),
                (ack_id,),
            ),
            now=now + dt.timedelta(seconds=6),
        )
        fresh_code, fresh_payload = self._fixed_ingest(
            fresh_cancel,
            as_participant="bob-reviewer",
            observed_at=now + dt.timedelta(seconds=7),
        )

        self.assertEqual(fresh_code, 0, fresh_payload)
        self.assertTrue(fresh_payload["lifecycle_committed"])
        self.assertIn(fresh_id, self.store.snapshot().lifecycle.entries)
        held = [
            record
            for record in journal.replay_records(self.binding)
            if record["event_type"] == "message.inbound.validated"
            and record["attributes"].get("message_id") == stale_id
        ]
        self.assertEqual(len(held), 1)
        self.assertEqual(held[0]["attributes"]["assessment"], "held_for_clarification")

    def test_stale_cancel_is_held_before_terminal_lifecycle_interpretation(
        self,
    ) -> None:
        now = self.base_time
        root = _request(now=now)
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
        root_code, root_payload = self._fixed_ingest(
            root,
            as_participant="bob-reviewer",
            observed_at=now + dt.timedelta(seconds=1),
        )
        self.assertEqual(root_code, 0, root_payload)

        rejected = builders.build_ack(
            root,
            sender_vendor="claude-code",
            sender_name="bob-reviewer",
            sender_session=CLAUDE_SESSION,
            reply_transport="claude_send_message",
            reply_address=CLAUDE_SESSION,
            status_value="rejected",
            now=now + dt.timedelta(seconds=2),
        )
        rejected_id = protocol.parse_exact_bytes(rejected)["message_id"]
        _append_intent(
            self.binding,
            rejected,
            sender_participant_id=CLAUDE_PARTICIPANT,
            recipient_participant_id=CODEX_PARTICIPANT,
            context=causal.CausalContext(
                root_id,
                (root_id,),
                (),
                (root_id,),
            ),
            now=now + dt.timedelta(seconds=2),
            accepted=True,
        )
        reply_code, reply_payload = self._fixed_ingest(
            rejected,
            as_participant="project-coordinator",
            observed_at=now + dt.timedelta(seconds=3),
        )
        self.assertEqual(reply_code, 0, reply_payload)
        self.assertTrue(self.store.snapshot().lifecycle.entries[root_id].terminal)

        cancel_time = now + dt.timedelta(seconds=4)
        stale_cancel = builders.build_cancel(
            root,
            sender_vendor="codex",
            sender_name="project-coordinator",
            sender_session=CODEX_SESSION,
            reply_transport="codex_queue",
            reply_address=CODEX_SESSION,
            authority="Test operator",
            authorization_reference="operator requested cancellation",
            authorization_verified_at=_utc_text(cancel_time),
            authorization_expires_at=_utc_text(cancel_time + dt.timedelta(minutes=10)),
            now=cancel_time,
        )
        cancel_id = protocol.parse_exact_bytes(stale_cancel)["message_id"]
        _append_intent(
            self.binding,
            stale_cancel,
            sender_participant_id=CODEX_PARTICIPANT,
            recipient_participant_id=CLAUDE_PARTICIPANT,
            context=causal.CausalContext(root_id, (root_id,), (), ()),
            now=cancel_time,
        )

        code, payload = self._fixed_ingest(
            stale_cancel,
            as_participant="bob-reviewer",
            observed_at=cancel_time + dt.timedelta(seconds=1),
        )

        self.assertEqual(code, 4, payload)
        self.assertEqual(payload["error"]["code"], "causal.stale_instruction")
        self.assertEqual(payload["causal"]["missing_frontier"], [rejected_id])
        self.assertFalse(payload["lifecycle_committed"])
        self.assertNotIn(cancel_id, self.store.snapshot().lifecycle.entries)
        self.assertTrue(self.store.snapshot().lifecycle.entries[root_id].terminal)

        duplicate_code, duplicate_payload = self._fixed_ingest(
            stale_cancel,
            as_participant="bob-reviewer",
            observed_at=cancel_time + dt.timedelta(seconds=2),
        )
        self.assertEqual(duplicate_code, 4, duplicate_payload)
        self.assertTrue(duplicate_payload["duplicate"])
        self.assertFalse(duplicate_payload["lifecycle_committed"])
        self.assertNotIn(cancel_id, self.store.snapshot().lifecycle.entries)


class CausalSendIngestConcurrencyTests(ProjectBoundTransportTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tool_checkout_temporary = tempfile.TemporaryDirectory()
        cls.tool_checkout = (
            Path(cls.tool_checkout_temporary.name).resolve() / "clean-cam-checkout"
        )
        shutil.copytree(
            SOURCE_ROOT,
            cls.tool_checkout,
            ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__", "*.pyc"),
        )
        subprocess.run(
            [project.DEFAULT_GIT_BIN, "-C", str(cls.tool_checkout), "init", "--quiet"],
            check=True,
        )
        subprocess.run(
            [project.DEFAULT_GIT_BIN, "-C", str(cls.tool_checkout), "add", "."],
            check=True,
        )
        subprocess.run(
            [
                project.DEFAULT_GIT_BIN,
                "-C",
                str(cls.tool_checkout),
                "-c",
                "user.name=CAM Test",
                "-c",
                "user.email=cam-test@example.invalid",
                "commit",
                "--quiet",
                "-m",
                "test fixture",
            ],
            check=True,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tool_checkout_temporary.cleanup()

    def project_command(self, *arguments: str) -> list[str]:
        return [
            sys.executable,
            str(self.tool_checkout / "tools" / "cam1_project.py"),
            "--project-root",
            str(self.repo),
            "--state-root",
            str(self.state_root),
            "--git-bin",
            project.DEFAULT_GIT_BIN,
            *arguments,
        ]

    def transport_command(
        self,
        *arguments: str,
        claude_bin: Path | None = None,
        codex_bin: Path | None = None,
        project_root: Path | None = None,
    ) -> list[str]:
        command = [
            sys.executable,
            str(self.tool_checkout / "tests" / "_cam1_cli_test_harness.py"),
            "transport",
        ]
        if claude_bin is not None:
            command.extend(("--claude-bin", str(claude_bin)))
        if codex_bin is not None:
            command.extend(("--codex-bin", str(codex_bin)))
        command.extend(
            (
                "--project-root",
                str(project_root or self.repo),
                "--state-root",
                str(self.state_root),
                "--git-bin",
                project.DEFAULT_GIT_BIN,
                *arguments,
            )
        )
        return command

    def setUp(self) -> None:
        super().setUp()
        self.add_claude_participant()
        self.add_codex_participant()
        store = state.StateStore(self.binding)
        route_now = dt.datetime.now(dt.UTC).replace(microsecond=0) + dt.timedelta(
            seconds=1
        )
        _activate_gate(
            self.binding,
            store,
            feature_id=compatibility.COMPATIBILITY_KERNEL_FEATURE_ID,
            now=route_now,
        )
        _activate_gate(
            self.binding,
            store,
            feature_id=causal.CAUSAL_FEATURE_ID,
            now=route_now + dt.timedelta(seconds=5),
        )

    def test_simultaneous_send_and_ingest_serialize_complete_intents(self) -> None:
        now = dt.datetime.now(dt.UTC).replace(microsecond=0)
        inbound = builders.build_request(
            sender_vendor="codex",
            sender_name="example-coordinator",
            sender_session=TRANSPORT_CODEX_SESSION,
            recipient_vendor="claude-code",
            recipient_name="local-worker",
            recipient_session=TRANSPORT_CLAUDE_SESSION,
            reply_transport="codex_queue",
            reply_address=TRANSPORT_CODEX_SESSION,
            risk_class="informational",
            operation="review_inbound",
            intent="Request one bounded inbound review",
            body="Review the inbound fixture without changes.",
            authorization_basis="none",
            now=now,
        )
        inbound_id = protocol.parse_exact_bytes(inbound)["message_id"]
        _append_intent(
            self.binding,
            inbound,
            sender_participant_id=TRANSPORT_CODEX_PARTICIPANT,
            recipient_participant_id=TRANSPORT_CLAUDE_PARTICIPANT,
            context=causal.CausalContext(inbound_id, (), (), ()),
            now=now,
        )
        outbound = builders.build_request(
            sender_vendor="claude-code",
            sender_name="local-worker",
            sender_session=TRANSPORT_CLAUDE_SESSION,
            recipient_vendor="codex",
            recipient_name="example-coordinator",
            recipient_session=TRANSPORT_CODEX_SESSION,
            reply_transport="claude_send_message",
            reply_address=TRANSPORT_CLAUDE_SESSION,
            risk_class="informational",
            operation="review_outbound",
            intent="Request one bounded outbound review",
            body="Review the outbound fixture without changes.",
            authorization_basis="none",
            now=now,
        )
        inbound_path = self.private_envelope("causal-inbound.json", inbound)
        outbound_path = self.private_envelope("causal-outbound.json", outbound)
        self.approved_codex_bin.write_text(
            f"#!{sys.executable}\n"
            "print('Queued message 00000000-0000-4000-8000-000000000999 '",
            encoding="utf-8",
        )
        with self.approved_codex_bin.open("a", encoding="utf-8") as stream:
            stream.write(f"      'for thread {TRANSPORT_CODEX_SESSION}.')\n")
        self.approved_codex_bin.chmod(0o700)
        send = subprocess.Popen(
            self.transport_command(
                "codex-send",
                "--participant",
                "example-coordinator",
                "--thread",
                TRANSPORT_CODEX_SESSION,
                "--envelope",
                str(outbound_path),
                codex_bin=self.approved_codex_bin,
            ),
            cwd=self.repo,
            env=self.transport_environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        ingest = subprocess.Popen(
            self.project_command(
                "message",
                "ingest",
                "--message",
                str(inbound_path),
                "--as-participant",
                "local-worker",
            ),
            cwd=self.repo,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        send_stdout, send_stderr = send.communicate(timeout=30)
        ingest_stdout, ingest_stderr = ingest.communicate(timeout=30)

        self.assertEqual(send.returncode, 0, send_stderr or send_stdout)
        self.assertEqual(ingest.returncode, 0, ingest_stderr or ingest_stdout)
        verification = journal.verify_journal(self.binding)
        self.assertEqual(verification.last_sequence, verification.record_count)
        records = journal.replay_records(self.binding)
        intent_contexts = [
            record["attributes"].get("causal_context")
            for record in records
            if record["event_type"] == "message.outbound.intent"
        ]
        self.assertEqual(len(intent_contexts), 2)
        self.assertTrue(all(context is not None for context in intent_contexts))
        self.assertEqual(
            sum(
                record["event_type"] == "message.inbound.validated"
                and record["attributes"].get("message_id") == inbound_id
                for record in records
            ),
            1,
        )


if __name__ == "__main__":
    unittest.main()
