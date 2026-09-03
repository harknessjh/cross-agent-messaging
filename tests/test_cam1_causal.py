# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import datetime as dt
import hashlib
import unittest
import uuid
from unittest import mock

from tools.cam1lib import builders, causal, journal, protocol

SENDER = "00000000-0000-4000-8000-000000000201"
RECIPIENT = "00000000-0000-4000-8000-000000000202"
THIRD_PARTY = "00000000-0000-4000-8000-000000000203"
SENDER_SESSION = "00000000-0000-4000-8000-000000000101"
RECIPIENT_SESSION = "00000000-0000-4000-8000-000000000102"
NOW = dt.datetime(2026, 9, 2, 12, 0, tzinfo=dt.UTC)


def _request(*, reverse: bool = False) -> bytes:
    sender_name, recipient_name = (
        ("recipient", "sender") if reverse else ("sender", "recipient")
    )
    sender_session, recipient_session = (
        (RECIPIENT_SESSION, SENDER_SESSION)
        if reverse
        else (SENDER_SESSION, RECIPIENT_SESSION)
    )
    return builders.build_request(
        sender_vendor="claude-code" if reverse else "codex",
        sender_name=sender_name,
        sender_session=sender_session,
        recipient_vendor="codex" if reverse else "claude-code",
        recipient_name=recipient_name,
        recipient_session=recipient_session,
        reply_transport="claude_send_message" if reverse else "codex_queue",
        reply_address=sender_session,
        risk_class="informational",
        operation="review",
        intent="Review one bounded fact",
        body="Review this fact without making changes.",
        authorization_basis="none",
        now=NOW,
    )


def _message_id(raw: bytes) -> str:
    return protocol.parse_exact_bytes(raw)["message_id"]


def _activation(sequence: int = 1) -> dict[str, object]:
    return {
        "event_type": "state.compatibility.gate_activated",
        "sequence": sequence,
        "attributes": {
            "feature_id": causal.CAUSAL_FEATURE_ID,
            "feature_version": causal.CAUSAL_FEATURE_VERSION,
        },
    }


def _intent(
    raw: bytes,
    *,
    sequence: int,
    sender: str = SENDER,
    recipient: str = RECIPIENT,
    context: causal.CausalContext | None,
    renewal_of: str | None = None,
    retry_after_intent: str | None = None,
    record_id: str | None = None,
) -> dict[str, object]:
    return {
        "event_type": "message.outbound.intent",
        "record_id": record_id or str(uuid.uuid4()),
        "sequence": sequence,
        "message": journal._encoded_message(raw),
        "attributes": {
            "message_id": _message_id(raw),
            "sender_participant_id": sender,
            "recipient_participant_id": recipient,
            "participant_id": recipient,
            "transport": "test_transport",
            "route_address": "test-route",
            "renewal_of": renewal_of,
            "retry_after_intent": retry_after_intent,
            "causal_context": context.as_dict() if context is not None else None,
        },
    }


def _validated(
    message_id: str,
    *,
    sequence: int,
    sender: str,
    recipient: str,
) -> dict[str, object]:
    return {
        "event_type": "message.inbound.validated",
        "sequence": sequence,
        "attributes": {
            "message_id": message_id,
            "sender_participant_id": sender,
            "recipient_participant_id": recipient,
        },
    }


def _accepted(intent: dict[str, object], *, sequence: int) -> dict[str, object]:
    return {
        "event_type": "transport.accepted",
        "sequence": sequence,
        "attributes": {"intent_record_id": intent["record_id"]},
    }


def _not_attempted(
    intent: dict[str, object],
    *,
    sequence: int,
    changed: dict[str, object] | None = None,
) -> dict[str, object]:
    intent_attributes = intent["attributes"]
    assert isinstance(intent_attributes, dict)
    attributes = {
        "intent_record_id": intent["record_id"],
        "participant_id": intent_attributes["participant_id"],
        "message_id": intent_attributes["message_id"],
        "transport": intent_attributes["transport"],
        "route_address": intent_attributes["route_address"],
        "delivery_state": "not_attempted",
        "error_code": "transport.synthetic_stop",
        "observed_at": "2026-09-02T12:00:00Z",
    }
    attributes.update(changed or {})
    return {
        "event_type": "transport.not_accepted",
        "sequence": sequence,
        "attributes": attributes,
    }


