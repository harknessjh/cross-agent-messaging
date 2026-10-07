# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Regress evidence-backed delayed rejection intake with synthetic peers only.

The audited positive path reuses R1's in-memory MCP boundary: no product runs,
and its test-only source/approval substitutions qualify no live installation.
Builders, validation, intent/outcome recording, lifecycle replay and recipient
ingest remain real. Seeded audit records below isolate missing-proof controls.
"""

from __future__ import annotations

import datetime as dt
import json
from contextlib import ExitStack, contextmanager
from unittest import mock

from tools import cam1, cam1_transport, cam1_transport_native
from tools.cam1lib import (
    inbound,
    journal,
    project,
    protocol,
    state,
    state_projection,
    transport_audit,
    validation,
)

if __package__:
    from . import test_cam1_reliability_reproductions as reliability
else:
    import test_cam1_reliability_reproductions as reliability


def timestamp(value: dt.datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


MISSING = object()


class LateRejectionEvidenceTests(reliability.ProjectBoundTransportTestCase):
    # Reuse the exact synthetic product boundary and audited send from R1,
    # without inheriting (and rerunning) its receipt-preservation test methods.
    synthetic_product = reliability.ClaudeReceiptPreservationTests.synthetic_product
    send = reliability.ClaudeReceiptPreservationTests.send

    def setUp(self) -> None:
        super().setUp()
        self.add_claude_participant()
        self.add_codex_participant()
        # Enrollment uses real time. Keep every synthetic observation later
        # than those bindings while advancing subsequent clocks without sleep.
        self.origin = dt.datetime.now(dt.UTC).replace(microsecond=0) + dt.timedelta(
            seconds=1
        )
        self.sent = self.origin + dt.timedelta(seconds=58)
        self.dispatched = self.origin + dt.timedelta(seconds=59)
        self.arrived = self.origin + dt.timedelta(seconds=61)
        self.clock = self.sent
        self.store = state.StateStore(self.binding)
        self.root = reliability.request(now=self.origin, expires_in=60)
        self.root_id = json.loads(self.root)["message_id"]
        self.store.lifecycle_root(self.root, now=self.origin)
        self.reply = cam1.build_ack(
            self.root,
            **reliability.CODEX_SENDER,
            status_value="rejected",
            now=self.sent,
        )
        self.reply_id = json.loads(self.reply)["message_id"]
        self.root_path = self.private_envelope("root.json", self.root)
        self.reply_path = self.private_envelope("reply.json", self.reply)
        self.capture_index = 0

    def new_exchange(self, *, root=None, status="rejected"):
        """Independent root for each seeded-proof control; no shared nonce."""
        self.root = (
            reliability.request(now=self.origin, expires_in=60)
            if root is None
            else root
        )
        self.root_id = json.loads(self.root)["message_id"]
        self.store.lifecycle_root(self.root, now=self.origin)
        self.reply = cam1.build_ack(
            self.root, **reliability.CODEX_SENDER, status_value=status, now=self.sent
        )
        self.reply_id = json.loads(self.reply)["message_id"]

    def seed_evidence(
        self,
        *,
        commit=True,
        intent=True,
        outcome="transport.accepted",
        marker=True,
        delivery_state=None,
        intent_raw=None,
        wrong_intent=False,
        wrong_message=False,
    ):
        """Minimal audit fixtures, not claims that a real product delivered."""
        intent_record = None
        if intent:
            intent_record = journal.append_record(
                self.binding,
                event_type="message.outbound.intent",
                exact_message=self.reply if intent_raw is None else intent_raw,
                attributes={"message_id": self.reply_id},
                now=self.dispatched,
            )
        if commit:
            self.store.lifecycle_reply(self.reply, now=self.dispatched)
        if outcome is not None:
            attributes = {
                "intent_record_id": (
                    "00000000-0000-4000-8000-000000000999"
                    if wrong_intent or intent_record is None
                    else intent_record["record_id"]
                ),
                "message_id": self.root_id if wrong_message else self.reply_id,
            }
            if marker is not MISSING and outcome == "transport.accepted":
                attributes["lifecycle_state_committed"] = marker
            if delivery_state is not None:
                attributes["delivery_state"] = delivery_state
            journal.append_record(
                self.binding,
                event_type=outcome,
                attributes=attributes,
                now=self.arrived,
            )

    @contextmanager
    def frozen_clock(self):
        normalize = validation._normalize_now
        with ExitStack() as stack:
            for module in (
                cam1_transport,
                cam1_transport_native,
                transport_audit,
                inbound,
            ):
                stack.enter_context(
                    mock.patch.object(
                        module,
                        "_utc_now",
                        side_effect=lambda: (self.clock, timestamp(self.clock)),
                    )
                )
            stack.enter_context(
                mock.patch.object(
                    state_projection,
                    "_current_utc_time",
                    side_effect=lambda: self.clock,
                )
            )
            stack.enter_context(
                mock.patch.object(
                    validation,
                    "_normalize_now",
                    side_effect=lambda value: normalize(
                        self.clock if value is None else value
                    ),
                )
            )
            yield

    def audited_rejection(self, *, during_send=None):
        case = self

        class Peer(reliability.SyntheticClient):
            async def call_tool(self, name, arguments):
                result = await super().call_tool(name, arguments)
                if name == "SendMessage":
                    # The journal lock has been released for product I/O.
                    case.clock = case.arrived
                    if during_send is not None:
                        during_send()
                return result

        peer = Peer(cleanup="none")
        self.peer = peer
        prepare = state.StateStore.prepare_lifecycle
        prepared_plans = []

        def prepare_at_dispatch(store, exact_message, **kwargs):
            # Advance only for the adapter's final root-correlated preparation,
            # distinct from the earlier initial preparation and wire sent_at.
            if kwargs.get("require_preserved_against"):
                self.clock = self.dispatched if prepared_plans else self.sent
                kwargs["now"] = self.clock
                plan = prepare(store, exact_message, **kwargs)
                prepared_plans.append(plan)
                return plan
            return prepare(store, exact_message, **kwargs)

        with (
            self.frozen_clock(),
            self.synthetic_product(peer),
            mock.patch.object(
                state.StateStore, "prepare_lifecycle", new=prepare_at_dispatch
            ),
        ):
            result = self.send()
        self.assertEqual(len(peer.sent), 1)
        self.assertEqual(peer.sent[0]["message"].encode(), self.reply)
        self.assertEqual(len(prepared_plans), 2)
        self.assertEqual(
            prepared_plans[0].attributes["observed_at"], timestamp(self.sent)
        )
        self.assertEqual(result["status"], "transport_accepted")
        reply_record = next(
            record
            for record in journal.replay_records(self.binding)
            if record["event_type"] == state.LIFECYCLE_REPLY_APPLIED
            and record["attributes"]["message_id"] == self.reply_id
        )
        self.assertEqual(
            reply_record["attributes"]["observed_at"],
            prepared_plans[-1].attributes["observed_at"],
        )
        self.assertEqual(
            reply_record["attributes"]["observed_at"], timestamp(self.dispatched)
        )
        self.assertNotEqual(
            reply_record["attributes"]["observed_at"], timestamp(self.sent)
        )
        self.assertGreaterEqual(
            result["journal"]["accepted_record"]["sequence"], reply_record["sequence"]
        )
        return result

    def ingest(self, raw=None, *, at=None, as_participant="local-worker"):
        self.capture_index += 1
        path = self.private_envelope(
            f"received-{self.capture_index}.json", self.reply if raw is None else raw
        )
        self.clock = self.arrived if at is None else at
        with self.frozen_clock():
            return inbound.ingest_message(
                self.binding,
                message_path=str(path),
                as_participant=as_participant,
                renewal_of=None,
            )

    def state_facts(self):
        snapshot = self.store.snapshot()
        return (
            snapshot.lifecycle.as_dict(),
            snapshot.lifecycle.processed_messages,
            snapshot._message_bytes,
            snapshot._nonce_owners,
            snapshot._nonce_echoes,
        )

    def assert_rejected(
        self,
        *,
        raw=None,
        at=None,
        problem="correlation.late_rejection_nonce",
        code="validation.failed",
    ):
        before = self.state_facts()
        exit_code, result = self.ingest(raw, at=at)
        self.assertEqual(exit_code, 2, result)
        self.assertEqual(result["error"]["code"], code, result)
        if problem is not None:
            self.assertIn(problem, result["error"]["problem_codes"])
        self.assertEqual(self.state_facts(), before)
        self.assertEqual(
            journal.replay_records(self.binding)[-1]["event_type"],
            "message.inbound.rejected",
        )
        return result

    def test_audited_timely_rejection_first_intake_then_repeat(self) -> None:
        self.audited_rejection()
        before = self.state_facts()
        records_before = journal.replay_records(self.binding)
        with self.assertRaises(cam1.CamValidationError) as context:
            cam1.validate_exact_bytes(
                self.reply, against_raw=self.root, now=self.arrived
            )
        self.assertEqual(
            [problem.code for problem in context.exception.problems],
            ["correlation.late_rejection_nonce"],
        )
        with project.project_transaction(self.binding) as transaction:
            plan = self.store.prepare_inbound_lifecycle(
                self.reply, now=self.arrived, transaction=transaction
            )
        self.assertTrue(plan.duplicate)
        self.assertEqual(plan.recorded_at, self.arrived)
        self.assertEqual(plan.attributes["observed_at"], timestamp(self.arrived))
        self.assertEqual(plan.freshness_deadline, json.loads(self.reply)["expires_at"])
        for status_value, duplicate in (("validated", False), ("duplicate", True)):
            code, result = self.ingest()
            self.assertEqual(code, 0, result)
            self.assertEqual(result["status"], status_value)
            self.assertIs(result["duplicate"], duplicate)
            self.assertEqual(result["lifecycle"]["state"], "rejected")
            self.assertFalse(result["action_authorized"])
            self.assertFalse(result["authorization_evaluated"])
            self.assertEqual(self.state_facts(), before)
        records = journal.replay_records(self.binding)
        self.assertEqual(
            [record["event_type"] for record in records[len(records_before) :]],
            [
                "message.inbound.observed",
                "message.inbound.validated",
                "message.inbound.observed",
                "message.inbound.duplicate",
            ],
        )

    def test_missing_or_mismatched_proof_never_qualifies(self) -> None:
        cases = {
            "no evidence": dict(commit=False, intent=False, outcome=None),
            "committed only": dict(intent=False, outcome=None),
            "orphaned intent": dict(commit=False, outcome=None),
            "accepted but no committed reply": dict(commit=False),
            "committed but no outcome": dict(outcome=None),
            "unknown": dict(outcome="transport.not_accepted", delivery_state="unknown"),
            "not attempted": dict(
                outcome="transport.not_accepted", delivery_state="not_attempted"
            ),
            "false marker": dict(marker=False),
            "missing marker": dict(marker=MISSING),
            "string marker": dict(marker="true"),
            "wrong intent link": dict(wrong_intent=True),
            "wrong message link": dict(wrong_message=True),
            "intent holds different exact bytes": dict(intent_raw=b"{}"),
        }
        for label, kwargs in cases.items():
            with self.subTest(label=label):
                self.new_exchange()
                self.seed_evidence(**kwargs)
                self.assert_rejected()

    def test_changed_capture_is_not_normalized(self) -> None:
        self.seed_evidence()
        changed = json.loads(self.reply)
        changed["intent"] = "Changed during capture"
        for raw in (self.reply + b"\n", cam1.serialize_envelope(changed)):
            with self.subTest(raw=raw[-30:]):
                # Late correlation keeps its existing precedence over conflict.
                self.assert_rejected(raw=raw)
        # Before root expiry, the same conflict still reaches the ordinary code.
        self.assert_rejected(
            raw=self.reply + b"\n",
            at=self.dispatched,
            problem=None,
            code="state.message_conflict",
        )

    def test_bad_body_and_schema_keep_precedence(self) -> None:
        self.seed_evidence()
        changed = json.loads(self.reply)
        changed["body"] += " capture error"
        self.assert_rejected(
            raw=cam1.serialize_envelope(changed), problem="semantic.body_hash"
        )
        del changed["protocol"]
        self.assert_rejected(
            raw=cam1.serialize_envelope(changed), problem="schema.required"
        )

    def test_other_or_multiple_validation_problems_do_not_enter_fallback(self) -> None:
        for codes in (
            ("correlation.sender",),
            ("correlation.late_rejection_nonce", "correlation.receipt"),
        ):
            with self.subTest(codes=codes):
                error = cam1.CamValidationError(
                    [
                        protocol.Problem(code, "/", "synthetic branch guard")
                        for code in codes
                    ]
                )
                with (
                    project.project_transaction(self.binding) as transaction,
                    mock.patch.object(
                        self.store, "prepare_lifecycle", side_effect=error
                    ),
                    mock.patch.object(
                        self.store, "_prepare_accepted_outbound_reply"
                    ) as fallback,
                ):
                    with self.assertRaises(cam1.CamValidationError) as context:
                        self.store.prepare_inbound_lifecycle(
                            self.reply, now=self.arrived, transaction=transaction
                        )
                    self.assertIs(context.exception, error)
                    fallback.assert_not_called()

    def test_wrong_nonce_cannot_use_committed_proof(self) -> None:
        self.seed_evidence()
        changed = json.loads(self.reply)
        changed["nonce"] = "A" * 32
        self.assert_rejected(raw=cam1.serialize_envelope(changed))

    def test_wrong_root_endpoint_and_callback_keep_their_guards(self) -> None:
        self.seed_evidence()
        changes = (
            ("root", "validation.failed", "semantic.receipt_correlation"),
            ("recipient", "roster.recipient_mismatch", None),
            ("callback", "validation.failed", "semantic.callback_identity"),
        )
        for label, code, problem in changes:
            with self.subTest(label=label):
                changed = json.loads(self.reply)
                if label == "root":
                    changed["receipt"]["for_message_id"] = self.reply_id
                elif label == "recipient":
                    changed["recipient"]["agent_name"] = "not-the-local-participant"
                else:
                    changed["reply_to"]["address"] = reliability.CLAUDE_SESSION
                self.assert_rejected(
                    raw=cam1.serialize_envelope(changed), code=code, problem=problem
                )

    def test_ordinary_preparation_does_not_enter_evidence_fallback(self) -> None:
        with (
            project.project_transaction(self.binding) as transaction,
            mock.patch.object(
                self.store, "_prepare_accepted_outbound_reply"
            ) as fallback,
        ):
            plan = self.store.prepare_inbound_lifecycle(
                self.reply, now=self.dispatched, transaction=transaction
            )
        self.assertFalse(plan.duplicate)
        self.assertEqual(plan.preview.state.value, "rejected")
        fallback.assert_not_called()

    def test_non_request_roots_are_out_of_scope(self) -> None:
        hello = cam1.build_hello(
            **reliability.CLAUDE_SENDER,
            recipient_vendor="codex",
            recipient_name="example-coordinator",
            recipient_session=reliability.CODEX_THREAD,
            expires_in=60,
            now=self.origin,
        )
        cancel = cam1.build_cancel(
            self.root,
            **reliability.CLAUDE_SENDER,
            authority="Synthetic fixture operator",
            authorization_reference="Synthetic cancellation",
            authorization_verified_at=timestamp(self.origin),
            authorization_expires_at=timestamp(self.origin + dt.timedelta(seconds=60)),
            expires_in=60,
            now=self.origin,
        )
        for root in (hello, cancel):
            with self.subTest(root_type=json.loads(root)["type"]):
                self.new_exchange(root=root)
                self.seed_evidence()
                self.assert_rejected()

    def test_reply_expiry_is_checked_at_intake(self) -> None:
        self.seed_evidence()
        self.assert_rejected(
            at=self.sent + dt.timedelta(seconds=600), problem="semantic.expired"
        )

    def test_reply_expiry_is_rechecked_at_refresh_and_commit(self) -> None:
        self.seed_evidence()
        expired = self.sent + dt.timedelta(seconds=600)
        for phase in ("refresh", "commit"):
            with self.subTest(phase=phase):
                before = self.state_facts()
                if phase == "refresh":
                    original = inbound._refresh_inbound_plan

                    def expire_then_refresh(*args, _original=original, **kwargs):
                        self.clock = expired
                        return _original(*args, **kwargs)

                    patch = mock.patch.object(
                        inbound,
                        "_refresh_inbound_plan",
                        side_effect=expire_then_refresh,
                    )
                else:
                    original = inbound._commit_inbound_plan

                    def expire_then_commit(*args, _original=original, **kwargs):
                        self.clock = expired
                        return _original(*args, **kwargs)

                    patch = mock.patch.object(
                        inbound, "_commit_inbound_plan", side_effect=expire_then_commit
                    )
                with patch:
                    code, result = self.ingest()
                self.assertEqual(code, 2, result)
                self.assertEqual(
                    result["error"]["code"],
                    "validation.failed"
                    if phase == "refresh"
                    else "state.observation_expired",
                )
                self.assertEqual(self.state_facts(), before)

    def test_exact_expiry_boundary_is_not_timely(self) -> None:
        boundary = self.origin + dt.timedelta(seconds=60)
        cam1.validate_exact_bytes(
            self.reply,
            against_raw=self.root,
            now=boundary - dt.timedelta(microseconds=1),
        )
        for label, observed in (
            ("at", boundary),
            ("after", boundary + dt.timedelta(microseconds=1)),
        ):
            with self.subTest(boundary=label):
                with self.assertRaises(cam1.CamValidationError) as context:
                    self.store.lifecycle_reply(self.reply, now=observed)
                self.assertEqual(
                    [p.code for p in context.exception.problems],
                    ["correlation.late_rejection_nonce"],
                )
        self.assertNotIn(self.reply_id, self.store.snapshot()._message_bytes)

    def test_replay_invalid_at_expiry_event_is_not_repaired(self) -> None:
        # Deliberately corrupt synthetic history: no public lifecycle method can
        # commit a nonce echo observed at expiry. R2 must not mask replay failure.
        journal.append_record(
            self.binding,
            event_type=state.LIFECYCLE_REPLY_APPLIED,
            exact_message=self.reply,
            attributes={
                "message_id": self.reply_id,
                "root_message_id": self.root_id,
                "message_type": "ack",
                "observed_at": timestamp(self.origin + dt.timedelta(seconds=60)),
            },
            now=self.arrived,
        )
        self.seed_evidence(commit=False)
        with project.project_transaction(self.binding) as transaction:
            with self.assertRaises(state_projection.StateError) as context:
                self.store.prepare_inbound_lifecycle(
                    self.reply, now=self.arrived, transaction=transaction
                )
        self.assertEqual(context.exception.code, "state.event_invalid")

    def test_held_request_and_last_fractional_moment_still_qualify(self) -> None:
        held = cam1.build_ack(
            self.root,
            **reliability.CODEX_SENDER,
            status_value="needs_human_confirmation",
            now=self.origin + dt.timedelta(seconds=1),
        )
        self.store.lifecycle_reply(held, now=self.origin + dt.timedelta(seconds=2))
        self.dispatched = self.origin + dt.timedelta(seconds=60, microseconds=-1)
        self.seed_evidence()
        before = self.state_facts()
        code, result = self.ingest()
        self.assertEqual(code, 0, result)
        self.assertEqual(result["lifecycle"]["state"], "rejected")
        self.assertEqual(self.state_facts(), before)

    def test_existing_accepted_ack_fallback_is_unchanged(self) -> None:
        self.new_exchange(status="accepted")
        self.seed_evidence()
        before = self.state_facts()
        code, result = self.ingest()
        self.assertEqual(code, 0, result)
        self.assertFalse(result["duplicate"])
        self.assertEqual(result["lifecycle"]["state"], "accepted")
        self.assertEqual(self.state_facts(), before)

    def test_delayed_acceptance_without_evidence_cannot_revive(self) -> None:
        self.new_exchange(status="accepted")
        self.assert_rejected(problem=None, code="lifecycle.root_expired_before_reply")

    def test_genuine_late_rejection_preserves_renewed_successor(self) -> None:
        self.store.lifecycle_expired(self.root_id, now=self.arrived)
        renewed = cam1.renew_request(
            self.root,
            authorization_basis="none",
            confirm_no_known_pending=True,
            now=self.arrived,
        )
        successor = self.store.lifecycle_root(
            renewed, renewal_of=self.root_id, now=self.arrived
        )
        late = cam1.build_late_rejection(
            self.root, **reliability.CODEX_SENDER, now=self.arrived
        )
        self.assertIsNone(json.loads(late)["nonce"])
        code, result = self.ingest(late)
        self.assertEqual(code, 0, result)
        self.assertEqual(result["lifecycle"]["state"], "late_rejected")
        self.assertEqual(
            self.store.snapshot().lifecycle.entries[successor.root_message_id],
            successor,
        )

    def test_expiry_and_renewal_during_send_leave_state_incomplete(self) -> None:
        successors = []

        def expire_and_renew():
            self.store.lifecycle_expired(self.root_id, now=self.arrived)
            renewed = cam1.renew_request(
                self.root,
                authorization_basis="none",
                confirm_no_known_pending=True,
                now=self.arrived,
            )
            successors.append(
                self.store.lifecycle_root(
                    renewed, renewal_of=self.root_id, now=self.arrived
                )
            )

        with self.assertRaises(cam1_transport_native.TransportError) as context:
            self.audited_rejection(during_send=expire_and_renew)
        self.assertEqual(context.exception.code, "transport.accepted_state_incomplete")
        self.assertEqual(len(self.peer.sent), 1)
        records = journal.replay_records(self.binding)
        accepted = [r for r in records if r["event_type"] == "transport.accepted"]
        self.assertEqual(len(accepted), 1)
        self.assertIs(accepted[0]["attributes"]["lifecycle_state_committed"], False)
        self.assertNotIn(self.reply_id, self.store.snapshot()._message_bytes)
        self.assert_rejected()
        self.assertEqual(
            self.store.snapshot().lifecycle.entries[successors[0].root_message_id],
            successors[0],
        )

    def test_prior_rejection_audit_replays_unchanged(self) -> None:
        self.seed_evidence()
        # Reproduce the old reader's first-intake verdict without its new catch.
        with mock.patch.object(
            state.StateStore,
            "prepare_inbound_lifecycle",
            state.StateStore.prepare_lifecycle,
        ):
            self.assert_rejected()
        prior = journal.replay_records(self.binding)
        before = self.state_facts()
        code, result = self.ingest()
        self.assertEqual(code, 0, result)
        self.assertFalse(result["duplicate"])
        self.assertEqual(journal.replay_records(self.binding)[: len(prior)], prior)
        self.assertEqual(self.state_facts(), before)

    def test_cli_first_intake_reports_validated_not_duplicate(self) -> None:
        # Real subprocess CLI and wall clock, with a roomy fresh reply lifetime.
        self.origin = dt.datetime.now(dt.UTC).replace(microsecond=0) - dt.timedelta(
            seconds=120
        )
        self.sent = self.origin + dt.timedelta(seconds=58)
        self.dispatched = self.origin + dt.timedelta(seconds=59)
        self.arrived = self.origin + dt.timedelta(seconds=61)
        self.new_exchange()
        self.seed_evidence()
        path = self.private_envelope("cli-received.json", self.reply)
        result = self.run_project(
            "message",
            "ingest",
            "--message",
            str(path),
            "--as-participant",
            "local-worker",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "validated")
        self.assertFalse(payload["duplicate"])
        self.assertFalse(payload["action_authorized"])
