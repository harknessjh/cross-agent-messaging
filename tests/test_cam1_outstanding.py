# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import uuid
from unittest import mock

from tools.cam1lib import (
    builders,
    journal,
    outstanding,
    project,
    state,
    state_projection,
)
from tools.cam1lib.protocol import CamUsageError, serialize_envelope

if __package__:
    from .test_cam1_project import ProjectTestCase
else:
    from test_cam1_project import ProjectTestCase

NOW = dt.datetime(2026, 10, 1, 12, tzinfo=dt.UTC)
PIDS = ("00000000-0000-4000-8000-000000000101", "00000000-0000-4000-8000-000000000102")
SESSIONS = (
    "00000000-0000-4000-8000-000000000201",
    "00000000-0000-4000-8000-000000000202",
)
NAMES = ("requester", "reviewer")
VENDORS = ("codex", "claude-code")
PRIVATE = "PRIVATE-REPORT-SENTINEL"


class OutstandingTests(ProjectTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.binding = self.initialize()
        self.store = state.StateStore(self.binding)
        self.replies: dict[str, list[bytes]] = {}
        for i in range(2):
            self.store.participant_add(
                participant_id=PIDS[i],
                common_name=NAMES[i],
                display_name=PRIVATE,
                role=PRIVATE,
                vendor=VENDORS[i],
                now=NOW,
            )
            self.bind(i)

    def bind(self, i: int, session: str | None = None) -> None:
        self.store.participant_bind(
            NAMES[i],
            session_id=session or SESSIONS[i],
            session_label=PRIVATE,
            session_kind="interactive",
            operator_reference=PRIVATE,
            bound_at=NOW.isoformat().replace("+00:00", "Z"),
            now=NOW,
        )

    def sender(self, i: int) -> dict[str, str]:
        return dict(
            sender_vendor=VENDORS[i],
            sender_name=NAMES[i],
            sender_session=SESSIONS[i],
            reply_transport="codex_queue" if i == 0 else "claude_send_message",
            reply_address=SESSIONS[i],
        )

    def request(
        self, *, reverse: bool = False, now: dt.datetime = NOW, **kwargs: object
    ) -> bytes:
        i = int(reverse)
        return builders.build_request(
            **self.sender(i),
            recipient_vendor=VENDORS[1 - i],
            recipient_name=NAMES[1 - i],
            recipient_session=SESSIONS[1 - i],
            risk_class="informational",
            operation="test_report",
            intent=PRIVATE,
            body=PRIVATE,
            authorization_basis="none",
            now=now,
            **kwargs,
        )

    def attrs(self, raw: bytes) -> dict[str, object]:
        env = json.loads(raw)
        i = NAMES.index(env["claimed_sender"]["agent_name"])
        return dict(
            message_id=env["message_id"],
            sender_participant_id=PIDS[i],
            recipient_participant_id=PIDS[1 - i],
        )

    def append(
        self, kind: str, *, raw: bytes | None = None, **attrs: object
    ) -> dict[str, object]:
        return journal.append_record(
            self.binding, event_type=kind, exact_message=raw, attributes=attrs, now=NOW
        )

    def intent(self, raw: bytes, **extra: object) -> dict[str, object]:
        attrs = {
            **self.attrs(raw),
            "route_address": PRIVATE,
            "private_attribute": PRIVATE,
            **extra,
        }
        return self.append("message.outbound.intent", raw=raw, **attrs)

    def outcome(
        self, intent: dict[str, object], status: str = "accepted", **extra: object
    ) -> None:
        attrs = intent["attributes"]
        self.append(
            "transport.accepted" if status == "accepted" else "transport.not_accepted",
            **{
                "intent_record_id": intent["record_id"],
                "message_id": attrs["message_id"],
                "participant_id": attrs["recipient_participant_id"],
                "delivery_state": status,
                "lifecycle_state_committed": True,
                **extra,
            },
        )

    def root(
        self,
        raw: bytes | None = None,
        *,
        transport: str | None = "accepted",
        register: bool = True,
    ) -> bytes:
        raw = raw or self.request()
        intent = self.intent(raw)
        if register:
            self.store.lifecycle_root(raw, now=NOW)
        if transport:
            self.outcome(intent, transport)
        return raw

    def intake(
        self,
        raw: bytes,
        *,
        held: bool = False,
        prior: str | None = None,
        **extra: object,
    ) -> dict[str, object]:
        observed = self.append("message.inbound.observed", raw=raw)
        attrs = {
            **self.attrs(raw),
            "observed_record_id": observed["record_id"],
            "assessment": "held_for_clarification" if held else "validated",
            "lifecycle_committed": not held,
            **extra,
        }
        if prior:
            attrs["prior_validated_record_id"] = prior
        return self.append(
            "message.inbound.duplicate" if prior else "message.inbound.validated",
            **attrs,
        )

    def reply(
        self,
        root: bytes,
        status: str,
        *,
        commit: bool = True,
        transport: str | None = "accepted",
    ) -> bytes:
        env = json.loads(root)
        i = NAMES.index(env["recipient"]["agent_name"])
        history = self.replies.setdefault(env["message_id"], [])
        options = dict(
            **self.sender(i),
            now=NOW + dt.timedelta(seconds=len(history) + 1),
            previous_responses=history,
        )
        if (
            status in {"received", "needs_human_confirmation", "accepted", "rejected"}
            and not history
        ):
            raw = builders.build_ack(
                root,
                status_value=status,
                **{
                    key: value
                    for key, value in options.items()
                    if key != "previous_responses"
                },
            )
        elif status in {"accepted", "started"}:
            raw = builders.build_status(
                root, status_value=status, body=PRIVATE, **options
            )
        elif status == "completed":
            raw = builders.build_result(root, body=PRIVATE, **options)
        else:
            raw = builders.build_error(root, body=PRIVATE, **options)
        intent = self.intent(raw)
        if commit:
            self.store.lifecycle_reply(raw, now=options["now"])
            history.append(raw)
        if transport:
            self.outcome(intent, transport, lifecycle_state_committed=commit)
        return raw

    def report(
        self, participant: str = "requester", **kwargs: object
    ) -> dict[str, object]:
        kwargs.setdefault("now", NOW + dt.timedelta(seconds=30))
        result = self.store.outstanding(participant, **kwargs)
        text = json.dumps(result)
        self.assertNotIn(PRIVATE, text)
        for session in SESSIONS:
            self.assertNotIn(session, text)
        return result

    def items(
        self, report: dict[str, object], section: str = "outgoing_nonterminal"
    ) -> list[dict[str, object]]:
        return report["sections"][section]["items"]

    def test_informational_outgoing_is_not_an_obligation(self) -> None:
        root = self.root()
        result = self.report()
        row = self.items(result)[0]
        self.assertEqual(result["format"], "CAM-OUTSTANDING/1")
        self.assertEqual(row["root_message_id"], json.loads(root)["message_id"])
        self.assertEqual(row["root_transport"]["status"], "accepted")
        self.assertEqual(
            row["journaled_intake_evidence"]["status"], "no_recorded_evidence"
        )
        self.assertNotIn("overdue", json.dumps(result))
        self.assertFalse(result["coverage"]["terminal_intake_tracking"])

    def test_pending_and_received_are_attention_not_accepted(self) -> None:
        root = self.root()
        self.assertEqual(
            self.items(self.report("reviewer", include_attention=True), "attention"), []
        )
        self.intake(root)
        self.assertEqual(
            len(
                self.items(self.report("reviewer", include_attention=True), "attention")
            ),
            1,
        )
        self.reply(root, "received")
        report = self.report("reviewer", include_attention=True)
        self.assertEqual(self.items(report, "incoming_accepted"), [])
        self.assertEqual(
            self.items(report, "attention")[0]["recorded_state"], "received"
        )
        self.reply(root, "accepted")
        self.assertEqual(
            len(self.items(self.report("reviewer"), "incoming_accepted")), 1
        )

    def test_accepted_and_started_survive_initial_expiry(self) -> None:
        for state_name in ("accepted", "started"):
            with self.subTest(state_name=state_name):
                root = self.root()
                self.reply(root, "accepted")
                if state_name == "started":
                    self.reply(root, "started")
                rows = self.items(
                    self.report("reviewer", now=NOW + dt.timedelta(hours=1)),
                    "incoming_accepted",
                )
                row = next(
                    item
                    for item in rows
                    if item["root_message_id"] == json.loads(root)["message_id"]
                )
                self.assertEqual(row["effective_state"], state_name)
                self.assertEqual(
                    row["expiry_annotation"],
                    "initial_deadline_passed_not_work_deadline",
                )

    def test_expiry_boundary_is_derived_without_mutation(self) -> None:
        self.root()
        before = self.binding.journal_path.read_bytes()
        for seconds, expected in (
            (599, "pending"),
            (600, "expired_unconfirmed"),
            (601, "expired_unconfirmed"),
        ):
            row = self.items(self.report(now=NOW + dt.timedelta(seconds=seconds)))[0]
            self.assertEqual(row["recorded_state"], "pending")
            self.assertEqual(row["effective_state"], expected)
        self.assertEqual(self.binding.journal_path.read_bytes(), before)

    def test_terminal_states_disappear_without_claiming_requester_intake(self) -> None:
        for terminal in ("completed", "failed", "rejected"):
            root = self.root()
            if terminal == "completed":
                self.reply(root, "accepted")
            self.reply(root, terminal)
        self.assertEqual(self.items(self.report()), [])

    def test_causal_hold_without_lifecycle_is_attention(self) -> None:
        root = self.request()
        proof = self.intake(root, held=True)
        self.intake(root, prior=proof["record_id"])
        row = self.items(self.report("reviewer", include_attention=True), "attention")[
            0
        ]
        self.assertFalse(row["lifecycle_recorded"])
        self.assertEqual(
            row["journaled_intake_evidence"]["status"], "held_for_clarification"
        )
        self.assertFalse(row["journaled_intake_evidence"]["lifecycle_committed"])
        self.assertEqual(row["journaled_intake_evidence"]["duplicate_count"], 1)
        self.assertEqual(self.items(self.report("reviewer"), "incoming_accepted"), [])

    def test_protocol_hold_and_received_expiry_are_distinct(self) -> None:
        for status, expected in (
            ("needs_human_confirmation", "expired_unconfirmed"),
            ("received", "received"),
        ):
            root = self.root()
            self.intake(root)
            self.reply(root, status)
            rows = self.items(
                self.report(
                    "reviewer", include_attention=True, now=NOW + dt.timedelta(hours=1)
                ),
                "attention",
            )
            row = next(
                item
                for item in rows
                if item["root_message_id"] == json.loads(root)["message_id"]
            )
            self.assertEqual(row["effective_state"], expected)

    def test_raw_observation_is_not_intake(self) -> None:
        root = self.root()
        self.append("message.inbound.observed", raw=root)
        self.assertEqual(
            self.items(self.report("reviewer", include_attention=True), "attention"), []
        )

    def test_missing_observation_or_duplicate_parent_is_not_intake(self) -> None:
        root = self.root()
        self.append(
            "message.inbound.validated",
            **self.attrs(root),
            observed_record_id=str(uuid.uuid4()),
            assessment="validated",
            lifecycle_committed=True,
        )
        self.intake(root, prior=str(uuid.uuid4()))
        report = self.report("reviewer", include_attention=True)
        self.assertEqual(self.items(report, "attention"), [])
        self.assertEqual(report["coverage"]["unusable_audit_records"], 2)

    def test_exact_duplicate_preserves_intake_classification(self) -> None:
        root = self.root()
        proof = self.intake(root)
        self.intake(root, prior=proof["record_id"])
        row = self.items(self.report())[0]
        self.assertEqual(row["journaled_intake_evidence"]["record_count"], 2)
        self.assertEqual(row["journaled_intake_evidence"]["duplicate_count"], 1)
        self.assertEqual(row["journaled_intake_evidence"]["status"], "validated")

    def test_wrong_recipient_fails_closed_without_private_values(self) -> None:
        root = self.root()
        self.intake(root, recipient_participant_id=PIDS[0])
        with self.assertRaises(CamUsageError) as error:
            self.report()
        self.assertEqual(error.exception.code, "outstanding.evidence_conflict")
        self.assertNotIn(PRIVATE, str(error.exception))

    def test_conflicting_exact_bytes_fail_closed(self) -> None:
        root = self.root()
        changed = json.loads(root)
        changed["body"] += PRIVATE
        self.intent(serialize_envelope(changed))
        with self.assertRaises(CamUsageError) as error:
            self.report()
        self.assertNotIn(PRIVATE, str(error.exception))

    def test_legacy_unattributed_lifecycle_is_counted_not_guessed(self) -> None:
        self.store.lifecycle_root(self.request(), now=NOW)
        report = self.report()
        self.assertEqual(self.items(report), [])
        self.assertEqual(report["coverage"]["unattributed_lifecycle_roots"], 1)

    def test_label_only_rebind_keeps_current_session(self) -> None:
        self.root()
        self.bind(0)
        row = self.items(self.report())[0]
        self.assertEqual(
            row["local_binding"],
            {
                "generation": 1,
                "session_relation": "current_session",
                "generation_relation": "earlier_generation",
            },
        )

    def test_new_session_and_retirement_preserve_history(self) -> None:
        self.root()
        self.bind(0, str(uuid.uuid4()))
        self.assertEqual(
            self.items(self.report())[0]["local_binding"]["session_relation"],
            "earlier_session",
        )
        self.store.participant_retire("requester", reason=PRIVATE, now=NOW)
        report = self.report(PIDS[0])
        self.assertEqual(report["participant"]["status"], "retired")
        self.assertEqual(len(self.items(report)), 1)

    def test_transport_outcomes_and_unregistered_orphan(self) -> None:
        for status in ("accepted", "not_attempted", "unknown", None):
            self.root(transport=status, register=status is not None)
        rows = self.items(self.report())
        self.assertEqual(
            {row["root_transport"]["status"] for row in rows},
            {"accepted", "not_attempted", "unknown", "orphaned"},
        )
        orphan = next(
            row for row in rows if row["root_transport"]["status"] == "orphaned"
        )
        self.assertFalse(orphan["lifecycle_recorded"])

    def test_no_intent_is_not_proven_not_attempted(self) -> None:
        root = self.request()
        self.store.lifecycle_root(root, now=NOW)
        self.intake(root)
        self.assertEqual(
            self.items(self.report())[0]["root_transport"]["status"],
            "no_recorded_evidence",
        )

    def test_later_acceptance_cannot_hide_unresolved_attempt(self) -> None:
        root = self.root(transport=None)
        self.outcome(self.intent(root))
        summary = self.items(self.report())[0]["root_transport"]
        self.assertEqual(summary["status"], "accepted")
        self.assertEqual(summary["unresolved_attempt_count"], 1)
        self.assertEqual(summary["attempt_count"], 2)

    def test_conflicting_outcomes_are_unknown(self) -> None:
        raw = self.root(transport=None)
        intent = self.intent(raw)
        self.outcome(intent)
        self.outcome(intent, "not_attempted")
        summary = self.items(self.report())[0]["root_transport"]
        self.assertEqual(summary["status"], "unknown")
        self.assertEqual(summary["conflicting_outcome_count"], 1)

    def test_accepted_transport_without_lifecycle_does_not_accept_work(self) -> None:
        root = self.root()
        self.reply(root, "accepted", commit=False)
        report = self.report("reviewer")
        self.assertEqual(self.items(report, "incoming_accepted"), [])
        row = self.items(self.report())[0]
        self.assertEqual(row["recorded_state"], "pending")
        self.assertIsNone(row["latest_reply_attempt"]["committed_sequence"])
        self.assertEqual(row["latest_reply_attempt"]["transport"]["status"], "accepted")

    def test_unknown_terminal_reply_attempt_does_not_complete_root(self) -> None:
        root = self.root()
        self.reply(root, "accepted")
        self.reply(root, "completed", commit=False, transport="unknown")
        row = self.items(self.report("reviewer"), "incoming_accepted")[0]
        self.assertEqual(row["recorded_state"], "accepted")
        self.assertEqual(
            row["latest_reply_attempt"]["transport"]["unresolved_attempt_count"], 1
        )

    def test_limits_counts_order_and_disabled_attention(self) -> None:
        roots = [self.root() for _ in range(3)]
        report = self.report(limit=2)
        section = report["sections"]["outgoing_nonterminal"]
        self.assertEqual(
            (section["total"], section["shown"], section["omitted"]), (3, 2, 1)
        )
        self.assertEqual(
            [row["root_message_id"] for row in section["items"]],
            [json.loads(root)["message_id"] for root in roots[:2]],
        )
        self.assertFalse(report["sections"]["attention"]["enabled"])
        self.assertIsNone(report["sections"]["attention"]["total"])
        self.assertEqual(report, self.report(limit=2))

    def test_bad_limits_and_unknown_selector_do_not_leak_values(self) -> None:
        for limit in (0, 201, True, PRIVATE):
            with self.assertRaises(CamUsageError) as error:
                self.report(limit=limit)
            self.assertNotIn(PRIVATE, str(error.exception))
        with self.assertRaises(CamUsageError) as error:
            self.report(PRIVATE)
        self.assertNotIn(PRIVATE, str(error.exception))

    def test_stale_missing_or_corrupt_disposable_projection_is_ignored(self) -> None:
        self.root()
        path = state.state_projection_path(self.binding)
        for content in (None, b"{}", b"PRIVATE-REPORT-SENTINEL"):
            if content is None:
                path.unlink()
            else:
                path.write_bytes(content)
            before = self.binding.journal_path.read_bytes()
            self.assertEqual(len(self.items(self.report())), 1)
            self.assertEqual(self.binding.journal_path.read_bytes(), before)
            self.assertEqual(path.read_bytes() if path.exists() else None, content)

    def test_single_verification_and_render_outside_lock_with_no_writes(self) -> None:
        for _ in range(3):
            self.root()
        original = outstanding.render

        def unlocked(*args: object, **kwargs: object) -> dict[str, object]:
            self.assertIsNone(project.current_project_transaction(self.binding))
            descriptor = os.open(self.binding.transaction_lock_path, os.O_RDWR)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return original(*args, **kwargs)
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)

        with (
            mock.patch.object(
                journal, "_verify_records", wraps=journal._verify_records
            ) as verification,
            mock.patch.object(
                state_projection,
                "_empty_snapshot",
                wraps=state_projection._empty_snapshot,
            ) as replay,
            mock.patch.object(
                outstanding, "parse_exact_bytes", wraps=outstanding.parse_exact_bytes
            ) as parse,
            mock.patch.object(outstanding, "render", side_effect=unlocked),
            mock.patch.object(
                state_projection,
                "_write_projections",
                side_effect=AssertionError("write"),
            ),
            mock.patch.object(
                journal, "append_record", side_effect=AssertionError("append")
            ),
        ):
            self.report()
        self.assertEqual(verification.call_count, 1)
        self.assertEqual(replay.call_count, 1)
        self.assertEqual(parse.call_count, 3)

    def test_nested_transaction_refused(self) -> None:
        with (
            project.project_transaction(self.binding),
            self.assertRaises(CamUsageError) as error,
        ):
            self.report()
        self.assertEqual(error.exception.code, "outstanding.nested_transaction")

    def test_cli_read_only_and_redacted_errors(self) -> None:
        self.root()
        before = self.binding.journal_path.read_bytes()
        result = self.run_tool(
            "state", "outstanding", "--participant", "requester", "--limit", "1"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["format"], outstanding.FORMAT)
        self.assertNotIn(PRIVATE, result.stdout + result.stderr)
        self.assertEqual(self.binding.journal_path.read_bytes(), before)
        invalid = self.run_tool(
            "state", "outstanding", "--participant", PRIVATE, "--limit", PRIVATE
        )
        self.assertNotEqual(invalid.returncode, 0)
        self.assertNotIn(PRIVATE, invalid.stdout + invalid.stderr)

    def test_hello_and_challenge_roots_and_verify_reply(self) -> None:
        endpoints = dict(
            **self.sender(0),
            recipient_vendor=VENDORS[1],
            recipient_name=NAMES[1],
            recipient_session=SESSIONS[1],
            now=NOW,
        )
        hello = self.root(builders.build_hello(**endpoints))
        challenge = self.root(builders.build_challenge(**endpoints))
        self.assertEqual(
            {row["root_type"] for row in self.items(self.report())},
            {"hello", "challenge"},
        )
        self.reply(hello, "received")
        verify = builders.build_verify(
            challenge, **self.sender(1), now=NOW + dt.timedelta(seconds=1)
        )
        self.intent(verify)
        self.store.lifecycle_reply(verify, now=NOW + dt.timedelta(seconds=1))
        self.assertEqual(self.items(self.report()), [])

    def test_cancel_pending_received_rejected_and_accepted(self) -> None:
        for status in (None, "received", "rejected", "accepted"):
            root = self.root()
            cancel = builders.build_cancel(
                root,
                **self.sender(0),
                authority="Synthetic operator",
                authorization_reference=PRIVATE,
                authorization_verified_at="2026-10-01T12:00:00Z",
                authorization_expires_at="2026-10-01T12:09:00Z",
                now=NOW,
            )
            self.root(cancel)
            if status:
                self.reply(cancel, status)
            rows = {row["root_message_id"]: row for row in self.items(self.report())}
            root_id, cancel_id = (
                json.loads(root)["message_id"],
                json.loads(cancel)["message_id"],
            )
            if status == "accepted":
                self.assertNotIn(root_id, rows)
                self.assertNotIn(cancel_id, rows)
            else:
                self.assertIn(root_id, rows)
                if status == "rejected":
                    self.assertNotIn(cancel_id, rows)
                else:
                    self.assertEqual(rows[cancel_id]["cancels_root_id"], root_id)

    def test_renewal_retains_expired_predecessor_as_separate_row(self) -> None:
        key = str(uuid.uuid4())
        old = self.root(self.request(idempotency_key=key))
        old_id = json.loads(old)["message_id"]
        new = self.request(idempotency_key=key, now=NOW + dt.timedelta(minutes=11))
        self.intent(new)
        self.store.lifecycle_root(
            new, renewal_of=old_id, now=NOW + dt.timedelta(minutes=11)
        )
        new_id = json.loads(new)["message_id"]
        rows = {
            row["root_message_id"]: row
            for row in self.items(self.report(now=NOW + dt.timedelta(minutes=12)))
        }
        self.assertEqual(rows[old_id]["recorded_state"], "expired_unconfirmed")
        self.assertEqual(rows[old_id]["superseded_by"], new_id)
        self.assertEqual(rows[new_id]["renewal_of"], old_id)
        self.assertEqual(rows[new_id]["effective_state"], "pending")

    def test_late_rejection_is_terminal(self) -> None:
        root = self.root()
        late = builders.build_late_rejection(
            root, **self.sender(1), now=NOW + dt.timedelta(minutes=11)
        )
        self.intent(late)
        self.store.lifecycle_reply(late, now=NOW + dt.timedelta(minutes=11))
        self.assertEqual(
            self.items(self.report(now=NOW + dt.timedelta(minutes=12))), []
        )

    def test_uppercase_wire_uuid_is_canonicalized(self) -> None:
        envelope = json.loads(self.request())
        envelope["message_id"] = envelope["message_id"].upper()
        self.root(serialize_envelope(envelope))
        self.assertEqual(
            self.items(self.report())[0]["root_message_id"],
            envelope["message_id"].lower(),
        )

    def test_unbound_and_stale_participants_can_be_inspected(self) -> None:
        self.store.participant_add(
            common_name="unbound",
            display_name=PRIVATE,
            role=None,
            vendor="codex",
            now=NOW,
        )
        self.assertEqual(self.report("unbound")["participant"]["status"], "unbound")
        self.root()
        self.store.participant_invalidate("requester", reason=PRIVATE, now=NOW)
        self.assertEqual(self.report()["participant"]["status"], "stale")

    def test_snapshot_position_mismatch_fails_closed(self) -> None:
        self.root()
        snapshot = self.store.snapshot()
        records = journal.replay_records(self.binding)
        with self.assertRaises(CamUsageError) as error:
            outstanding.render(
                snapshot,
                records[:-1],
                project_name="example",
                participant="requester",
                as_of=NOW,
            )
        self.assertEqual(error.exception.code, "outstanding.snapshot_mismatch")

    def test_report_uses_snapshot_even_if_journal_advances_during_render(self) -> None:
        self.root()
        original = outstanding.render
        sequence = journal.verify_journal(self.binding).record_count

        def advanced(*args: object, **kwargs: object) -> dict[str, object]:
            self.append("note.test", private=PRIVATE)
            return original(*args, **kwargs)

        with mock.patch.object(outstanding, "render", side_effect=advanced):
            report = self.report()
        self.assertEqual(report["journal_position"]["sequence"], sequence)
        self.assertEqual(
            journal.verify_journal(self.binding).record_count, sequence + 1
        )

    def test_cli_attribution_error_does_not_disclose_private_values(self) -> None:
        root = self.root()
        self.intake(root, sender_participant_id=PIDS[1])
        result = self.run_tool("state", "outstanding", "--participant", "requester")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(
            json.loads(result.stderr)["error"]["code"], "outstanding.evidence_conflict"
        )
        self.assertNotIn(PRIVATE, result.stdout + result.stderr)

    def test_only_confirmed_enrollment_enters_binding_history(self) -> None:
        session = str(uuid.uuid4())
        proposal, _ = self.store.participant_enrollment_propose(
            common_name="newcomer",
            display_name=PRIVATE,
            role=None,
            vendor="codex",
            session_id=session,
            session_label=PRIVATE,
            session_kind="interactive",
            session_git_top_level=str(self.binding.git_top_level),
            session_git_common_dir=str(self.binding.git_common_dir),
            discovery_source="CODEX_THREAD_ID",
            execution_context={
                "cam_checkout": str(self.repo),
                "validation_profile_sha256": "a" * 64,
                "project_root": str(self.binding.git_top_level),
                "product_executable": "/example/bin/codex",
                "product_executable_source": "explicit_candidate",
            },
            now=NOW,
        )
        with self.assertRaises(CamUsageError):
            self.report("newcomer")
        participant, _ = self.store.participant_enrollment_confirm(
            proposal.proposal_id,
            expected_proposal_sha256=proposal.proposal_sha256,
            operator_reference=PRIVATE,
            confirmed_at="2026-10-01T12:00:00Z",
            now=NOW,
        )
        root = builders.build_request(
            sender_vendor="codex",
            sender_name="newcomer",
            sender_session=session,
            recipient_vendor=VENDORS[1],
            recipient_name=NAMES[1],
            recipient_session=SESSIONS[1],
            reply_transport="codex_queue",
            reply_address=session,
            risk_class="informational",
            operation="test_report",
            intent=PRIVATE,
            body=PRIVATE,
            authorization_basis="none",
            now=NOW,
        )
        self.append(
            "message.outbound.intent",
            raw=root,
            message_id=json.loads(root)["message_id"],
            sender_participant_id=participant.participant_id,
            recipient_participant_id=PIDS[1],
        )
        self.store.lifecycle_root(root, now=NOW)
        row = self.items(self.report("newcomer"))[0]
        self.assertEqual(row["local_binding"]["generation"], 1)
        self.assertEqual(row["local_binding"]["session_relation"], "current_session")

    def test_canonical_corruption_fails_without_repair_or_disclosure(self) -> None:
        self.root()
        self.binding.journal_path.write_bytes(
            self.binding.journal_path.read_bytes() + PRIVATE.encode()
        )
        before = self.binding.journal_path.read_bytes()
        result = self.run_tool("state", "outstanding", "--participant", "requester")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(PRIVATE, result.stdout + result.stderr)
        self.assertEqual(self.binding.journal_path.read_bytes(), before)

    def test_wrong_project_snapshot_is_rejected(self) -> None:
        self.root()
        snapshot = self.store.snapshot()
        records = journal.replay_records(self.binding)
        # A same-length foreign tip must not be paired with the captured state.
        foreign = tuple({**record, "record_sha256": "f" * 64} for record in records)
        with self.assertRaises(CamUsageError) as error:
            outstanding.render(
                snapshot,
                foreign,
                project_name="other",
                participant="requester",
                as_of=NOW,
            )
        self.assertEqual(error.exception.code, "outstanding.snapshot_mismatch")
