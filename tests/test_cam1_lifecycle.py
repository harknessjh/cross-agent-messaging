# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import unittest
import uuid

from tools.cam1lib.lifecycle import LifecycleProjection, LifecycleState
from tools.cam1lib.protocol import CamUsageError

PROJECT_ID = "00000000-0000-4000-8000-000000000001"
SENT = dt.datetime(2026, 8, 27, 18, 0, tzinfo=dt.UTC)


def identifier(value: int) -> str:
    return str(uuid.UUID(int=(1 << 78) | value))


def timestamp(value: dt.datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def root(
    message_type: str,
    value: int = 10,
    *,
    sent_at: dt.datetime = SENT,
    idempotency_key: str | None = None,
) -> dict[str, object]:
    message_id = identifier(value)
    body = "Perform one stable semantic operation."
    return {
        "protocol": "CAM/1",
        "message_id": message_id,
        "type": message_type,
        "sent_at": timestamp(sent_at),
        "expires_at": timestamp(sent_at + dt.timedelta(minutes=10)),
        "claimed_sender": {
            "vendor": "codex",
            "agent_name": "coordinator",
            "session_id": identifier(1),
            "host_id": None,
        },
        "recipient": {
            "vendor": "claude-code",
            "agent_name": "worker",
            "session_id": identifier(2),
        },
        "reply_to": {"transport": "codex_queue", "address": identifier(1)},
        "in_reply_to": None,
        "receipt": None,
        "nonce": "stable-challenge",
        "intent": "Perform one stable semantic operation",
        "action": {
            "risk_class": "informational",
            "operation": "stable_operation",
            "scope": {
                "repositories": ["/example/a", "/example/b"],
                "paths": [],
                "hosts": [],
                "external_recipients": [],
            },
            "idempotency_key": idempotency_key or message_id,
        },
        "authorization": {
            "basis": "none",
            "authority": None,
            "reference": None,
            "verified_at": None,
            "expires_at": None,
        },
        "constraints": {
            "no_repository_changes": True,
            "no_external_side_effects": True,
            "no_secrets": True,
        },
        "body": body,
        "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
        "evidence": [],
    }


def reply(
    root_envelope: dict[str, object],
    message_type: str,
    status: str | None,
    value: int,
    *,
    sent_at: dt.datetime | None = None,
) -> dict[str, object]:
    root_id = root_envelope["message_id"]
    return {
        "protocol": "CAM/1",
        "message_id": identifier(value),
        "type": message_type,
        "sent_at": timestamp(sent_at or SENT + dt.timedelta(minutes=1)),
        "in_reply_to": root_id,
        "receipt": (
            None
            if status is None
            else {"status": status, "for_message_id": root_id, "detail": None}
        ),
    }


def register(
    projection: LifecycleProjection,
    envelope: dict[str, object],
    *,
    observed_at: dt.datetime | None = None,
    renewal_of: str | None = None,
):
    return projection.register_root(
        envelope,
        observed_at=timestamp(observed_at or SENT),
        renewal_of=renewal_of,
    )


class LifecycleProjectionTests(unittest.TestCase):
    def test_request_requires_application_receipt_before_result(self) -> None:
        request = root("request")
        projection = LifecycleProjection(PROJECT_ID)
        register(projection, request)

        with self.assertRaises(CamUsageError) as context:
            projection.apply_reply(reply(request, "result", "completed", 11))
        self.assertEqual(context.exception.code, "lifecycle.transition")

        received = projection.apply_reply(reply(request, "ack", "received", 12))
        self.assertEqual(received.state, LifecycleState.RECEIVED)
        with self.assertRaises(CamUsageError) as still_unaccepted:
            projection.apply_reply(reply(request, "result", "completed", 13))
        self.assertEqual(still_unaccepted.exception.code, "lifecycle.transition")
        accepted = projection.apply_reply(reply(request, "status", "accepted", 14))
        self.assertEqual(accepted.state, LifecycleState.ACCEPTED)
        completed = projection.apply_reply(reply(request, "result", "completed", 15))
        self.assertEqual(completed.state, LifecycleState.COMPLETED)
        self.assertTrue(completed.terminal)

    def test_request_cannot_regress_or_advance_after_terminal_state(self) -> None:
        request = root("request")
        projection = LifecycleProjection(PROJECT_ID)
        register(projection, request)
        projection.apply_reply(reply(request, "ack", "accepted", 14))
        projection.apply_reply(reply(request, "status", "started", 15))

        with self.assertRaises(CamUsageError) as context:
            projection.apply_reply(reply(request, "status", "accepted", 16))
        self.assertEqual(context.exception.code, "lifecycle.transition")

        projection.apply_reply(reply(request, "result", "completed", 17))
        with self.assertRaises(CamUsageError) as terminal:
            projection.apply_reply(reply(request, "result", "completed", 18))
        self.assertEqual(terminal.exception.code, "lifecycle.terminal")

    def test_human_confirmation_hold_cannot_skip_explicit_acceptance(self) -> None:
        request = root("request", 62)
        projection = LifecycleProjection(PROJECT_ID)
        register(projection, request)
        held = projection.apply_reply(
            reply(request, "ack", "needs_human_confirmation", 63)
        )
        self.assertEqual(held.state, LifecycleState.HELD)

        with self.assertRaises(CamUsageError) as context:
            projection.apply_reply(reply(request, "status", "started", 64))
        self.assertEqual(context.exception.code, "lifecycle.transition")
        accepted = projection.apply_reply(reply(request, "ack", "accepted", 65))
        self.assertEqual(accepted.state, LifecycleState.ACCEPTED)

    def test_duplicate_identical_reply_is_idempotent(self) -> None:
        request = root("request")
        candidate = reply(request, "ack", "received", 19)
        projection = LifecycleProjection(PROJECT_ID)
        register(projection, request)
        first = projection.apply_reply(candidate)
        second = projection.apply_reply(copy.deepcopy(candidate))
        self.assertEqual(first, second)
        self.assertEqual(len(second.reply_message_ids), 1)

    def test_expired_root_retransmission_is_rejected_after_completion(self) -> None:
        request = root("request")
        projection = LifecycleProjection(PROJECT_ID)
        register(projection, request)
        projection.apply_reply(reply(request, "ack", "accepted", 96))
        completed = projection.apply_reply(reply(request, "result", "completed", 97))

        with self.assertRaises(CamUsageError) as context:
            register(
                projection,
                copy.deepcopy(request),
                observed_at=SENT + dt.timedelta(minutes=11),
            )

        self.assertEqual(context.exception.code, "lifecycle.duplicate_expired")
        self.assertEqual(
            projection.entries[request["message_id"]].state,
            LifecycleState.COMPLETED,
        )
        self.assertEqual(completed.state, LifecycleState.COMPLETED)

    def test_reused_message_id_with_different_content_is_rejected(self) -> None:
        request = root("request")
        candidate = reply(request, "ack", "received", 20)
        projection = LifecycleProjection(PROJECT_ID)
        register(projection, request)
        projection.apply_reply(candidate)
        candidate["type"] = "status"

        with self.assertRaises(CamUsageError) as context:
            projection.apply_reply(candidate)
        self.assertEqual(context.exception.code, "lifecycle.message_conflict")

    def test_semantic_idempotency_key_cannot_start_two_roots(self) -> None:
        first = root("request", 21)
        second = root("request", 22)
        second["action"] = first["action"]
        projection = LifecycleProjection(PROJECT_ID)
        register(projection, first)

        with self.assertRaises(CamUsageError) as context:
            register(projection, second)
        self.assertEqual(context.exception.code, "lifecycle.idempotency_conflict")

    def test_expired_operation_can_be_explicitly_renewed(self) -> None:
        first = root("request", 34)
        second = root(
            "request",
            35,
            sent_at=SENT + dt.timedelta(minutes=12),
        )
        second["action"] = first["action"]
        projection = LifecycleProjection(PROJECT_ID)
        prior = register(projection, first)
        projection.mark_expired_unconfirmed(
            prior.root_message_id,
            observed_at=timestamp(SENT + dt.timedelta(minutes=11)),
        )

        renewed = register(
            projection,
            second,
            renewal_of=prior.root_message_id,
            observed_at=SENT + dt.timedelta(minutes=12),
        )
        self.assertEqual(renewed.renewal_of, prior.root_message_id)
        self.assertEqual(renewed.state, LifecycleState.PENDING)

    def test_renewal_is_rejected_while_prior_operation_is_live(self) -> None:
        first = root("request", 36)
        second = root("request", 37)
        second["action"] = first["action"]
        projection = LifecycleProjection(PROJECT_ID)
        prior = register(projection, first)

        with self.assertRaises(CamUsageError) as context:
            register(projection, second, renewal_of=prior.root_message_id)
        self.assertEqual(context.exception.code, "lifecycle.renewal_state")

    def test_renewal_is_blocked_while_predecessor_cancel_is_unresolved(self) -> None:
        key = identifier(200)

        for acknowledge_cancel in (False, True):
            with self.subTest(acknowledge_cancel=acknowledge_cancel):
                request = root("request", 201, idempotency_key=key)
                cancel = root(
                    "cancel",
                    202,
                    sent_at=SENT + dt.timedelta(minutes=8),
                )
                cancel["in_reply_to"] = request["message_id"]
                renewal = root(
                    "request",
                    203,
                    sent_at=SENT + dt.timedelta(minutes=12),
                    idempotency_key=key,
                )
                projection = LifecycleProjection(PROJECT_ID)
                first = register(projection, request)
                cancel_entry = register(
                    projection,
                    cancel,
                    observed_at=SENT + dt.timedelta(minutes=8),
                )
                if acknowledge_cancel:
                    projection.apply_reply(
                        reply(
                            cancel,
                            "ack",
                            "received",
                            204,
                            sent_at=SENT + dt.timedelta(minutes=9),
                        )
                    )
                    self.assertEqual(
                        projection.entries[cancel_entry.root_message_id].state,
                        LifecycleState.RECEIVED,
                    )

                with self.assertRaises(CamUsageError) as context:
                    register(
                        projection,
                        renewal,
                        observed_at=SENT + dt.timedelta(minutes=12),
                        renewal_of=first.root_message_id,
                    )

                self.assertEqual(
                    context.exception.code,
                    "lifecycle.renewal_cancel_unresolved",
                )
                self.assertEqual(
                    projection.entries[first.root_message_id].state,
                    LifecycleState.PENDING,
                )

    def test_rejected_cancel_does_not_block_explicit_renewal(self) -> None:
        key = identifier(205)
        request = root("request", 206, idempotency_key=key)
        cancel = root(
            "cancel",
            207,
            sent_at=SENT + dt.timedelta(minutes=8),
        )
        cancel["in_reply_to"] = request["message_id"]
        renewal = root(
            "request",
            208,
            sent_at=SENT + dt.timedelta(minutes=12),
            idempotency_key=key,
        )
        projection = LifecycleProjection(PROJECT_ID)
        first = register(projection, request)
        register(
            projection,
            cancel,
            observed_at=SENT + dt.timedelta(minutes=8),
        )
        projection.apply_reply(
            reply(
                cancel,
                "ack",
                "rejected",
                209,
                sent_at=SENT + dt.timedelta(minutes=9),
            )
        )

        renewed = register(
            projection,
            renewal,
            observed_at=SENT + dt.timedelta(minutes=12),
            renewal_of=first.root_message_id,
        )

        self.assertEqual(renewed.renewal_of, first.root_message_id)
        self.assertEqual(
            projection.entries[first.root_message_id].state,
            LifecycleState.EXPIRED_UNCONFIRMED,
        )

    def test_expired_pending_cancel_is_aged_before_explicit_renewal(self) -> None:
        key = identifier(210)
        request = root("request", 211, idempotency_key=key)
        cancel = root(
            "cancel",
            212,
            sent_at=SENT + dt.timedelta(minutes=1),
        )
        cancel["in_reply_to"] = request["message_id"]
        renewal = root(
            "request",
            213,
            sent_at=SENT + dt.timedelta(minutes=12),
            idempotency_key=key,
        )
        projection = LifecycleProjection(PROJECT_ID)
        first = register(projection, request)
        cancel_entry = register(
            projection,
            cancel,
            observed_at=SENT + dt.timedelta(minutes=1),
        )

        renewed = register(
            projection,
            renewal,
            observed_at=SENT + dt.timedelta(minutes=12),
            renewal_of=first.root_message_id,
        )

        self.assertEqual(renewed.renewal_of, first.root_message_id)
        self.assertEqual(
            projection.entries[cancel_entry.root_message_id].state,
            LifecycleState.EXPIRED_UNCONFIRMED,
        )

    def test_hello_handling_does_not_use_enrollment_language(self) -> None:
        hello = root("hello", 23)
        projection = LifecycleProjection(PROJECT_ID)
        register(projection, hello)
        held = projection.apply_reply(
            reply(hello, "ack", "needs_human_confirmation", 24)
        )
        self.assertEqual(held.state, LifecycleState.HELD)
        handled = projection.apply_reply(reply(hello, "ack", "received", 25))
        self.assertEqual(handled.state, LifecycleState.HANDLED)

    def test_challenge_verify_completes_only_challenge_leg(self) -> None:
        challenge = root("challenge", 26)
        projection = LifecycleProjection(PROJECT_ID)
        register(projection, challenge)
        correlated = projection.apply_reply(reply(challenge, "verify", None, 27))
        self.assertEqual(correlated.state, LifecycleState.CORRELATED)

    def test_expired_unconfirmed_state_cannot_advance(self) -> None:
        request = root("request", 28)
        projection = LifecycleProjection(PROJECT_ID)
        register(projection, request)
        expired = projection.mark_expired_unconfirmed(
            request["message_id"],
            observed_at=timestamp(SENT + dt.timedelta(minutes=11)),
        )
        self.assertEqual(expired.state, LifecycleState.EXPIRED_UNCONFIRMED)
        self.assertFalse(expired.terminal)

        with self.assertRaises(CamUsageError) as context:
            projection.apply_reply(reply(request, "ack", "received", 29))
        self.assertEqual(context.exception.code, "lifecycle.expired")

    def test_late_observation_derives_expired_unconfirmed_state(self) -> None:
        request = root("request", 69)
        projection = LifecycleProjection(PROJECT_ID)
        expired = register(
            projection,
            request,
            observed_at=SENT + dt.timedelta(minutes=11),
        )
        self.assertEqual(expired.state, LifecycleState.EXPIRED_UNCONFIRMED)

        with self.assertRaises(CamUsageError) as context:
            projection.apply_reply(
                reply(
                    request,
                    "ack",
                    "received",
                    70,
                    sent_at=SENT + dt.timedelta(minutes=11),
                )
            )
        self.assertEqual(
            context.exception.code,
            "lifecycle.root_expired_before_reply",
        )

    def test_cancel_has_its_own_lifecycle_root(self) -> None:
        request = root("request", 29)
        cancel = root("cancel", 30)
        cancel["in_reply_to"] = request["message_id"]
        projection = LifecycleProjection(PROJECT_ID)
        register(projection, request)
        cancel_entry = register(projection, cancel)
        self.assertEqual(cancel_entry.cancels_root_id, request["message_id"])
        received = projection.apply_reply(reply(cancel, "ack", "received", 31))
        self.assertEqual(received.state, LifecycleState.RECEIVED)
        cancelled = projection.apply_reply(reply(cancel, "status", "accepted", 32))
        self.assertEqual(cancelled.state, LifecycleState.CANCELLED)
        original = projection.entries[request["message_id"]]
        self.assertEqual(original.state, LifecycleState.CANCELLED)
        self.assertTrue(original.terminal)
        self.assertEqual(original.cancelled_by_root_id, cancel["message_id"])

    def test_received_cancel_requires_nonce_free_terminal_update(self) -> None:
        request = root("request", 73)
        cancel = root("cancel", 74)
        cancel["in_reply_to"] = request["message_id"]
        projection = LifecycleProjection(PROJECT_ID)
        register(projection, request)
        register(projection, cancel)
        projection.apply_reply(reply(cancel, "ack", "received", 75))

        with self.assertRaises(CamUsageError) as second_ack:
            projection.apply_reply(reply(cancel, "ack", "accepted", 76))
        self.assertEqual(second_ack.exception.code, "lifecycle.transition")

        cancelled = projection.apply_reply(reply(cancel, "status", "accepted", 77))
        self.assertEqual(cancelled.state, LifecycleState.CANCELLED)

    def test_expired_cancel_does_not_block_fresh_cancel(self) -> None:
        request = root("request", 78)
        first_cancel = root("cancel", 79, sent_at=SENT + dt.timedelta(minutes=1))
        first_cancel["in_reply_to"] = request["message_id"]
        second_cancel = root("cancel", 80, sent_at=SENT + dt.timedelta(minutes=13))
        second_cancel["in_reply_to"] = request["message_id"]
        projection = LifecycleProjection(PROJECT_ID)
        register(projection, request)
        projection.apply_reply(reply(request, "ack", "accepted", 81))
        first = register(
            projection,
            first_cancel,
            observed_at=SENT + dt.timedelta(minutes=1),
        )
        projection.mark_expired_unconfirmed(
            first.root_message_id,
            observed_at=timestamp(SENT + dt.timedelta(minutes=12)),
        )

        second = register(
            projection,
            second_cancel,
            observed_at=SENT + dt.timedelta(minutes=13),
        )

        self.assertEqual(second.cancels_root_id, request["message_id"])

    def test_cancel_after_expiry_requires_prior_handling_evidence(self) -> None:
        request = root("request", 66)
        late_cancel = root(
            "cancel",
            67,
            sent_at=SENT + dt.timedelta(minutes=11),
        )
        late_cancel["in_reply_to"] = request["message_id"]
        pending = LifecycleProjection(PROJECT_ID)
        register(pending, request)
        with self.assertRaises(CamUsageError) as expired:
            register(
                pending,
                late_cancel,
                observed_at=SENT + dt.timedelta(minutes=11),
            )
        self.assertEqual(expired.exception.code, "lifecycle.cancel_target_expired")

        claimed_early = root(
            "cancel",
            69,
            sent_at=SENT + dt.timedelta(minutes=9),
        )
        claimed_early["in_reply_to"] = request["message_id"]
        with self.assertRaises(CamUsageError) as observed_late:
            register(
                pending,
                claimed_early,
                observed_at=SENT + dt.timedelta(minutes=11),
            )
        self.assertEqual(
            observed_late.exception.code,
            "lifecycle.cancel_target_expired",
        )

        acknowledged = LifecycleProjection(PROJECT_ID)
        register(acknowledged, request)
        acknowledged.apply_reply(reply(request, "ack", "received", 68))
        cancel_entry = register(
            acknowledged,
            late_cancel,
            observed_at=SENT + dt.timedelta(minutes=11),
        )
        self.assertEqual(cancel_entry.cancels_root_id, request["message_id"])

    def test_multiple_renewals_follow_one_unbranched_semantic_chain(self) -> None:
        key = identifier(40)
        first = root("request", 41, idempotency_key=key)
        second = root(
            "request",
            42,
            sent_at=SENT + dt.timedelta(minutes=12),
            idempotency_key=key,
        )
        third = root(
            "request",
            43,
            sent_at=SENT + dt.timedelta(minutes=24),
            idempotency_key=key,
        )
        projection = LifecycleProjection(PROJECT_ID)
        first_entry = register(projection, first)
        projection.mark_expired_unconfirmed(
            first_entry.root_message_id,
            observed_at=timestamp(SENT + dt.timedelta(minutes=11)),
        )
        second_entry = register(
            projection,
            second,
            observed_at=SENT + dt.timedelta(minutes=12),
            renewal_of=first_entry.root_message_id,
        )
        projection.mark_expired_unconfirmed(
            second_entry.root_message_id,
            observed_at=timestamp(SENT + dt.timedelta(minutes=23)),
        )
        third_entry = register(
            projection,
            third,
            observed_at=SENT + dt.timedelta(minutes=24),
            renewal_of=second_entry.root_message_id,
        )

        self.assertEqual(third_entry.renewal_of, second_entry.root_message_id)
        branch = root(
            "request",
            44,
            sent_at=SENT + dt.timedelta(minutes=25),
            idempotency_key=key,
        )
        with self.assertRaises(CamUsageError) as context:
            register(
                projection,
                branch,
                observed_at=SENT + dt.timedelta(minutes=25),
                renewal_of=first_entry.root_message_id,
            )
        self.assertEqual(context.exception.code, "lifecycle.renewal_superseded")

    def test_renewal_rejects_semantic_change_but_allows_refreshed_authorization(
        self,
    ) -> None:
        key = identifier(45)
        first = root("request", 46, idempotency_key=key)
        projection = LifecycleProjection(PROJECT_ID)
        first_entry = register(projection, first)
        projection.mark_expired_unconfirmed(
            first_entry.root_message_id,
            observed_at=timestamp(SENT + dt.timedelta(minutes=11)),
        )
        renewal_template = root(
            "request",
            47,
            sent_at=SENT + dt.timedelta(minutes=12),
            idempotency_key=key,
        )
        semantic_changes = {
            "body": lambda value: value.update(
                {
                    "body": "Perform a different operation.",
                    "body_sha256": hashlib.sha256(
                        b"Perform a different operation."
                    ).hexdigest(),
                }
            ),
            "recipient": lambda value: value["recipient"].update(
                {"session_id": identifier(90)}
            ),
            "constraints": lambda value: value["constraints"].update(
                {"no_repository_changes": False}
            ),
            "evidence": lambda value: value["evidence"].append(
                {"kind": "other", "reference": "changed", "sha256": None}
            ),
        }
        for name, mutate in semantic_changes.items():
            with self.subTest(name=name):
                changed = copy.deepcopy(renewal_template)
                mutate(changed)
                with self.assertRaises(CamUsageError) as context:
                    register(
                        projection,
                        changed,
                        observed_at=SENT + dt.timedelta(minutes=12),
                        renewal_of=first_entry.root_message_id,
                    )
                self.assertEqual(
                    context.exception.code,
                    "lifecycle.renewal_semantics",
                )

        refreshed = root(
            "request",
            48,
            sent_at=SENT + dt.timedelta(minutes=12),
            idempotency_key=key,
        )
        refreshed["authorization"] = {
            "basis": "operator_confirmation",
            "authority": "Example Operator",
            "reference": "refreshed confirmation",
            "verified_at": timestamp(SENT + dt.timedelta(minutes=12)),
            "expires_at": timestamp(SENT + dt.timedelta(minutes=30)),
        }
        renewed = register(
            projection,
            refreshed,
            observed_at=SENT + dt.timedelta(minutes=12),
            renewal_of=first_entry.root_message_id,
        )
        self.assertEqual(renewed.renewal_of, first_entry.root_message_id)

    def test_scope_order_does_not_change_renewal_semantics(self) -> None:
        key = identifier(49)
        first = root("request", 50, idempotency_key=key)
        projection = LifecycleProjection(PROJECT_ID)
        first_entry = register(projection, first)
        projection.mark_expired_unconfirmed(
            first_entry.root_message_id,
            observed_at=timestamp(SENT + dt.timedelta(minutes=11)),
        )
        renewed_root = root(
            "request",
            51,
            sent_at=SENT + dt.timedelta(minutes=12),
            idempotency_key=key,
        )
        renewed_root["action"]["scope"]["repositories"].reverse()

        renewed = register(
            projection,
            renewed_root,
            observed_at=SENT + dt.timedelta(minutes=12),
            renewal_of=first_entry.root_message_id,
        )
        self.assertEqual(renewed.renewal_of, first_entry.root_message_id)

    def test_late_rejection_is_distinct_and_allows_fresh_renewal(self) -> None:
        key = identifier(52)
        first = root("request", 53, idempotency_key=key)
        projection = LifecycleProjection(PROJECT_ID)
        first_entry = register(projection, first)
        late = projection.apply_reply(
            reply(
                first,
                "ack",
                "rejected",
                54,
                sent_at=SENT + dt.timedelta(minutes=11),
            )
        )
        self.assertEqual(late.state, LifecycleState.LATE_REJECTED)
        self.assertTrue(late.terminal)

        renewed_root = root(
            "request",
            55,
            sent_at=SENT + dt.timedelta(minutes=12),
            idempotency_key=key,
        )
        renewed = register(
            projection,
            renewed_root,
            observed_at=SENT + dt.timedelta(minutes=12),
            renewal_of=first_entry.root_message_id,
        )
        self.assertEqual(renewed.renewal_of, first_entry.root_message_id)

    def test_expiry_only_closes_unconfirmed_work(self) -> None:
        pending = root("request", 56)
        projection = LifecycleProjection(PROJECT_ID)
        register(projection, pending)
        with self.assertRaises(CamUsageError) as too_early:
            projection.mark_expired_unconfirmed(
                pending["message_id"],
                observed_at=timestamp(SENT + dt.timedelta(minutes=9)),
            )
        self.assertEqual(too_early.exception.code, "lifecycle.not_expired")

        projection.apply_reply(reply(pending, "ack", "received", 57))
        with self.assertRaises(CamUsageError) as confirmed:
            projection.mark_expired_unconfirmed(
                pending["message_id"],
                observed_at=timestamp(SENT + dt.timedelta(minutes=11)),
            )
        self.assertEqual(confirmed.exception.code, "lifecycle.already_confirmed")
        projection.apply_reply(
            reply(
                pending,
                "status",
                "accepted",
                58,
                sent_at=SENT + dt.timedelta(minutes=2),
            )
        )
        completed = projection.apply_reply(
            reply(
                pending,
                "result",
                "completed",
                61,
                sent_at=SENT + dt.timedelta(minutes=20),
            )
        )
        self.assertEqual(completed.state, LifecycleState.COMPLETED)

    def test_reply_cannot_predate_root_on_same_host(self) -> None:
        request = root("request", 59)
        projection = LifecycleProjection(PROJECT_ID)
        register(projection, request)

        with self.assertRaises(CamUsageError) as context:
            projection.apply_reply(
                reply(
                    request,
                    "ack",
                    "received",
                    60,
                    sent_at=SENT - dt.timedelta(minutes=1),
                )
            )
        self.assertEqual(context.exception.code, "lifecycle.reply_predates_root")

    def test_projection_output_is_stable_and_body_free(self) -> None:
        request = root("request", 33)
        projection = LifecycleProjection(PROJECT_ID)
        register(projection, request)
        rendered = projection.as_dict()
        self.assertEqual(rendered["format"], "CAM-LIFECYCLE/1")
        self.assertEqual(rendered["project_id"], PROJECT_ID)
        self.assertNotIn("body", str(rendered))


if __name__ == "__main__":
    unittest.main()
