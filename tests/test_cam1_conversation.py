# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import copy
import unittest
import uuid

from tools.cam1lib import builders, causal, conversation, journal, protocol

if __package__:
    from .test_cam1_causal import (
        NOW,
        RECIPIENT,
        RECIPIENT_SESSION,
        SENDER,
        _intent,
        _message_id,
        _not_attempted,
        _request,
    )
else:
    from test_cam1_causal import (
        NOW,
        RECIPIENT,
        RECIPIENT_SESSION,
        SENDER,
        _intent,
        _message_id,
        _not_attempted,
        _request,
    )


def intent(raw, sequence, *, reverse=False, link=None):
    value = _intent(
        raw,
        sequence=sequence,
        context=None,
        sender=RECIPIENT if reverse else SENDER,
        recipient=SENDER if reverse else RECIPIENT,
    )
    value["attributes"]["conversation_link"] = link.as_dict() if link else None
    return value


def received(raw, sequence, *, reverse=False):
    observation = {
        "event_type": "message.inbound.observed",
        "record_id": str(uuid.uuid4()),
        "sequence": sequence,
        "message": journal._encoded_message(raw),
    }
    validation = {
        "event_type": "message.inbound.validated",
        "sequence": sequence + 1,
        "attributes": {
            "observed_record_id": observation["record_id"],
            "message_id": str(uuid.UUID(_message_id(raw))),
            "sender_participant_id": RECIPIENT if reverse else SENDER,
            "recipient_participant_id": SENDER if reverse else RECIPIENT,
            "assessment": "validated",
            "lifecycle_committed": True,
        },
    }
    return [observation, validation]