class CausalContextTests(unittest.TestCase):
    def test_schema_requires_canonical_sorted_bounded_uuid_arrays(self) -> None:
        first = "00000000-0000-4000-8000-000000000001"
        second = "00000000-0000-4000-8000-000000000002"
        valid = {
            "format": causal.CAUSAL_FORMAT,
            "conversation_id": first,
            "depends_on": [first, second],
            "supersedes": [],
            "recipient_frontier": [],
        }
        self.assertEqual(causal.parse_context(valid).depends_on, (first, second))

        for changed, code in (
            ({**valid, "depends_on": [second, first]}, "causal.context"),
            ({**valid, "depends_on": [first, first]}, "causal.context"),
            (
                {
                    **valid,
                    "depends_on": [str(uuid.uuid4()) for _ in range(65)],
                },
                "causal.frontier_too_large",
            ),
        ):
            with self.subTest(code=code), self.assertRaises(causal.CausalError) as ctx:
                causal.parse_context(changed)
            self.assertEqual(ctx.exception.code, code)

    def test_frontier_handles_a_chain_deeper_than_python_recursion_limit(
        self,
    ) -> None:
        identifiers = [str(uuid.UUID(int=value)) for value in range(1, 1_501)]
        contexts = {
            identifier: causal.CausalContext(
                identifiers[0],
                ((identifiers[index - 1],) if index else ()),
                (),
                (),
            )
            for index, identifier in enumerate(identifiers)
        }

        self.assertEqual(
            causal._frontier(identifiers, contexts),
            (identifiers[-1],),
        )

    def test_frontier_rejects_more_than_64_independent_heads(self) -> None:
        identifiers = [str(uuid.UUID(int=value)) for value in range(1, 66)]
        contexts = {
            identifier: causal.CausalContext(identifier, (), (), ())
            for identifier in identifiers
        }

        with self.assertRaises(causal.CausalError) as context:
            causal._frontier(identifiers, contexts)

        self.assertEqual(context.exception.code, "causal.frontier_too_large")

    def test_retry_heavy_history_resolves_each_message_group_once(self) -> None:
        root = _request()
        root_id = _message_id(root)
        retries = tuple(
            _intent(
                root,
                sequence=sequence,
                context=causal.CausalContext(root_id, (), (), ()),
            )
            for sequence in range(2, 202)
        )
        reply = builders.build_ack(
            root,
            sender_vendor="claude-code",
            sender_name="recipient",
            sender_session=RECIPIENT_SESSION,
            reply_transport="claude_send_message",
            reply_address=RECIPIENT_SESSION,
            status_value="received",
            now=NOW,
        )
        reply_intent = _intent(
            reply,
            sequence=202,
            sender=RECIPIENT,
            recipient=SENDER,
            context=causal.CausalContext(
                root_id,
                (root_id,),
                (),
                (root_id,),
            ),
        )
        intents = causal._all_intents((*retries, reply_intent))

        with mock.patch.object(
            causal,
            "_consistent_intent",
            wraps=causal._consistent_intent,
        ) as resolve:
            index = causal._validate_intent_contexts(intents)

        self.assertEqual(set(index), {root_id, _message_id(reply)})
        self.assertEqual(resolve.call_count, len(index))

    def test_new_root_defaults_to_own_conversation_after_activation(self) -> None:
        raw = _request()
        context = causal.build_outbound_context(
            (_activation(),),
            protocol.parse_exact_bytes(raw),
            sender_participant_id=SENDER,
            recipient_participant_id=RECIPIENT,
            renewal_of=None,
            retry_after_intent=None,
        )
        self.assertIsNotNone(context)
        assert context is not None
        self.assertEqual(context.conversation_id, _message_id(raw))
        self.assertEqual(context.depends_on, ())
        self.assertEqual(context.supersedes, ())
        self.assertEqual(context.recipient_frontier, ())

    def test_duplicate_activation_does_not_move_the_enforcement_cutoff(self) -> None:
        raw = _request()
        message_id = _message_id(raw)
        intent = _intent(
            raw,
            sequence=2,
            context=causal.CausalContext(message_id, (), (), ()),
        )

        assessment = causal.assess_inbound_order(
            (_activation(sequence=1), intent, _activation(sequence=3)),
            raw,
            local_participant_id=RECIPIENT,
            sender_participant_id=SENDER,
        )

        self.assertTrue(assessment.enforced)
        self.assertFalse(assessment.held)
        self.assertEqual(assessment.conversation_id, message_id)

    def test_reply_inherits_conversation_and_validated_recipient_frontier(self) -> None:
        root = _request()
        root_id = _message_id(root)
        root_context = causal.CausalContext(root_id, (), (), ())
        root_intent = _intent(root, sequence=2, context=root_context)
        recipient_reply = builders.build_ack(
            root,
            sender_vendor="claude-code",
            sender_name="recipient",
            sender_session=RECIPIENT_SESSION,
            reply_transport="claude_send_message",
            reply_address=RECIPIENT_SESSION,
            status_value="received",
            now=NOW,
        )
        reply_id = _message_id(recipient_reply)
        reply_context = causal.CausalContext(root_id, (root_id,), (), (root_id,))
        reply_intent = _intent(
            recipient_reply,
            sequence=4,
            sender=RECIPIENT,
            recipient=SENDER,
            context=reply_context,
        )
        result = _request()
        records = (
            _activation(),
            root_intent,
            _accepted(root_intent, sequence=3),
            reply_intent,
            _validated(
                reply_id,
                sequence=5,
                sender=RECIPIENT,
                recipient=SENDER,
            ),
        )
        context = causal.build_outbound_context(
            records,
            protocol.parse_exact_bytes(result),
            sender_participant_id=SENDER,
            recipient_participant_id=RECIPIENT,
            renewal_of=root_id,
            retry_after_intent=None,
        )
        assert context is not None
        self.assertEqual(context.conversation_id, root_id)
        self.assertEqual(context.depends_on, ())
        self.assertEqual(context.supersedes, (root_id,))
        self.assertEqual(context.recipient_frontier, (reply_id,))

    def test_observed_and_third_party_messages_never_enter_frontier(self) -> None:
        root = _request()
        root_id = _message_id(root)
        root_intent = _intent(
            root,
            sequence=2,
            context=causal.CausalContext(root_id, (), (), ()),
        )
        third = _request(reverse=True)
        third_id = _message_id(third)
        third_intent = _intent(
            third,
            sequence=3,
            sender=THIRD_PARTY,
            recipient=SENDER,
            context=causal.CausalContext(third_id, (), (), ()),
        )
        records = (
            _activation(),
            root_intent,
            third_intent,
            {
                "event_type": "message.inbound.observed",
                "sequence": 4,
                "message": journal._encoded_message(third),
                "attributes": {},
            },
            _validated(
                third_id,
                sequence=5,
                sender=THIRD_PARTY,
                recipient=SENDER,
            ),
        )
        followup = _request()
        context = causal.build_outbound_context(
            records,
            protocol.parse_exact_bytes(followup),
            sender_participant_id=SENDER,
            recipient_participant_id=RECIPIENT,
            renewal_of=root_id,
            retry_after_intent=None,
        )
        assert context is not None
        self.assertEqual(context.recipient_frontier, ())

    def test_retry_reuses_original_context_despite_newer_inbound(self) -> None:
        root = _request()
        root_id = _message_id(root)
        original_context = causal.CausalContext(root_id, (), (), ())
        record_id = "00000000-0000-4000-8000-000000000301"
        intent = _intent(
            root,
            sequence=2,
            context=original_context,
            record_id=record_id,
        )
        recipient_reply = builders.build_ack(
            root,
            sender_vendor="claude-code",
            sender_name="recipient",
            sender_session=RECIPIENT_SESSION,
            reply_transport="claude_send_message",
            reply_address=RECIPIENT_SESSION,
            status_value="received",
            now=NOW,
        )
        reply_intent = _intent(
            recipient_reply,
            sequence=3,
            sender=RECIPIENT,
            recipient=SENDER,
            context=causal.CausalContext(
                root_id,
                (root_id,),
                (),
                (root_id,),
            ),
        )
        retry_context = causal.build_outbound_context(
            (
                _activation(),
                intent,
                reply_intent,
                _validated(
                    _message_id(recipient_reply),
                    sequence=4,
                    sender=RECIPIENT,
                    recipient=SENDER,
                ),
            ),
            protocol.parse_exact_bytes(root),
            sender_participant_id=SENDER,
            recipient_participant_id=RECIPIENT,
            renewal_of=None,
            retry_after_intent=record_id,
        )
        self.assertEqual(retry_context, original_context)

    def test_preactivation_reply_and_renewal_remain_grandfathered(self) -> None:
        root = _request()
        old_intent = _intent(root, sequence=1, context=None)
        activation = _activation(sequence=2)
        reply = builders.build_ack(
            root,
            sender_vendor="claude-code",
            sender_name="recipient",
            sender_session=RECIPIENT_SESSION,
            reply_transport="claude_send_message",
            reply_address=RECIPIENT_SESSION,
            status_value="received",
            now=NOW,
        )
        context = causal.build_outbound_context(
            (old_intent, activation),
            protocol.parse_exact_bytes(reply),
            sender_participant_id=RECIPIENT,
            recipient_participant_id=SENDER,
            renewal_of=None,
            retry_after_intent=None,
        )
        self.assertIsNone(context)

        renewal = _request()
        renewal_context = causal.build_outbound_context(
            (old_intent, activation),
            protocol.parse_exact_bytes(renewal),
            sender_participant_id=SENDER,
            recipient_participant_id=RECIPIENT,
            renewal_of=_message_id(root),
            retry_after_intent=None,
        )
        self.assertIsNone(renewal_context)

        cancel = builders.build_cancel(
            root,
            sender_vendor="codex",
            sender_name="sender",
            sender_session=SENDER_SESSION,
            reply_transport="codex_queue",
            reply_address=SENDER_SESSION,
            authority="Test operator",
            authorization_reference="operator requested cancellation",
            authorization_verified_at="2026-09-02T12:00:00Z",
            authorization_expires_at="2026-09-02T12:10:00Z",
            now=NOW,
        )
        cancel_context = causal.build_outbound_context(
            (old_intent, activation),
            protocol.parse_exact_bytes(cancel),
            sender_participant_id=SENDER,
            recipient_participant_id=RECIPIENT,
            renewal_of=None,
            retry_after_intent=None,
        )
        self.assertIsNone(cancel_context)
        cancel_intent = _intent(cancel, sequence=3, context=None)
        assessment = causal.assess_inbound_order(
            (old_intent, activation, cancel_intent),
            cancel,
            local_participant_id=RECIPIENT,
            sender_participant_id=SENDER,
        )
        self.assertFalse(assessment.enforced)
        self.assertFalse(assessment.held)

    def test_multihop_preactivation_conversation_remains_grandfathered(self) -> None:
        root = _request()
        root_id = _message_id(root)
        root_intent = _intent(root, sequence=1, context=None)
        activation = _activation(sequence=2)
        renewal_one = _request()
        renewal_one_id = _message_id(renewal_one)
        renewal_one_record_id = "00000000-0000-4000-8000-000000000401"
        renewal_one_intent = _intent(
            renewal_one,
            sequence=3,
            context=None,
            renewal_of=root_id,
            record_id=renewal_one_record_id,
        )
        records = (root_intent, activation, renewal_one_intent)

        renewal_two = _request()
        self.assertIsNone(
            causal.build_outbound_context(
                records,
                protocol.parse_exact_bytes(renewal_two),
                sender_participant_id=SENDER,
                recipient_participant_id=RECIPIENT,
                renewal_of=renewal_one_id,
                retry_after_intent=None,
            )
        )
        renewal_two_intent = _intent(
            renewal_two,
            sequence=4,
            context=None,
            renewal_of=renewal_one_id,
        )

        reply = builders.build_ack(
            renewal_one,
            sender_vendor="claude-code",
            sender_name="recipient",
            sender_session=RECIPIENT_SESSION,
            reply_transport="claude_send_message",
            reply_address=RECIPIENT_SESSION,
            status_value="received",
            now=NOW,
        )
        self.assertIsNone(
            causal.build_outbound_context(
                records,
                protocol.parse_exact_bytes(reply),
                sender_participant_id=RECIPIENT,
                recipient_participant_id=SENDER,
                renewal_of=None,
                retry_after_intent=None,
            )
        )

        cancel = builders.build_cancel(
            renewal_one,
            sender_vendor="codex",
            sender_name="sender",
            sender_session=SENDER_SESSION,
            reply_transport="codex_queue",
            reply_address=SENDER_SESSION,
            authority="Test operator",
            authorization_reference="operator requested cancellation",
            authorization_verified_at="2026-09-02T12:00:00Z",
            authorization_expires_at="2026-09-02T12:10:00Z",
            now=NOW,
        )
        self.assertIsNone(
            causal.build_outbound_context(
                records,
                protocol.parse_exact_bytes(cancel),
                sender_participant_id=SENDER,
                recipient_participant_id=RECIPIENT,
                renewal_of=None,
                retry_after_intent=None,
            )
        )
        self.assertIsNone(
            causal.build_outbound_context(
                records,
                protocol.parse_exact_bytes(renewal_one),
                sender_participant_id=SENDER,
                recipient_participant_id=RECIPIENT,
                renewal_of=root_id,
                retry_after_intent=renewal_one_record_id,
            )
        )

        assessment = causal.assess_inbound_order(
            (*records, renewal_two_intent),
            renewal_two,
            local_participant_id=RECIPIENT,
            sender_participant_id=SENDER,
        )
        self.assertFalse(assessment.enforced)
        self.assertFalse(assessment.held)

    def test_multihop_preactivation_exact_retries_remain_grandfathered(self) -> None:
        raw = _request()
        original_record_id = "00000000-0000-4000-8000-000000000411"
        first_retry_record_id = "00000000-0000-4000-8000-000000000412"
        second_retry_record_id = "00000000-0000-4000-8000-000000000413"
        original = _intent(
            raw,
            sequence=1,
            context=None,
            record_id=original_record_id,
        )
        first_retry = _intent(
            raw,
            sequence=3,
            context=None,
            retry_after_intent=original_record_id,
            record_id=first_retry_record_id,
        )
        second_retry = _intent(
            raw,
            sequence=4,
            context=None,
            retry_after_intent=first_retry_record_id,
            record_id=second_retry_record_id,
        )
        records = (original, _activation(sequence=2), first_retry, second_retry)

        for retry_after in (first_retry_record_id, second_retry_record_id):
            with self.subTest(retry_after=retry_after):
                self.assertIsNone(
                    causal.build_outbound_context(
                        records,
                        protocol.parse_exact_bytes(raw),
                        sender_participant_id=SENDER,
                        recipient_participant_id=RECIPIENT,
                        renewal_of=None,
                        retry_after_intent=retry_after,
                    )
                )

        assessment = causal.assess_inbound_order(
            records,
            raw,
            local_participant_id=RECIPIENT,
            sender_participant_id=SENDER,
        )
        self.assertFalse(assessment.enforced)
        self.assertFalse(assessment.held)

    def test_postactivation_cancel_depends_on_target_conversation(self) -> None:
        root = _request()
        root_id = _message_id(root)
        root_intent = _intent(
            root,
            sequence=2,
            context=causal.CausalContext(root_id, (), (), ()),
        )
        cancel = builders.build_cancel(
            root,
            sender_vendor="codex",
            sender_name="sender",
            sender_session=SENDER_SESSION,
            reply_transport="codex_queue",
            reply_address=SENDER_SESSION,
            authority="Test operator",
            authorization_reference="operator requested cancellation",
            authorization_verified_at="2026-09-02T12:00:00Z",
            authorization_expires_at="2026-09-02T12:10:00Z",
            now=NOW,
        )

        context = causal.build_outbound_context(
            (_activation(), root_intent),
            protocol.parse_exact_bytes(cancel),
            sender_participant_id=SENDER,
            recipient_participant_id=RECIPIENT,
            renewal_of=None,
            retry_after_intent=None,
        )

        assert context is not None
        self.assertEqual(context.conversation_id, root_id)
        self.assertEqual(context.depends_on, (root_id,))
        self.assertEqual(context.supersedes, ())

    def test_stale_request_requires_potentially_dispatched_frontier(self) -> None:
        conversation_root = _request()
        conversation_id = _message_id(conversation_root)
        root_intent = _intent(
            conversation_root,
            sequence=2,
            context=causal.CausalContext(conversation_id, (), (), ()),
        )
        recipient_raw = builders.build_ack(
            conversation_root,
            sender_vendor="claude-code",
            sender_name="recipient",
            sender_session=RECIPIENT_SESSION,
            reply_transport="claude_send_message",
            reply_address=RECIPIENT_SESSION,
            status_value="received",
            now=NOW,
        )
        recipient_message = _intent(
            recipient_raw,
            sequence=4,
            sender=RECIPIENT,
            recipient=SENDER,
            context=causal.CausalContext(
                conversation_id,
                (conversation_id,),
                (),
                (conversation_id,),
            ),
        )
        stale = _request()
        stale_intent = _intent(
            stale,
            sequence=3,
            context=causal.CausalContext(
                conversation_id,
                (),
                (conversation_id,),
                (),
            ),
            renewal_of=conversation_id,
        )
        records = (
            _activation(),
            root_intent,
            stale_intent,
            recipient_message,
            _accepted(recipient_message, sequence=5),
        )
        assessment = causal.assess_inbound_order(
            records,
            stale,
            local_participant_id=RECIPIENT,
            sender_participant_id=SENDER,
        )
        self.assertTrue(assessment.enforced)
        self.assertTrue(assessment.held)
        self.assertEqual(assessment.missing_frontier, (_message_id(recipient_raw),))

    def test_conclusively_not_attempted_message_is_not_required(self) -> None:
        root = _request()
        conversation_id = _message_id(root)
        root_intent = _intent(
            root,
            sequence=2,
            context=causal.CausalContext(conversation_id, (), (), ()),
        )
        recipient_raw = builders.build_ack(
            root,
            sender_vendor="claude-code",
            sender_name="recipient",
            sender_session=RECIPIENT_SESSION,
            reply_transport="claude_send_message",
            reply_address=RECIPIENT_SESSION,
            status_value="received",
            now=NOW,
        )
        recipient_intent = _intent(
            recipient_raw,
            sequence=3,
            sender=RECIPIENT,
            recipient=SENDER,
            context=causal.CausalContext(
                conversation_id,
                (conversation_id,),
                (),
                (conversation_id,),
            ),
        )
        outcome = _not_attempted(recipient_intent, sequence=4)
        current = _request()
        current_intent = _intent(
            current,
            sequence=5,
            context=causal.CausalContext(
                conversation_id,
                (),
                (conversation_id,),
                (),
            ),
            renewal_of=conversation_id,
        )
        assessment = causal.assess_inbound_order(
            (_activation(), root_intent, recipient_intent, outcome, current_intent),
            current,
            local_participant_id=RECIPIENT,
            sender_participant_id=SENDER,
        )
        self.assertFalse(assessment.held)

    def test_preintent_or_malformed_not_attempted_outcome_remains_required(
        self,
    ) -> None:
        root = _request()
        conversation_id = _message_id(root)
        root_intent = _intent(
            root,
            sequence=2,
            context=causal.CausalContext(conversation_id, (), (), ()),
        )
        recipient_raw = builders.build_ack(
            root,
            sender_vendor="claude-code",
            sender_name="recipient",
            sender_session=RECIPIENT_SESSION,
            reply_transport="claude_send_message",
            reply_address=RECIPIENT_SESSION,
            status_value="received",
            now=NOW,
        )
        recipient_intent = _intent(
            recipient_raw,
            sequence=4,
            sender=RECIPIENT,
            recipient=SENDER,
            context=causal.CausalContext(
                conversation_id,
                (conversation_id,),
                (),
                (conversation_id,),
            ),
        )
        current = _request()
        current_intent = _intent(
            current,
            sequence=6,
            context=causal.CausalContext(
                conversation_id,
                (),
                (conversation_id,),
                (),
            ),
            renewal_of=conversation_id,
        )
        malformed_cases = {
            "pre_intent": _not_attempted(recipient_intent, sequence=3),
            "pre_intent_plus_valid": (
                _not_attempted(recipient_intent, sequence=3),
                _not_attempted(recipient_intent, sequence=5),
            ),
            "missing_message_id": _not_attempted(
                recipient_intent,
                sequence=5,
                changed={"message_id": None},
            ),
            "wrong_participant": _not_attempted(
                recipient_intent,
                sequence=5,
                changed={"participant_id": THIRD_PARTY},
            ),
            "wrong_transport": _not_attempted(
                recipient_intent,
                sequence=5,
                changed={"transport": "different_transport"},
            ),
            "missing_route": _not_attempted(
                recipient_intent,
                sequence=5,
                changed={"route_address": None},
            ),
            "missing_error": _not_attempted(
                recipient_intent,
                sequence=5,
                changed={"error_code": None},
            ),
            "missing_observation_time": _not_attempted(
                recipient_intent,
                sequence=5,
                changed={"observed_at": None},
            ),
        }

        for name, outcomes in malformed_cases.items():
            with self.subTest(name=name):
                outcome_records = (
                    outcomes if isinstance(outcomes, tuple) else (outcomes,)
                )
                assessment = causal.assess_inbound_order(
                    (
                        _activation(),
                        root_intent,
                        *(
                            record
                            for record in outcome_records
                            if record["sequence"] < recipient_intent["sequence"]
                        ),
                        recipient_intent,
                        *(
                            record
                            for record in outcome_records
                            if record["sequence"] > recipient_intent["sequence"]
                        ),
                        current_intent,
                    ),
                    current,
                    local_participant_id=RECIPIENT,
                    sender_participant_id=SENDER,
                )
                self.assertTrue(assessment.held)
                self.assertEqual(
                    assessment.missing_frontier,
                    (_message_id(recipient_raw),),
                )

    def test_terminal_lifecycle_record_does_not_override_causal_frontier(self) -> None:
        root = _request()
        root_id = _message_id(root)
        root_intent = _intent(
            root,
            sequence=2,
            context=causal.CausalContext(root_id, (), (), ()),
        )
        recipient_raw = builders.build_ack(
            root,
            sender_vendor="claude-code",
            sender_name="recipient",
            sender_session=RECIPIENT_SESSION,
            reply_transport="claude_send_message",
            reply_address=RECIPIENT_SESSION,
            status_value="rejected",
            now=NOW,
        )
        recipient_id = _message_id(recipient_raw)
        recipient_intent = _intent(
            recipient_raw,
            sequence=3,
            sender=RECIPIENT,
            recipient=SENDER,
            context=causal.CausalContext(
                root_id,
                (root_id,),
                (),
                (root_id,),
            ),
        )
        terminal = {
            "event_type": "state.lifecycle.reply_applied",
            "sequence": 5,
            "attributes": {
                "message_id": recipient_id,
                "root_message_id": root_id,
                "message_type": "ack",
            },
        }
        renewal = _request()
        renewal_intent = _intent(
            renewal,
            sequence=6,
            context=causal.CausalContext(root_id, (), (root_id,), ()),
            renewal_of=root_id,
        )

        assessment = causal.assess_inbound_order(
            (
                _activation(),
                root_intent,
                recipient_intent,
                _accepted(recipient_intent, sequence=4),
                terminal,
                renewal_intent,
            ),
            renewal,
            local_participant_id=RECIPIENT,
            sender_participant_id=SENDER,
        )

        self.assertTrue(assessment.held)
        self.assertEqual(assessment.missing_frontier, (recipient_id,))

    def test_unreferenced_duplicate_intent_conflicts_fail_closed(self) -> None:
        original = _request()
        original_id = _message_id(original)
        first = _intent(
            original,
            sequence=2,
            context=causal.CausalContext(original_id, (), (), ()),
        )
        changed_envelope = protocol.parse_exact_bytes(original)
        changed_envelope["body"] = "Different exact bytes under the same message ID."
        changed_envelope["body_sha256"] = hashlib.sha256(
            changed_envelope["body"].encode("utf-8")
        ).hexdigest()
        changed = protocol.serialize_envelope(changed_envelope)
        conflicting = _intent(
            changed,
            sequence=3,
            context=causal.CausalContext(original_id, (), (), ()),
        )
        unrelated = _request()
        unrelated_id = _message_id(unrelated)
        current = _intent(
            unrelated,
            sequence=4,
            context=causal.CausalContext(unrelated_id, (), (), ()),
        )

        with self.assertRaises(causal.CausalError) as context:
            causal.assess_inbound_order(
                (_activation(), first, conflicting, current),
                unrelated,
                local_participant_id=RECIPIENT,
                sender_participant_id=SENDER,
            )

        self.assertEqual(context.exception.code, "causal.intent_conflict")


if __name__ == "__main__":
    unittest.main()