class ConversationLinkTests(unittest.TestCase):
    def setUp(self):
        self.parent = _request(reverse=True)
        self.current = protocol.parse_exact_bytes(_request())
        self.records = [
            intent(self.parent, 1, reverse=True),
            *received(self.parent, 2, reverse=True),
        ]

    def link(self, **kwargs):
        return conversation.build_outbound_link(
            self.records,
            self.current,
            sender_participant_id=SENDER,
            recipient_participant_id=RECIPIENT,
            **kwargs,
        )

    def test_opt_in_derives_link_without_changing_wire_bytes(self):
        original = protocol.serialize_envelope(self.current)
        self.assertIsNone(self.link())
        link = self.link(continues_message=_message_id(self.parent))
        self.assertEqual(link.conversation_id, _message_id(self.parent))
        self.assertEqual(link.parent_message_id, _message_id(self.parent))
        self.assertEqual(conversation.parse_link(link.as_dict()), link)
        self.assertEqual(protocol.serialize_envelope(self.current), original)

    def test_discussion_group_does_not_merge_causal_conversations(self):
        parent_id = _message_id(self.parent)
        self.records[0]["attributes"]["causal_context"] = causal.CausalContext(
            parent_id, (), (), ()
        ).as_dict()
        for record in self.records:
            record["sequence"] += 1
        self.records.insert(
            0,
            {
                "event_type": "state.compatibility.gate_activated",
                "sequence": 1,
                "attributes": {"feature_id": "causal.ordering", "feature_version": 1},
            },
        )
        link = self.link(continues_message=parent_id)
        context = causal.build_outbound_context(
            self.records,
            self.current,
            sender_participant_id=SENDER,
            recipient_participant_id=RECIPIENT,
            renewal_of=None,
            retry_after_intent=None,
        )
        self.assertEqual(link.conversation_id, parent_id)
        self.assertEqual(context.conversation_id, self.current["message_id"])
        self.assertEqual(context.depends_on, ())

    def test_link_uuid_is_not_an_abbreviated_or_fabricated_identifier(self):
        for target in (
            _message_id(self.parent).replace("-", ""),
            "{" + _message_id(self.parent) + "}",
        ):
            with (
                self.subTest(target=target),
                self.assertRaises(conversation.ConversationError) as error,
            ):
                self.link(continues_message=target)
            self.assertEqual(error.exception.code, "conversation.identifier")
        link = self.link(continues_message=_message_id(self.parent).upper())
        self.assertEqual(link.parent_message_id, _message_id(self.parent))

    def test_uppercase_wire_id_matches_canonical_inbound_metadata(self):
        envelope = protocol.parse_exact_bytes(self.parent)
        envelope["message_id"] = "ABCDEF00-ABCD-4000-8000-ABCDEF000001"
        self.parent = protocol.serialize_envelope(envelope)
        self.records = [
            intent(self.parent, 1, reverse=True),
            *received(self.parent, 2, reverse=True),
        ]
        link = self.link(continues_message=envelope["message_id"])
        self.assertEqual(link.parent_message_id, envelope["message_id"].lower())
        self.assertEqual(link.conversation_id, envelope["message_id"].lower())

    def test_new_roots_and_lifecycle_replies_derive_same_audit_group(self):
        link = self.link(continues_message=_message_id(self.parent))
        current_raw = protocol.serialize_envelope(self.current)
        self.records.extend(
            [intent(current_raw, 4, link=link), *received(current_raw, 5)]
        )
        ack = builders.build_ack(
            current_raw,
            sender_vendor="claude-code",
            sender_name="recipient",
            sender_session=RECIPIENT_SESSION,
            reply_transport="claude_send_message",
            reply_address=RECIPIENT_SESSION,
            status_value="received",
            now=NOW,
        )
        self.records.extend(
            [intent(ack, 7, reverse=True), *received(ack, 8, reverse=True)]
        )
        self.current = protocol.parse_exact_bytes(_request())
        next_link = self.link(continues_message=_message_id(ack))
        self.assertEqual(next_link.conversation_id, _message_id(self.parent))
        self.assertEqual(next_link.parent_message_id, _message_id(ack))
        self.assertIsNone(self.current["in_reply_to"])

    def test_retry_inherits_original_link_or_legacy_absence(self):
        prior = self.records[0]
        self.assertIsNone(self.link(retry_after_intent=prior["record_id"]))
        link = conversation.ConversationLink(str(uuid.uuid4()), str(uuid.uuid4()))
        prior["attributes"]["conversation_link"] = link.as_dict()
        self.assertEqual(self.link(retry_after_intent=prior["record_id"]), link)
        for field in ("retry_after_intent", "renewal_of"):
            with (
                self.subTest(field=field),
                self.assertRaises(conversation.ConversationError) as error,
            ):
                self.link(
                    continues_message=_message_id(self.parent),
                    **{field: str(uuid.uuid4())},
                )
            self.assertEqual(error.exception.code, "conversation.argument_conflict")

    def legacy_retry_history(self, *, second_retry=False):
        oldest = _request()
        link = conversation.ConversationLink(_message_id(oldest), _message_id(oldest))
        original = intent(self.parent, 4, reverse=True, link=link)
        retry = intent(self.parent, 6, reverse=True)
        retry["attributes"].pop("conversation_link")
        retry["attributes"]["retry_after_intent"] = original["record_id"]
        self.records = [
            intent(oldest, 1),
            *received(oldest, 2),
            original,
            _not_attempted(original, sequence=5),
            retry,
        ]
        if second_retry:
            later = intent(self.parent, 8, reverse=True)
            later["attributes"]["retry_after_intent"] = retry["record_id"]
            self.records.extend([_not_attempted(retry, sequence=7), later])
            retry = later
        self.records.extend(received(self.parent, retry["sequence"] + 1, reverse=True))
        return link, original, retry

    def test_legacy_retry_omissions_preserve_links_and_unrelated_discussions(self):
        for second_retry in (False, True):
            with self.subTest(second_retry=second_retry):
                expected, _original, _retry = self.legacy_retry_history(
                    second_retry=second_retry
                )
                exact_history = copy.deepcopy(self.records)
                link = self.link(continues_message=_message_id(self.parent))
                self.assertEqual(link.conversation_id, expected.conversation_id)
                self.assertEqual(link.parent_message_id, _message_id(self.parent))
                self.assertEqual(self.records, exact_history)

                unrelated = _request(reverse=True)
                self.records.extend(
                    [
                        intent(unrelated, 20, reverse=True),
                        *received(unrelated, 21, reverse=True),
                    ]
                )
                other = self.link(continues_message=_message_id(unrelated))
                self.assertEqual(other.conversation_id, _message_id(unrelated))

    def test_new_retry_recovers_link_from_legacy_retry_lineage(self):
        expected, _original, latest = self.legacy_retry_history(second_retry=True)
        self.records = self.records[:-2]
        self.records.append(_not_attempted(latest, sequence=9))
        self.assertEqual(self.link(retry_after_intent=latest["record_id"]), expected)

    def test_retry_link_resolution_does_not_validate_unrelated_links(self):
        original = self.records[0]
        unrelated = intent(_request(), 10)
        unrelated["attributes"]["conversation_link"] = {"format": "unknown"}
        self.records.append(unrelated)
        self.assertIsNone(self.link(retry_after_intent=original["record_id"]))

    def test_legacy_omission_requires_exact_conclusive_retry_lineage(self):
        for variant in (
            "no_reference",
            "wrong_reference",
            "no_outcome",
            "accepted",
            "unknown",
            "duplicate_outcome",
            "late_outcome",
            "early_outcome",
            "different_bytes",
            "different_participant",
            "different_link",
        ):
            with self.subTest(variant=variant):
                expected, original, retry = self.legacy_retry_history()
                outcome = self.records[4]
                if variant == "no_reference":
                    retry["attributes"].pop("retry_after_intent")
                elif variant == "wrong_reference":
                    retry["attributes"]["retry_after_intent"] = self.records[0][
                        "record_id"
                    ]
                elif variant == "no_outcome":
                    self.records.remove(outcome)
                elif variant == "accepted":
                    outcome["event_type"] = "transport.accepted"
                elif variant == "unknown":
                    outcome["attributes"]["delivery_state"] = "unknown"
                elif variant == "duplicate_outcome":
                    self.records.append(copy.deepcopy(outcome))
                elif variant == "late_outcome":
                    outcome["sequence"] = retry["sequence"] + 1
                elif variant == "early_outcome":
                    outcome["sequence"] = original["sequence"]
                elif variant == "different_bytes":
                    retry["message"] = journal._encoded_message(self.parent + b"\n")
                elif variant == "different_participant":
                    retry["attributes"]["recipient_participant_id"] = str(uuid.uuid4())
                else:
                    retry["attributes"]["conversation_link"] = (
                        conversation.ConversationLink(
                            str(uuid.uuid4()), expected.parent_message_id
                        ).as_dict()
                    )
                with self.assertRaises(conversation.ConversationError) as error:
                    self.link(continues_message=_message_id(self.parent))
                self.assertEqual(error.exception.code, "conversation.message_conflict")

    def test_legacy_retry_cannot_skip_the_latest_attempt(self):
        _expected, original, latest = self.legacy_retry_history(second_retry=True)
        latest["attributes"]["retry_after_intent"] = original["record_id"]
        with self.assertRaises(conversation.ConversationError) as error:
            self.link(continues_message=_message_id(self.parent))
        self.assertEqual(error.exception.code, "conversation.message_conflict")

    def test_unknown_self_and_non_root_links_are_rejected(self):
        for target, code in (
            (str(uuid.uuid4()), "conversation.parent_unknown"),
            (self.current["message_id"], "conversation.self_reference"),
            ("bad", "conversation.identifier"),
        ):
            with (
                self.subTest(target=target),
                self.assertRaises(conversation.ConversationError) as error,
            ):
                self.link(continues_message=target)
            self.assertEqual(error.exception.code, code)
        self.current["type"] = "ack"
        with self.assertRaises(conversation.ConversationError) as error:
            self.link(continues_message=_message_id(self.parent))
        self.assertEqual(error.exception.code, "conversation.root_required")

    def test_observed_held_mismatched_or_reordered_evidence_is_not_received(self):
        for variant in (
            "unobserved",
            "unvalidated",
            "held",
            "different_bytes",
            "different_recipient",
            "early_validation",
        ):
            with self.subTest(variant=variant):
                self.setUp()
                if variant == "unobserved":
                    self.records.pop(1)
                elif variant == "unvalidated":
                    self.records.pop(2)
                elif variant == "held":
                    self.records[2]["attributes"]["assessment"] = (
                        "held_for_clarification"
                    )
                elif variant == "different_bytes":
                    self.records[1]["message"] = journal._encoded_message(
                        self.parent + b"\n"
                    )
                elif variant == "different_recipient":
                    self.records[2]["attributes"]["recipient_participant_id"] = str(
                        uuid.uuid4()
                    )
                else:
                    self.records[2]["sequence"] = 1
                with self.assertRaises(conversation.ConversationError) as error:
                    self.link(continues_message=_message_id(self.parent))
                self.assertEqual(
                    error.exception.code, "conversation.parent_not_received"
                )

    def test_participant_and_session_mismatch_are_rejected(self):
        for field in ("participant", "session"):
            with self.subTest(field=field):
                self.setUp()
                if field == "participant":
                    self.records[0]["attributes"]["sender_participant_id"] = str(
                        uuid.uuid4()
                    )
                else:
                    self.current["recipient"]["session_id"] = str(uuid.uuid4())
                with self.assertRaises(conversation.ConversationError) as error:
                    self.link(continues_message=_message_id(self.parent))
                self.assertEqual(
                    error.exception.code, "conversation.participant_mismatch"
                )

    def test_false_root_dangling_or_forward_ancestry_is_rejected(self):
        oldest = _request()
        for variant, code in (
            ("false_root", "conversation.root_mismatch"),
            ("dangling", "conversation.parent_unknown"),
            ("forward", "conversation.ancestry_order"),
        ):
            with self.subTest(variant=variant):
                self.setUp()
                oldest_id = _message_id(oldest)
                link = conversation.ConversationLink(oldest_id, oldest_id)
                self.records[0]["sequence"] = 4
                self.records[1]["sequence"] = 5
                self.records[2]["sequence"] = 6
                self.records.insert(0, intent(oldest, 7 if variant == "forward" else 1))
                if variant == "false_root":
                    link = conversation.ConversationLink(str(uuid.uuid4()), oldest_id)
                elif variant == "dangling":
                    link = conversation.ConversationLink(oldest_id, str(uuid.uuid4()))
                self.records[1]["attributes"]["conversation_link"] = link.as_dict()
                with self.assertRaises(conversation.ConversationError) as error:
                    self.link(continues_message=_message_id(self.parent))
                self.assertEqual(error.exception.code, code)

    def test_conflicting_retry_metadata_and_unknown_link_fields_are_rejected(self):
        duplicate = copy.deepcopy(self.records[0])
        duplicate["attributes"]["conversation_link"] = conversation.ConversationLink(
            str(uuid.uuid4()), str(uuid.uuid4())
        ).as_dict()
        self.records.append(duplicate)
        with self.assertRaises(conversation.ConversationError) as error:
            self.link(continues_message=_message_id(self.parent))
        self.assertEqual(error.exception.code, "conversation.message_conflict")
        for value in ({}, {"format": "future"}, {"conversation_id": "bad"}):
            with (
                self.subTest(value=value),
                self.assertRaises(conversation.ConversationError),
            ):
                conversation.parse_link(value)


if __name__ == "__main__":
    unittest.main()
