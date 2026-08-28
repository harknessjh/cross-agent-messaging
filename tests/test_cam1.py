# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import ast
import datetime as dt
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar
from unittest import mock

from tools import cam1

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
NOW = dt.datetime(2026, 8, 21, 20, 5, tzinfo=dt.UTC)


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def changed(name: str, mutate) -> bytes:
    envelope = json.loads(fixture(name))
    mutate(envelope)
    return cam1.serialize_envelope(envelope)


def challenge_envelope() -> bytes:
    return changed(
        "valid-hello.json",
        lambda envelope: envelope.update(type="challenge"),
    )


def verify_envelope() -> bytes:
    return changed(
        "valid-ack.json",
        lambda envelope: envelope.update(
            type="verify",
            receipt=None,
            nonce="AAECAwQFBgcICQoLDA0ODw",
        ),
    )


class CamValidationTests(unittest.TestCase):
    def problem_codes(self, raw: bytes, **kwargs) -> list[cam1.Problem]:
        with self.assertRaises(cam1.CamValidationError) as context:
            cam1.validate_exact_bytes(raw, now=NOW, **kwargs)
        return list(context.exception.problems)

    def test_protocol_fixtures_validate_and_correlate(self) -> None:
        hello = cam1.validate_exact_bytes(fixture("valid-hello.json"), now=NOW)
        ack = cam1.validate_exact_bytes(
            fixture("valid-ack.json"),
            against_raw=fixture("valid-hello.json"),
            now=NOW,
        )
        self.assertTrue(hello.fresh)
        self.assertTrue(hello.body_hash_valid)
        self.assertIsNone(hello.correlated)
        self.assertTrue(ack.correlated)

    def test_validation_summary_preserves_the_profile_captured_at_start(self) -> None:
        captured = {
            "available": True,
            "format": "CAM-VALIDATION-PROFILE/1",
            "validation_profile_sha256": "a" * 64,
        }
        with mock.patch(
            "tools.cam1lib.validation.validation_profile_report",
            return_value=captured,
        ):
            result = cam1.validate_exact_bytes(fixture("valid-hello.json"), now=NOW)

        self.assertEqual(result.summary()["validation_profile"], captured)

    def test_preserved_valid_uuid_is_accepted(self) -> None:
        valid = "123e4567-e89b-42d3-a456-426614174000"
        raw = changed(
            "valid-hello.json",
            lambda envelope: envelope["action"].update(idempotency_key=valid),
        )
        result = cam1.validate_exact_bytes(raw, now=NOW)
        self.assertEqual(result.envelope["action"]["idempotency_key"], valid)

    def test_reconstructed_uuid_tail_is_rejected_without_echoing_value(self) -> None:
        invalid = "123e4567-e89b-42d3-a456-4266141740000"
        raw = changed(
            "valid-hello.json",
            lambda envelope: envelope["action"].update(idempotency_key=invalid),
        )
        problems = self.problem_codes(raw)
        semantic = [
            item
            for item in problems
            if item.code == "semantic.uuid" and item.path == "/action/idempotency_key"
        ]
        self.assertEqual(len(semantic), 1)
        self.assertIn("8-4-4-4-13", semantic[0].detail)
        self.assertNotIn(invalid, json.dumps([item.as_dict() for item in problems]))

    def test_abbreviated_ack_reports_required_missing_pointers(self) -> None:
        problems = self.problem_codes(fixture("abbreviated-ack.json"))
        missing = {item.path for item in problems if item.code == "schema.required"}
        expected = {
            "/expires_at",
            "/reply_to",
            "/action",
            "/authorization",
            "/constraints",
            "/body_sha256",
            "/evidence",
            "/claimed_sender/host_id",
            "/recipient/agent_name",
            "/receipt/detail",
        }
        self.assertTrue(expected.issubset(missing), missing)

    def test_removed_audit_ref_is_rejected_as_an_unknown_field(self) -> None:
        raw = changed(
            "valid-hello.json",
            lambda envelope: envelope.update(audit_ref=None),
        )
        problems = self.problem_codes(raw)
        self.assertIn(
            ("schema.additionalProperties", ""),
            {(item.code, item.path) for item in problems},
        )

    def test_duplicate_key_is_rejected_before_schema(self) -> None:
        problems = self.problem_codes(b'{"protocol":"CAM/1","protocol":"CAM/1"}')
        self.assertEqual(problems[0].code, "wire.duplicate_key")

    def test_malformed_utf8_is_rejected(self) -> None:
        problems = self.problem_codes(b'{"body":"\xff"}')
        self.assertEqual(problems[0].code, "wire.utf8")

    def test_non_finite_json_number_is_rejected(self) -> None:
        problems = self.problem_codes(b'{"value":NaN}')
        self.assertEqual(problems[0].code, "wire.json")

    def test_exponent_overflow_is_rejected_by_the_public_parser(self) -> None:
        with self.assertRaises(cam1.CamValidationError) as context:
            cam1.parse_exact_bytes(b'{"value":1e309}')
        self.assertEqual(context.exception.problems[0].code, "wire.json")

    def test_schema_invalid_scalars_return_bounded_diagnostics(self) -> None:
        cases = (
            (
                "array message type",
                lambda envelope: envelope.update(type=[]),
                "schema.enum",
                "/type",
            ),
            (
                "object message type",
                lambda envelope: envelope.update(type={}),
                "schema.enum",
                "/type",
            ),
            (
                "non-ASCII digest",
                lambda envelope: envelope.update(body_sha256="\u00e9" * 64),
                "schema.pattern",
                "/body_sha256",
            ),
        )
        for label, mutate, expected_code, expected_path in cases:
            with self.subTest(label=label):
                problems = self.problem_codes(changed("valid-hello.json", mutate))
                self.assertIn(
                    (expected_code, expected_path),
                    {(item.code, item.path) for item in problems},
                )

        invalid_status = changed(
            "valid-ack.json",
            lambda envelope: envelope["receipt"].update(status=[]),
        )
        status_problems = self.problem_codes(
            invalid_status,
            against_raw=fixture("valid-hello.json"),
        )
        self.assertIn("/receipt/status", {item.path for item in status_problems})

    def test_nesting_deeper_than_sixteen_is_rejected(self) -> None:
        raw = b'{"value":' + (b"[" * 16) + b"0" + (b"]" * 16) + b"}"
        problems = self.problem_codes(raw)
        self.assertEqual(problems[0].code, "wire.nesting_limit")

    def test_unpaired_surrogate_escape_is_rejected(self) -> None:
        problems = self.problem_codes(b'{"body":"\\ud800"}')
        self.assertEqual(problems[0].code, "wire.unicode_scalar")

    def test_surrogate_diagnostic_does_not_echo_member_name(self) -> None:
        canary = "PRIVATE_MEMBER_CANARY"
        raw = ('{"' + canary + '":"\\ud800"}').encode()
        problems = self.problem_codes(raw)
        rendered = json.dumps([item.as_dict() for item in problems])
        self.assertNotIn(canary, rendered)

    def test_byte_limit_is_enforced_before_parsing(self) -> None:
        raw = b" " * (cam1.MAX_ENVELOPE_BYTES + 1)
        problems = self.problem_codes(raw)
        self.assertEqual(problems[0].code, "wire.size_limit")

    def test_oversized_collection_short_circuits_schema_fanout(self) -> None:
        raw = changed(
            "valid-hello.json",
            lambda envelope: envelope.update(evidence=[""] * 100_000),
        )
        self.assertLess(len(raw), cam1.MAX_ENVELOPE_BYTES)
        problems = self.problem_codes(raw)
        self.assertIn("wire.collection_limit", {item.code for item in problems})
        self.assertLessEqual(len(problems), cam1.MAX_PROBLEMS)

    def test_body_hash_mismatch_is_rejected_without_body_echo(self) -> None:
        secret_shaped_body = "do-not-repeat-this-message"

        def mutate(envelope):
            envelope["body"] = secret_shaped_body

        raw = changed("valid-hello.json", mutate)
        problems = self.problem_codes(raw)
        self.assertIn("semantic.body_hash", {item.code for item in problems})
        self.assertNotIn(
            secret_shaped_body,
            json.dumps([item.as_dict() for item in problems]),
        )

    def test_receipt_ids_must_match(self) -> None:
        raw = changed(
            "valid-ack.json",
            lambda envelope: envelope["receipt"].update(
                for_message_id="00000000-0000-4000-8000-000000000099"
            ),
        )
        problems = self.problem_codes(raw)
        self.assertIn("semantic.receipt_correlation", {item.code for item in problems})

    def test_reply_correlation_failures_have_stable_diagnostics(self) -> None:
        alternate_id = "00000000-0000-4000-8000-000000000099"
        cases = (
            (
                "in_reply_to",
                lambda envelope: envelope.update(in_reply_to=alternate_id),
                "correlation.in_reply_to",
                "/in_reply_to",
            ),
            (
                "receipt",
                lambda envelope: envelope["receipt"].update(
                    for_message_id=alternate_id
                ),
                "correlation.receipt",
                "/receipt/for_message_id",
            ),
            (
                "recipient",
                lambda envelope: envelope["recipient"].update(
                    session_id="00000000-0000-4000-8000-000000000088"
                ),
                "correlation.recipient",
                "/recipient",
            ),
            (
                "recipient common name",
                lambda envelope: envelope["recipient"].update(
                    agent_name="different-recipient"
                ),
                "correlation.recipient",
                "/recipient",
            ),
            (
                "sender",
                lambda envelope: envelope["claimed_sender"].update(
                    session_id="00000000-0000-4000-8000-000000000087"
                ),
                "correlation.sender",
                "/claimed_sender",
            ),
            (
                "sender common name",
                lambda envelope: envelope["claimed_sender"].update(
                    agent_name="different-sender"
                ),
                "correlation.sender",
                "/claimed_sender",
            ),
            (
                "nonce",
                lambda envelope: (
                    envelope["receipt"].update(status="received"),
                    envelope.update(nonce="AQECAwQFBgcICQoLDA0ODw"),
                ),
                "correlation.nonce",
                "/nonce",
            ),
            (
                "interim nonce",
                lambda envelope: envelope.update(nonce="AAECAwQFBgcICQoLDA0ODw"),
                "correlation.interim_nonce",
                "/nonce",
            ),
        )
        for label, mutate, expected_code, expected_path in cases:
            with self.subTest(label=label):
                problems = self.problem_codes(
                    changed("valid-ack.json", mutate),
                    against_raw=fixture("valid-hello.json"),
                )
                self.assertIn(
                    (expected_code, expected_path),
                    {(item.code, item.path) for item in problems},
                )

    def test_invalid_calendar_time_is_rejected(self) -> None:
        raw = changed(
            "valid-hello.json",
            lambda envelope: envelope.update(expires_at="2026-02-30T20:10:00Z"),
        )
        problems = self.problem_codes(raw)
        self.assertIn("semantic.timestamp", {item.code for item in problems})

    def test_expired_message_fails_closed_but_can_be_inspected(self) -> None:
        later = dt.datetime(2026, 8, 21, 21, 0, tzinfo=dt.UTC)
        with self.assertRaises(cam1.CamValidationError):
            cam1.validate_exact_bytes(fixture("valid-hello.json"), now=later)
        result = cam1.validate_exact_bytes(
            fixture("valid-hello.json"),
            now=later,
            policy=cam1.ValidationPolicy(allow_expired=True),
        )
        self.assertFalse(result.fresh)

    def test_validation_policy_is_frozen_slotted_and_rejects_bad_limits(self) -> None:
        policy = cam1.ValidationPolicy()
        self.assertFalse(hasattr(policy, "__dict__"))
        with self.assertRaises((AttributeError, TypeError)):
            policy.max_ttl_seconds = 60  # type: ignore[misc]
        for keyword_args in (
            {"allow_expired": 1},
            {"max_ttl_seconds": 0},
            {"clock_skew_seconds": -1},
        ):
            with (
                self.subTest(keyword_args=keyword_args),
                self.assertRaises(cam1.CamUsageError),
            ):
                cam1.ValidationPolicy(**keyword_args)

    def test_far_future_message_is_rejected(self) -> None:
        raw = changed(
            "valid-hello.json",
            lambda envelope: envelope.update(
                sent_at="2026-08-21T21:00:00Z",
                expires_at="2026-08-21T21:10:00Z",
            ),
        )
        problems = self.problem_codes(raw)
        self.assertIn("semantic.future", {item.code for item in problems})

    def test_upper_bound_timestamps_return_validation_problems(self) -> None:
        def mutate(envelope):
            envelope.update(
                sent_at="9999-12-31T23:55:01Z",
                expires_at="9999-12-31T23:55:05Z",
            )
            envelope["authorization"].update(
                verified_at="9999-12-31T23:55:02Z",
                expires_at="9999-12-31T23:55:03Z",
            )

        problems = self.problem_codes(changed("valid-hello.json", mutate))
        self.assertIn("semantic.future", {item.code for item in problems})

    def test_extreme_clock_values_do_not_overflow_validation(self) -> None:
        latest = dt.datetime.max.replace(tzinfo=dt.UTC)
        result = cam1.validate_exact_bytes(
            fixture("valid-hello.json"),
            now=latest,
            policy=cam1.ValidationPolicy(
                allow_expired=True,
                clock_skew_seconds=10**30,
            ),
        )
        self.assertFalse(result.fresh)

    def test_excessive_ttl_is_rejected(self) -> None:
        raw = changed(
            "valid-hello.json",
            lambda envelope: envelope.update(expires_at="2026-08-21T22:00:00Z"),
        )
        problems = self.problem_codes(raw)
        self.assertIn("semantic.ttl", {item.code for item in problems})

    def test_codex_queue_callback_must_be_literal_uuid(self) -> None:
        raw = changed(
            "valid-hello.json",
            lambda envelope: envelope["reply_to"].update(address="$CODEX_THREAD_ID"),
        )
        problems = self.problem_codes(raw)
        callback = [
            item for item in problems if item.code == "semantic.callback_address"
        ]
        self.assertEqual(len(callback), 1)

    def test_reply_route_is_bound_to_the_claimed_sender_session(self) -> None:
        wrong_transport = changed(
            "valid-hello.json",
            lambda envelope: envelope["reply_to"].update(
                transport="claude_send_message"
            ),
        )
        wrong_identity = changed(
            "valid-ack.json",
            lambda envelope: envelope["reply_to"].update(
                address="00000000-0000-4000-8000-000000000199"
            ),
        )
        self.assertIn(
            "semantic.callback_transport",
            {item.code for item in self.problem_codes(wrong_transport)},
        )
        self.assertIn(
            "semantic.callback_identity",
            {item.code for item in self.problem_codes(wrong_identity)},
        )

    def test_challenge_requires_a_nonce(self) -> None:
        def mutate(envelope):
            envelope["type"] = "challenge"
            envelope["nonce"] = None

        problems = self.problem_codes(changed("valid-hello.json", mutate))
        self.assertIn("semantic.challenge_nonce", {item.code for item in problems})

    def test_challenge_has_exactly_three_legal_response_shapes(self) -> None:
        legal_replies = (
            ("direct verify", verify_envelope()),
            ("interim hold", fixture("valid-ack.json")),
            (
                "terminal rejection",
                changed(
                    "valid-ack.json",
                    lambda envelope: (
                        envelope["receipt"].update(status="rejected"),
                        envelope.update(nonce=None),
                    ),
                ),
            ),
        )
        for label, reply in legal_replies:
            with self.subTest(label=label):
                result = cam1.validate_exact_bytes(
                    reply,
                    against_raw=challenge_envelope(),
                    now=NOW,
                )
                self.assertTrue(result.correlated)

    def test_stateless_reply_transition_table_is_exact(self) -> None:
        expected = {
            ("hello", "ack", "received"),
            ("hello", "ack", "needs_human_confirmation"),
            ("hello", "ack", "rejected"),
            ("challenge", "ack", "needs_human_confirmation"),
            ("challenge", "ack", "rejected"),
            ("challenge", "verify", None),
            ("request", "ack", "received"),
            ("request", "ack", "needs_human_confirmation"),
            ("request", "ack", "accepted"),
            ("request", "ack", "rejected"),
            ("request", "status", "accepted"),
            ("request", "status", "started"),
            ("request", "result", "completed"),
            ("request", "error", "failed"),
            ("cancel", "ack", "received"),
            ("cancel", "ack", "accepted"),
            ("cancel", "ack", "rejected"),
            ("cancel", "status", "accepted"),
            ("cancel", "error", "failed"),
        }
        self.assertEqual(cam1.STATELESS_REPLY_TRANSITIONS, expected)

    def test_request_allows_later_reply_shapes_without_claiming_history(self) -> None:
        request = changed(
            "valid-hello.json",
            lambda envelope: envelope.update(type="request"),
        )
        candidates = (
            ("status", "accepted"),
            ("status", "started"),
            ("result", "completed"),
            ("error", "failed"),
        )
        for message_type, status_value in candidates:
            with self.subTest(message_type=message_type, status_value=status_value):
                reply = changed(
                    "valid-ack.json",
                    lambda envelope, message_type=message_type, status_value=status_value: (
                        envelope.update(type=message_type, nonce=None),
                        envelope["receipt"].update(status=status_value),
                    ),
                )
                result = cam1.validate_exact_bytes(
                    reply,
                    against_raw=request,
                    now=NOW,
                )
                self.assertTrue(result.correlated)

    def test_result_cannot_answer_hello(self) -> None:
        result_reply = changed(
            "valid-ack.json",
            lambda envelope: (
                envelope.update(type="result", nonce=None),
                envelope["receipt"].update(status="completed"),
            ),
        )
        problems = self.problem_codes(
            result_reply,
            against_raw=fixture("valid-hello.json"),
        )
        self.assertIn("correlation.transition", {item.code for item in problems})

    def test_error_can_answer_request_or_cancel_but_not_hello(self) -> None:
        error_reply = changed(
            "valid-ack.json",
            lambda envelope: (
                envelope.update(type="error", nonce=None),
                envelope["receipt"].update(status="failed"),
            ),
        )
        request = changed(
            "valid-hello.json",
            lambda envelope: envelope.update(type="request"),
        )
        cancel = changed(
            "valid-hello.json",
            lambda envelope: envelope.update(
                type="cancel",
                in_reply_to="00000000-0000-4000-8000-000000000099",
            ),
        )
        for original in (request, cancel):
            with self.subTest(original_type=json.loads(original)["type"]):
                validated = cam1.validate_exact_bytes(
                    error_reply,
                    against_raw=original,
                    now=NOW,
                )
                self.assertTrue(validated.correlated)

        problems = self.problem_codes(
            error_reply,
            against_raw=fixture("valid-hello.json"),
        )
        self.assertIn("correlation.transition", {item.code for item in problems})

    def test_reply_to_reply_requires_state_and_is_not_statelessly_correlated(
        self,
    ) -> None:
        problems = self.problem_codes(
            fixture("valid-ack.json"),
            against_raw=fixture("valid-ack.json"),
        )
        self.assertIn("correlation.transition", {item.code for item in problems})

    def test_reply_cannot_reuse_the_root_message_id(self) -> None:
        original = json.loads(fixture("valid-hello.json"))

        def reuse_root_id(envelope):
            envelope["message_id"] = original["message_id"]
            envelope["action"]["idempotency_key"] = original["message_id"]

        problems = self.problem_codes(
            changed("valid-ack.json", reuse_root_id),
            against_raw=fixture("valid-hello.json"),
        )
        self.assertIn(
            ("correlation.message_id", "/message_id"),
            {(item.code, item.path) for item in problems},
        )

    def test_uuid_case_alias_cannot_bypass_message_id_uniqueness(self) -> None:
        original = json.loads(fixture("valid-hello.json"))

        def reuse_uppercase_root_id(envelope):
            uppercase_root = original["message_id"].upper()
            envelope["message_id"] = uppercase_root
            envelope["action"]["idempotency_key"] = uppercase_root

        problems = self.problem_codes(
            changed("valid-ack.json", reuse_uppercase_root_id),
            against_raw=fixture("valid-hello.json"),
        )
        self.assertIn("correlation.message_id", {item.code for item in problems})

    def test_uuid_correlation_accepts_distinct_uppercase_wire_values(self) -> None:
        def use_uppercase_uuids(envelope):
            envelope["message_id"] = envelope["message_id"].upper()
            envelope["action"]["idempotency_key"] = envelope["message_id"]
            envelope["in_reply_to"] = envelope["in_reply_to"].upper()
            envelope["receipt"]["for_message_id"] = envelope["in_reply_to"]

        result = cam1.validate_exact_bytes(
            changed("valid-ack.json", use_uppercase_uuids),
            against_raw=fixture("valid-hello.json"),
            now=NOW,
        )
        self.assertTrue(result.correlated)

    def test_illegal_challenge_responses_are_rejected_by_transition(self) -> None:
        cases = (
            (
                "received acknowledgment",
                changed(
                    "valid-ack.json",
                    lambda envelope: (
                        envelope["receipt"].update(status="received"),
                        envelope.update(nonce="AAECAwQFBgcICQoLDA0ODw"),
                    ),
                ),
                "correlation.transition",
            ),
            (
                "accepted acknowledgment",
                changed(
                    "valid-ack.json",
                    lambda envelope: (
                        envelope["receipt"].update(status="accepted"),
                        envelope.update(nonce="AAECAwQFBgcICQoLDA0ODw"),
                    ),
                ),
                "correlation.transition",
            ),
            (
                "rejection reusing nonce",
                changed(
                    "valid-ack.json",
                    lambda envelope: (
                        envelope["receipt"].update(status="rejected"),
                        envelope.update(nonce="AAECAwQFBgcICQoLDA0ODw"),
                    ),
                ),
                "correlation.challenge_ack_nonce",
            ),
            (
                "reverse challenge",
                changed(
                    "valid-ack.json",
                    lambda envelope: envelope.update(
                        type="challenge",
                        receipt=None,
                    ),
                ),
                "correlation.transition",
            ),
        )
        for label, reply, expected_code in cases:
            with self.subTest(label=label):
                problems = self.problem_codes(
                    reply,
                    against_raw=challenge_envelope(),
                )
                self.assertIn(expected_code, {item.code for item in problems})

    def test_verify_can_answer_only_the_preserved_challenge(self) -> None:
        wrong_nonce = changed(
            "valid-ack.json",
            lambda envelope: envelope.update(
                type="verify",
                receipt=None,
                nonce="AQECAwQFBgcICQoLDA0ODw",
            ),
        )
        nonce_problems = self.problem_codes(
            wrong_nonce,
            against_raw=challenge_envelope(),
        )
        self.assertIn("correlation.nonce", {item.code for item in nonce_problems})

        transition_problems = self.problem_codes(
            verify_envelope(),
            against_raw=fixture("valid-hello.json"),
        )
        self.assertIn(
            "correlation.transition",
            {item.code for item in transition_problems},
        )

    def test_reply_cannot_invent_nonce_for_nonce_less_original(self) -> None:
        original = changed(
            "valid-hello.json", lambda envelope: envelope.update(nonce=None)
        )
        reply = changed(
            "valid-ack.json",
            lambda envelope: (
                envelope["receipt"].update(status="received"),
                envelope.update(nonce="AAECAwQFBgcICQoLDA0ODw"),
            ),
        )
        problems = self.problem_codes(reply, against_raw=original)
        self.assertIn("correlation.nonce", {item.code for item in problems})

    def test_verify_requires_nonce_even_for_nonce_less_original(self) -> None:
        original = changed(
            "valid-hello.json", lambda envelope: envelope.update(nonce=None)
        )

        def make_verify(envelope):
            envelope["type"] = "verify"
            envelope["receipt"] = None
            envelope["nonce"] = None

        reply = changed("valid-ack.json", make_verify)
        problems = self.problem_codes(reply, against_raw=original)
        self.assertIn("semantic.verify_nonce", {item.code for item in problems})

    def test_authorization_may_expire_before_message_expiry(self) -> None:
        def mutate(envelope):
            envelope["authorization"] = {
                "basis": "operator_confirmation",
                "authority": "example operator",
                "reference": "example decision",
                "verified_at": "2026-08-21T19:59:00Z",
                "expires_at": "2026-08-21T20:06:00Z",
            }

        cam1.validate_exact_bytes(changed("valid-hello.json", mutate), now=NOW)

        def expire_with_message(envelope):
            mutate(envelope)
            envelope["authorization"]["expires_at"] = envelope["expires_at"]

        cam1.validate_exact_bytes(
            changed("valid-hello.json", expire_with_message), now=NOW
        )

    def test_authorization_must_not_outlive_message(self) -> None:
        def mutate(envelope):
            envelope["authorization"] = {
                "basis": "operator_confirmation",
                "authority": "example operator",
                "reference": "example decision",
                "verified_at": "2026-08-21T19:59:00Z",
                "expires_at": "2026-08-21T20:11:00Z",
            }

        problems = self.problem_codes(changed("valid-hello.json", mutate))
        self.assertIn(
            "semantic.authorization_exceeds_message",
            {item.code for item in problems},
        )

        def omit_verification(envelope):
            envelope["authorization"] = {
                "basis": "none",
                "authority": None,
                "reference": None,
                "verified_at": None,
                "expires_at": "2026-08-21T20:11:00Z",
            }

        problems = self.problem_codes(changed("valid-hello.json", omit_verification))
        self.assertIn(
            "semantic.authorization_exceeds_message",
            {item.code for item in problems},
        )

    def test_malformed_uuid_diagnostic_is_bounded(self) -> None:
        malformed = "-" * 200_000
        raw = changed(
            "valid-hello.json",
            lambda envelope: envelope["action"].update(idempotency_key=malformed),
        )
        problems = self.problem_codes(raw)
        self.assertTrue(all(len(item.detail) <= 240 for item in problems))
        self.assertTrue(all(len(item.path) <= 256 for item in problems))
        self.assertNotIn("sha256", json.dumps([item.as_dict() for item in problems]))


class CamBuilderTests(unittest.TestCase):
    def build_request(self) -> bytes:
        return cam1.build_hello(
            sender_vendor="codex",
            sender_name="example coordinator",
            sender_session="00000000-0000-4000-8000-000000000101",
            recipient_vendor="claude-code",
            recipient_name="example worker",
            recipient_session="00000000-0000-4000-8000-000000000102",
            reply_transport="codex_queue",
            reply_address="00000000-0000-4000-8000-000000000101",
            now=NOW,
        )

    def test_hello_builder_preserves_exact_callback_and_serialization(self) -> None:
        raw = self.build_request()
        self.assertFalse(raw.endswith(b"\n"))
        envelope = json.loads(raw)
        self.assertEqual(
            envelope["claimed_sender"]["session_id"],
            "00000000-0000-4000-8000-000000000101",
        )
        self.assertEqual(
            envelope["reply_to"]["address"],
            "00000000-0000-4000-8000-000000000101",
        )
        self.assertEqual(cam1.serialize_envelope(envelope), raw)
        cam1.validate_exact_bytes(raw, now=NOW)

    def test_default_ack_is_complete_fail_closed_and_correlated(self) -> None:
        request = self.build_request()
        raw = cam1.build_ack(
            request,
            sender_vendor="claude-code",
            sender_name="example worker",
            sender_session="00000000-0000-4000-8000-000000000102",
            reply_transport="claude_send_message",
            reply_address="00000000-0000-4000-8000-000000000102",
            now=NOW + dt.timedelta(seconds=30),
        )
        envelope = json.loads(raw)
        request_id = json.loads(request)["message_id"]
        self.assertEqual(envelope["receipt"]["status"], "needs_human_confirmation")
        self.assertIsNone(envelope["nonce"])
        self.assertEqual(envelope["in_reply_to"], request_id)
        self.assertEqual(envelope["receipt"]["for_message_id"], request_id)
        result = cam1.validate_exact_bytes(
            raw,
            against_raw=request,
            now=NOW + dt.timedelta(seconds=30),
        )
        self.assertTrue(result.correlated)

    def test_received_ack_echoes_original_nonce(self) -> None:
        request = self.build_request()
        raw = cam1.build_ack(
            request,
            sender_vendor="claude-code",
            sender_name="example worker",
            sender_session="00000000-0000-4000-8000-000000000102",
            reply_transport="claude_send_message",
            reply_address="00000000-0000-4000-8000-000000000102",
            status_value="received",
            now=NOW + dt.timedelta(seconds=30),
        )
        self.assertEqual(json.loads(raw)["nonce"], json.loads(request)["nonce"])
        cam1.validate_exact_bytes(
            raw,
            against_raw=request,
            now=NOW + dt.timedelta(seconds=30),
        )

    def test_challenge_ack_builder_holds_or_rejects_without_nonce(self) -> None:
        common = {
            "sender_vendor": "claude-code",
            "sender_name": "example worker",
            "sender_session": "00000000-0000-4000-8000-000000000102",
            "reply_transport": "claude_send_message",
            "reply_address": "00000000-0000-4000-8000-000000000102",
            "now": NOW + dt.timedelta(seconds=30),
        }
        for status_value in ("needs_human_confirmation", "rejected"):
            with self.subTest(status_value=status_value):
                raw = cam1.build_ack(
                    challenge_envelope(),
                    status_value=status_value,
                    **common,
                )
                self.assertIsNone(json.loads(raw)["nonce"])
                result = cam1.validate_exact_bytes(
                    raw,
                    against_raw=challenge_envelope(),
                    now=NOW + dt.timedelta(seconds=30),
                )
                self.assertTrue(result.correlated)

        for status_value in ("received", "accepted"):
            with (
                self.subTest(status_value=status_value),
                self.assertRaises(cam1.CamUsageError),
            ):
                cam1.build_ack(
                    challenge_envelope(),
                    status_value=status_value,
                    **common,
                )

    def test_ack_builder_does_not_invent_missing_sender_identity(self) -> None:
        with self.assertRaises(cam1.CamValidationError):
            cam1.build_ack(
                self.build_request(),
                sender_vendor="claude-code",
                sender_name="example worker",
                sender_session="",
                reply_transport="claude_send_message",
                reply_address="00000000-0000-4000-8000-000000000102",
                now=NOW + dt.timedelta(seconds=30),
            )

    def test_all_public_time_inputs_reject_naive_datetimes(self) -> None:
        naive = NOW.replace(tzinfo=None)
        cases = (
            lambda: cam1.validate_exact_bytes(
                fixture("valid-hello.json"),
                now=naive,
            ),
            lambda: cam1.build_hello(
                sender_vendor="codex",
                sender_name="example coordinator",
                sender_session="00000000-0000-4000-8000-000000000101",
                recipient_vendor="claude-code",
                recipient_name="example worker",
                recipient_session=None,
                reply_transport="codex_queue",
                reply_address="00000000-0000-4000-8000-000000000101",
                now=naive,
            ),
            lambda: cam1.build_ack(
                self.build_request(),
                sender_vendor="claude-code",
                sender_name="example worker",
                sender_session="00000000-0000-4000-8000-000000000102",
                reply_transport="claude_send_message",
                reply_address="00000000-0000-4000-8000-000000000102",
                now=naive,
            ),
        )
        for operation in cases:
            with self.subTest(operation=operation):
                with self.assertRaises(cam1.CamUsageError) as context:
                    operation()
                self.assertEqual(context.exception.code, "argument.now")

    def test_output_file_is_private_and_never_overwritten(self) -> None:
        raw = self.build_request()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "message.json"
            cam1._write_output(raw, str(output))
            self.assertEqual(output.read_bytes(), raw)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            with self.assertRaises(cam1.CliError):
                cam1._write_output(raw, str(output))

    def test_input_symlink_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "target.json"
            target.write_bytes(self.build_request())
            link = Path(directory) / "message.json"
            link.symlink_to(target)
            with self.assertRaises(cam1.CliError):
                cam1.read_envelope_file(str(link))

    def test_input_and_output_refuse_symlinked_ancestor_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            real = root / "real"
            real.mkdir(mode=0o700)
            source = real / "message.json"
            source.write_bytes(self.build_request())
            link = root / "redirected"
            link.symlink_to(real, target_is_directory=True)

            with self.assertRaises(cam1.CliError):
                cam1.read_envelope_file(str(link / "message.json"))
            with self.assertRaises(cam1.CliError):
                cam1._write_output(self.build_request(), str(link / "output.json"))
            self.assertFalse((real / "output.json").exists())

    def test_input_and_output_reject_parent_reference_that_erases_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            real = root / "real"
            real.mkdir(mode=0o700)
            link = root / "redirected"
            link.symlink_to(real, target_is_directory=True)
            safe = root / "safe"
            safe.mkdir(mode=0o700)
            source = safe / "message.json"
            source.write_bytes(self.build_request())
            disguised_source = link / ".." / safe.name / source.name
            disguised_output = link / ".." / safe.name / "output.json"

            with self.assertRaises(cam1.CliError):
                cam1.read_envelope_file(str(disguised_source))
            with self.assertRaises(cam1.CliError):
                cam1._write_output(self.build_request(), str(disguised_output))
            self.assertFalse(safe.joinpath("output.json").exists())

    def test_interrupted_output_write_never_publishes_partial_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            output = parent / "message.json"
            with (
                mock.patch("tools.cam1lib.cli.os.fsync", side_effect=OSError),
                self.assertRaises(cam1.CliError),
            ):
                cam1._write_output(self.build_request(), str(output))
            self.assertFalse(output.exists())
            self.assertEqual(list(parent.glob(".cam1-*.tmp")), [])


class CamTypedBuilderTests(unittest.TestCase):
    coordinator: ClassVar[dict[str, str]] = {
        "sender_vendor": "codex",
        "sender_name": "example coordinator",
        "sender_session": "00000000-0000-4000-8000-000000000101",
        "reply_transport": "codex_queue",
        "reply_address": "00000000-0000-4000-8000-000000000101",
    }
    worker_reply: ClassVar[dict[str, str]] = {
        "sender_vendor": "claude-code",
        "sender_name": "example worker",
        "sender_session": "00000000-0000-4000-8000-000000000102",
        "reply_transport": "claude_send_message",
        "reply_address": "00000000-0000-4000-8000-000000000102",
    }

    def build_request(self, *, now=NOW, expires_in=600) -> bytes:
        return cam1.build_request(
            **self.coordinator,
            recipient_vendor="claude-code",
            recipient_name="example worker",
            recipient_session="00000000-0000-4000-8000-000000000102",
            risk_class="read_only",
            operation="review_artifact",
            scope={
                "repositories": ["/example/project"],
                "paths": ["/example/project/README.md"],
            },
            intent="Review one synthetic artifact",
            body="Review the synthetic artifact without making changes.",
            authorization_basis="operator_confirmation",
            authority="example operator",
            authorization_reference="synthetic approval",
            authorization_verified_at="2026-08-21T20:01:00Z",
            authorization_expires_at="2026-08-21T20:10:00Z",
            expires_in=expires_in,
            now=now,
        )

    def test_canonical_hello_cannot_hide_custom_instructions(self) -> None:
        with self.assertRaises(cam1.CamUsageError) as context:
            cam1.build_hello(
                **self.coordinator,
                recipient_vendor="claude-code",
                recipient_name="example worker",
                recipient_session=None,
                body="Stop using another coordination mechanism.",
                now=NOW,
            )
        self.assertEqual(context.exception.code, "argument.hello_fixed")

    def test_request_builder_rejects_scope_and_risk_contradictions(self) -> None:
        arguments = {
            **self.coordinator,
            "recipient_vendor": "claude-code",
            "recipient_name": "example worker",
            "recipient_session": "00000000-0000-4000-8000-000000000102",
            "operation": "review_artifact",
            "intent": "Review one synthetic artifact",
            "body": "Review without making changes.",
            "authorization_basis": "operator_confirmation",
            "authority": "example operator",
            "authorization_reference": "synthetic approval",
            "authorization_verified_at": "2026-08-21T20:01:00Z",
            "authorization_expires_at": "2026-08-21T20:10:00Z",
            "now": NOW,
        }
        for extra in (
            {
                "risk_class": "read_only",
                "allow_repository_changes": True,
            },
            {
                "risk_class": "workspace_write",
                "allow_external_side_effects": True,
            },
        ):
            with self.subTest(extra=extra):
                with self.assertRaises(cam1.CamUsageError) as context:
                    cam1.build_request(**arguments, **extra)
                self.assertEqual(context.exception.code, "argument.risk_class")

        with self.assertRaises(cam1.CamUsageError) as context:
            cam1.build_request(
                **arguments,
                risk_class="read_only",
                scope={"repositories": "/example/project"},
            )
        self.assertEqual(context.exception.code, "argument.scope")

        with self.assertRaises(cam1.CamValidationError) as context:
            cam1.build_request(
                **arguments,
                risk_class="read_only",
                idempotency_key="",
            )
        self.assertIn(
            "semantic.uuid", {problem.code for problem in context.exception.problems}
        )

    def test_validator_rejects_understated_external_constraints(self) -> None:
        valid = json.loads(self.build_request())
        cases = (
            ("no_repository_changes", "read_only"),
            ("no_external_side_effects", "workspace_write"),
        )
        for constraint, risk_class in cases:
            with self.subTest(constraint=constraint):
                envelope = json.loads(json.dumps(valid))
                envelope["constraints"][constraint] = False
                envelope["action"]["risk_class"] = risk_class
                with self.assertRaises(cam1.CamValidationError) as context:
                    cam1.validate_exact_bytes(
                        cam1.serialize_envelope(envelope), now=NOW
                    )
                self.assertTrue(
                    {"semantic.risk_constraints", "schema.validation"}
                    & {problem.code for problem in context.exception.problems}
                )

    def test_typed_request_and_result_fixtures_validate_and_correlate(self) -> None:
        request = fixture("valid-request.json")
        result = fixture("valid-result.json")
        self.assertEqual(
            cam1.validate_exact_bytes(request, now=NOW).envelope["type"], "request"
        )
        self.assertTrue(
            cam1.validate_exact_bytes(
                result,
                against_raw=request,
                now=NOW,
            ).correlated
        )

    def test_challenge_and_verify_builders_complete_one_leg(self) -> None:
        challenge = cam1.build_challenge(
            **self.coordinator,
            recipient_vendor="claude-code",
            recipient_name="example worker",
            recipient_session="00000000-0000-4000-8000-000000000102",
            now=NOW,
        )
        verify = cam1.build_verify(
            challenge,
            **self.worker_reply,
            now=NOW + dt.timedelta(seconds=30),
        )
        challenge_value = json.loads(challenge)
        verify_value = json.loads(verify)
        self.assertEqual(challenge_value["type"], "challenge")
        self.assertEqual(verify_value["type"], "verify")
        self.assertEqual(verify_value["nonce"], challenge_value["nonce"])
        self.assertTrue(
            cam1.validate_exact_bytes(
                verify,
                against_raw=challenge,
                now=NOW + dt.timedelta(seconds=30),
            ).correlated
        )

    def test_request_and_all_typed_lifecycle_builders_correlate(self) -> None:
        request = self.build_request()
        observed = NOW + dt.timedelta(seconds=30)
        replies = (
            cam1.build_ack(
                request, **self.worker_reply, status_value="accepted", now=observed
            ),
            cam1.build_status(
                request,
                **self.worker_reply,
                status_value="started",
                body="Synthetic review started.",
                now=observed,
            ),
            cam1.build_result(
                request,
                **self.worker_reply,
                body="Review completed; no changes were made.",
                now=observed,
            ),
            cam1.build_error(
                request,
                **self.worker_reply,
                body="Review failed without changing files.",
                now=observed,
            ),
        )
        self.assertEqual(
            [json.loads(reply)["type"] for reply in replies],
            ["ack", "status", "result", "error"],
        )
        for reply in replies:
            self.assertTrue(
                cam1.validate_exact_bytes(
                    reply, against_raw=request, now=observed
                ).correlated
            )

    def test_result_builder_rejects_second_result_when_history_is_supplied(
        self,
    ) -> None:
        request = self.build_request()
        first = cam1.build_result(
            request,
            **self.worker_reply,
            body="First and only result.",
            now=NOW + dt.timedelta(seconds=30),
        )
        with self.assertRaises(cam1.CamUsageError) as context:
            cam1.build_result(
                request,
                **self.worker_reply,
                body="Duplicate result.",
                previous_responses=(first,),
                now=NOW + dt.timedelta(seconds=40),
            )
        self.assertEqual(context.exception.code, "lifecycle.result_exists")

    def test_cancel_requires_original_sender_and_fresh_authority(self) -> None:
        request = self.build_request()
        cancel = cam1.build_cancel(
            request,
            **self.coordinator,
            authority="example operator",
            authorization_reference="cancel approval",
            authorization_verified_at="2026-08-21T20:04:00Z",
            authorization_expires_at="2026-08-21T20:09:00Z",
            now=NOW,
        )
        value = json.loads(cancel)
        self.assertEqual(value["type"], "cancel")
        self.assertEqual(value["in_reply_to"], json.loads(request)["message_id"])
        cam1.validate_exact_bytes(cancel, now=NOW)

        with self.assertRaises(cam1.CamUsageError) as context:
            cam1.build_cancel(
                request,
                **self.worker_reply,
                authority="example operator",
                authorization_reference="cancel approval",
                authorization_verified_at="2026-08-21T20:04:00Z",
                authorization_expires_at="2026-08-21T20:09:00Z",
                now=NOW,
            )
        self.assertEqual(context.exception.code, "argument.sender")

    def test_status_inquiry_is_a_new_informational_request(self) -> None:
        original = self.build_request()
        inquiry = cam1.build_status_inquiry(original, now=NOW)
        original_value = json.loads(original)
        inquiry_value = json.loads(inquiry)
        self.assertEqual(inquiry_value["type"], "request")
        self.assertEqual(inquiry_value["action"]["operation"], "inquire_status")
        self.assertEqual(inquiry_value["authorization"]["basis"], "none")
        self.assertNotEqual(inquiry_value["message_id"], original_value["message_id"])
        self.assertIn(original_value["message_id"], inquiry_value["body"])

    def test_follow_up_helpers_reject_one_way_request_routes(self) -> None:
        value = json.loads(fixture("valid-request.json"))
        value["reply_to"] = None
        one_way = cam1.serialize_envelope(value)
        with self.assertRaises(cam1.CamUsageError) as inquiry:
            cam1.build_status_inquiry(one_way, now=NOW)
        self.assertEqual(inquiry.exception.code, "argument.reply_to")

        value["sent_at"] = "2026-08-21T19:00:00Z"
        value["expires_at"] = "2026-08-21T19:01:00Z"
        value["authorization"]["verified_at"] = "2026-08-21T18:59:00Z"
        value["authorization"]["expires_at"] = "2026-08-21T19:01:00Z"
        expired_one_way = cam1.serialize_envelope(value)
        with self.assertRaises(cam1.CamUsageError) as renewal:
            cam1.renew_request(
                expired_one_way,
                authorization_basis="operator_confirmation",
                authority="example operator",
                authorization_reference="synthetic renewal approval",
                authorization_verified_at="2026-08-21T20:04:00Z",
                authorization_expires_at="2026-08-21T20:10:00Z",
                confirm_no_known_pending=True,
                now=NOW,
            )
        self.assertEqual(renewal.exception.code, "argument.reply_to")

    def test_expired_request_renewal_preserves_only_semantic_idempotency(self) -> None:
        expired_at = NOW - dt.timedelta(minutes=20)
        expired = cam1.build_request(
            **self.coordinator,
            recipient_vendor="claude-code",
            recipient_name="example worker",
            recipient_session="00000000-0000-4000-8000-000000000102",
            risk_class="informational",
            operation="review_feedback",
            intent="Review feedback",
            body="Review this synthetic feedback.",
            authorization_basis="none",
            expires_in=60,
            now=expired_at,
        )
        with self.assertRaises(cam1.CamUsageError):
            cam1.renew_request(
                expired,
                authorization_basis="none",
                confirm_no_known_pending=False,
                now=NOW,
            )
        renewed = cam1.renew_request(
            expired,
            authorization_basis="none",
            confirm_no_known_pending=True,
            now=NOW,
        )
        old = json.loads(expired)
        new = json.loads(renewed)
        self.assertEqual(
            old["action"]["idempotency_key"], new["action"]["idempotency_key"]
        )
        self.assertNotEqual(old["message_id"], new["message_id"])
        self.assertNotEqual(old["nonce"], new["nonce"])
        self.assertEqual(old["body"], new["body"])

    def test_expired_root_replies_need_stateful_lifecycle_interpretation(self) -> None:
        root_time = NOW - dt.timedelta(minutes=20)
        root = cam1.build_request(
            **self.coordinator,
            recipient_vendor="claude-code",
            recipient_name="example worker",
            recipient_session="00000000-0000-4000-8000-000000000102",
            risk_class="informational",
            operation="review_feedback",
            intent="Review feedback",
            body="Review this synthetic feedback.",
            authorization_basis="none",
            expires_in=60,
            now=root_time,
        )
        rejection = cam1.build_late_rejection(root, **self.worker_reply, now=NOW)
        self.assertTrue(
            cam1.validate_exact_bytes(rejection, against_raw=root, now=NOW).correlated
        )

        late_result = json.loads(fixture("valid-result.json"))
        root_value = json.loads(root)
        late_result["in_reply_to"] = root_value["message_id"]
        late_result["receipt"]["for_message_id"] = root_value["message_id"]
        late_result["recipient"] = root_value["claimed_sender"]
        late_result["claimed_sender"] = {
            **root_value["recipient"],
            "host_id": root_value["recipient"].get("host_id"),
        }
        late_result["sent_at"] = "2026-08-21T20:05:00Z"
        late_result["expires_at"] = "2026-08-21T20:15:00Z"
        validated = cam1.validate_exact_bytes(
            cam1.serialize_envelope(late_result), against_raw=root, now=NOW
        )
        self.assertTrue(validated.correlated)

    def test_reply_cannot_predate_its_root(self) -> None:
        request = self.build_request(now=NOW)
        reply = cam1.build_result(
            request,
            **self.worker_reply,
            body="Synthetic result.",
            now=NOW + dt.timedelta(seconds=1),
        )
        value = json.loads(reply)
        value["sent_at"] = "2026-08-21T19:59:59Z"
        value["expires_at"] = "2026-08-21T20:09:59Z"
        problems = self._problems(cam1.serialize_envelope(value), against_raw=request)
        self.assertIn("correlation.reply_before_root", {item.code for item in problems})

    def _problems(self, raw: bytes, **kwargs) -> tuple[cam1.Problem, ...]:
        with self.assertRaises(cam1.CamValidationError) as context:
            cam1.validate_exact_bytes(raw, now=NOW, **kwargs)
        return context.exception.problems

    def test_incident_wrappers_fail_closed(self) -> None:
        cases = {
            "resolved": lambda value: value["receipt"].update(status="resolved"),
            "incomplete": lambda value: value["receipt"].update(status="incomplete"),
            "also_answers": lambda value: value.update(also_answers=[]),
            "missing_action": lambda value: value.pop("action"),
            "over_ttl": lambda value: value.update(expires_at="2026-08-21T22:04:00Z"),
        }
        for label, mutate in cases.items():
            with self.subTest(label=label):
                value = json.loads(fixture("valid-result.json"))
                mutate(value)
                problems = self._problems(
                    cam1.serialize_envelope(value),
                    against_raw=fixture("valid-request.json"),
                )
                codes = {item.code for item in problems}
                if label in {"resolved", "incomplete"}:
                    self.assertIn("schema.enum", codes)
                elif label == "also_answers":
                    self.assertIn("schema.additionalProperties", codes)
                elif label == "missing_action":
                    self.assertIn("schema.required", codes)
                else:
                    self.assertIn("semantic.ttl", codes)

    def test_malformed_receipt_still_reports_impossible_nonce(self) -> None:
        value = json.loads(fixture("valid-ack.json"))
        value["receipt"]["status"] = "resolved"
        value["nonce"] = "AQECAwQFBgcICQoLDA0ODw"
        problems = self._problems(
            cam1.serialize_envelope(value),
            against_raw=fixture("valid-hello.json"),
        )
        codes = {item.code for item in problems}
        self.assertIn("schema.enum", codes)
        self.assertIn("correlation.nonce_impossible", codes)

    def test_authorization_cannot_expire_before_send(self) -> None:
        value = json.loads(fixture("valid-request.json"))
        value["authorization"]["expires_at"] = "2026-08-21T20:02:00Z"
        problems = self._problems(cam1.serialize_envelope(value))
        self.assertIn(
            "semantic.authorization_expired_before_send",
            {item.code for item in problems},
        )

    def test_request_builder_refuses_authorization_beyond_message_expiry(self) -> None:
        with self.assertRaises(cam1.CamValidationError) as context:
            self.build_request(expires_in=60)
        self.assertIn(
            "semantic.authorization_exceeds_message",
            {item.code for item in context.exception.problems},
        )


class CamPublicSurfaceTests(unittest.TestCase):
    def test_protocol_examples_equal_checked_fixtures(self) -> None:
        protocol = (ROOT / "PROTOCOL.md").read_text(encoding="utf-8")
        section = protocol.split("## 20. Minimal first-contact example", 1)[1]
        fence = chr(96) * 3
        pattern = re.escape(fence + "json\n") + r"(\{.*?\})\n" + re.escape(fence)
        blocks = re.findall(pattern, section, flags=re.DOTALL)
        self.assertGreaterEqual(len(blocks), 2)
        self.assertEqual(json.loads(blocks[0]), json.loads(fixture("valid-hello.json")))
        self.assertEqual(json.loads(blocks[1]), json.loads(fixture("valid-ack.json")))
        for block in blocks[:2]:
            envelope = json.loads(block)
            self.assertEqual(
                envelope["body_sha256"],
                hashlib.sha256(envelope["body"].encode("utf-8")).hexdigest(),
            )

    def test_reference_tool_imports_no_transport_modules(self) -> None:
        tree = ast.parse((ROOT / "tools" / "cam1.py").read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".", 1)[0])
        forbidden = {
            "http",
            "requests",
            "socket",
            "subprocess",
            "urllib",
            "webbrowser",
        }
        self.assertFalse(imported & forbidden)

    def test_public_reply_types_include_verify_for_against_enforcement(self) -> None:
        self.assertEqual(
            cam1.REPLY_TYPES,
            {"ack", "status", "result", "error", "verify"},
        )

    def test_invalid_cli_input_emits_no_stdout_envelope(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "tools" / "cam1.py"),
                "validate",
                str(FIXTURES / "abbreviated-ack.json"),
                "--allow-expired",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(completed.stdout, b"")
        error = json.loads(completed.stderr)
        self.assertFalse(error["valid"])
        self.assertTrue(error["validation_profile"]["available"])
        self.assertRegex(
            error["validation_profile"]["validation_profile_sha256"],
            r"\A[0-9a-f]{64}\Z",
        )

    def test_semantically_invalid_cli_input_returns_nonzero(self) -> None:
        raw = changed(
            "valid-hello.json",
            lambda envelope: envelope["reply_to"].update(
                address="00000000-0000-4000-8000-000000000199"
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wrong-callback.json"
            path.write_bytes(raw)
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "tools" / "cam1.py"),
                    "validate",
                    str(path),
                    "--allow-expired",
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
            )

        self.assertEqual(completed.returncode, 2)
        self.assertEqual(completed.stdout, b"")
        error = json.loads(completed.stderr)
        self.assertFalse(error["valid"])
        self.assertIn(
            "semantic.callback_identity",
            {problem["code"] for problem in error["problems"]},
        )

    def test_valid_cli_summary_is_bounded_and_not_a_trust_claim(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "tools" / "cam1.py"),
                "validate",
                str(FIXTURES / "valid-hello.json"),
                "--allow-expired",
            ],
            cwd=ROOT,
            check=True,
            capture_output=True,
        )
        summary = json.loads(completed.stdout)
        self.assertTrue(summary["structurally_valid"])
        self.assertTrue(summary["validation_profile"]["available"])
        self.assertIn("runtime", summary["validation_profile"])
        self.assertNotIn("trusted", summary)
        self.assertNotIn("authorized", summary)
        self.assertNotIn("safe", summary)
        self.assertEqual(completed.stderr, b"")

    def test_validation_profile_command_reports_source_state(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "tools" / "cam1.py"),
                "validation-profile",
            ],
            cwd=ROOT,
            check=True,
            capture_output=True,
        )
        result = json.loads(completed.stdout)
        self.assertTrue(result["available"])
        self.assertEqual(result["format"], "CAM-VALIDATION-PROFILE/1")
        self.assertIn(result["source_control"]["kind"], {"git", "not_git"})
        self.assertEqual(completed.stderr, b"")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires POSIX FIFO support")
    def test_fifo_input_is_rejected_without_waiting_for_a_writer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fifo = Path(directory) / "message.fifo"
            os.mkfifo(fifo)
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "tools" / "cam1.py"),
                    "validate",
                    str(fifo),
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                timeout=2,
            )
        self.assertEqual(completed.returncode, 2)
        error = json.loads(completed.stderr)
        self.assertEqual(error["problems"][0]["code"], "input.type")

    def test_offline_validation_still_accepts_stdin(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "tools" / "cam1.py"),
                "validate",
                "-",
                "--allow-expired",
            ],
            cwd=ROOT,
            check=False,
            input=fixture("valid-hello.json"),
            capture_output=True,
            timeout=2,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(json.loads(completed.stdout)["structurally_valid"])

    def test_cli_build_validate_ack_validate_round_trip(self) -> None:
        tool = str(ROOT / "tools" / "cam1.py")
        coordinator_session = "00000000-0000-4000-8000-000000000201"
        worker_session = "00000000-0000-4000-8000-000000000202"
        with tempfile.TemporaryDirectory() as directory:
            hello_path = Path(directory) / "hello.json"
            ack_path = Path(directory) / "ack.json"
            build_hello = subprocess.run(
                [
                    sys.executable,
                    tool,
                    "build-hello",
                    "--sender-vendor",
                    "codex",
                    "--sender-name",
                    "cli coordinator",
                    "--sender-session",
                    coordinator_session,
                    "--recipient-vendor",
                    "claude-code",
                    "--recipient-name",
                    "cli worker",
                    "--recipient-session",
                    worker_session,
                    "--reply-transport",
                    "codex_queue",
                    "--reply-address",
                    coordinator_session,
                    "--output",
                    str(hello_path),
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
            )
            self.assertEqual(build_hello.returncode, 0, build_hello.stderr)
            self.assertEqual(build_hello.stdout, b"")
            self.assertEqual(stat.S_IMODE(hello_path.stat().st_mode), 0o600)

            validate_hello = subprocess.run(
                [sys.executable, tool, "validate", str(hello_path)],
                cwd=ROOT,
                check=False,
                capture_output=True,
            )
            self.assertEqual(validate_hello.returncode, 0, validate_hello.stderr)
            self.assertTrue(json.loads(validate_hello.stdout)["structurally_valid"])

            build_ack = subprocess.run(
                [
                    sys.executable,
                    tool,
                    "build-ack",
                    "--request",
                    str(hello_path),
                    "--sender-vendor",
                    "claude-code",
                    "--sender-name",
                    "cli worker",
                    "--sender-session",
                    worker_session,
                    "--reply-transport",
                    "claude_send_message",
                    "--reply-address",
                    worker_session,
                    "--output",
                    str(ack_path),
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
            )
            self.assertEqual(build_ack.returncode, 0, build_ack.stderr)
            self.assertEqual(build_ack.stdout, b"")
            self.assertEqual(stat.S_IMODE(ack_path.stat().st_mode), 0o600)

            validate_ack = subprocess.run(
                [
                    sys.executable,
                    tool,
                    "validate",
                    str(ack_path),
                    "--against",
                    str(hello_path),
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
            )
            self.assertEqual(validate_ack.returncode, 0, validate_ack.stderr)
            self.assertTrue(json.loads(validate_ack.stdout)["correlated"])

    def test_cli_build_request_and_result_without_hand_authored_json(self) -> None:
        tool = str(ROOT / "tools" / "cam1.py")
        coordinator_session = "00000000-0000-4000-8000-000000000201"
        worker_session = "00000000-0000-4000-8000-000000000202"
        observed = dt.datetime.now(dt.UTC).replace(microsecond=0)
        verified_at = (
            (observed - dt.timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
        )
        authorization_expires = (
            (observed + dt.timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
        )
        with tempfile.TemporaryDirectory() as directory:
            request_path = Path(directory) / "request.json"
            result_path = Path(directory) / "result.json"
            request = subprocess.run(
                [
                    sys.executable,
                    tool,
                    "build-request",
                    "--sender-vendor",
                    "codex",
                    "--sender-name",
                    "cli coordinator",
                    "--sender-session",
                    coordinator_session,
                    "--recipient-vendor",
                    "claude-code",
                    "--recipient-name",
                    "cli worker",
                    "--recipient-session",
                    worker_session,
                    "--reply-transport",
                    "codex_queue",
                    "--reply-address",
                    coordinator_session,
                    "--risk-class",
                    "read_only",
                    "--operation",
                    "review_artifact",
                    "--scope-repository",
                    "/example/project",
                    "--authorization-basis",
                    "operator_confirmation",
                    "--authority",
                    "example operator",
                    "--authorization-reference",
                    "synthetic CLI approval",
                    "--authorization-verified-at",
                    verified_at,
                    "--authorization-expires-at",
                    authorization_expires,
                    "--intent",
                    "Review a synthetic artifact",
                    "--body",
                    "Review without making changes.",
                    "--output",
                    str(request_path),
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
            )
            self.assertEqual(request.returncode, 0, request.stderr)
            self.assertEqual(request.stdout, b"")

            result = subprocess.run(
                [
                    sys.executable,
                    tool,
                    "build-result",
                    "--request",
                    str(request_path),
                    "--sender-vendor",
                    "claude-code",
                    "--sender-name",
                    "cli worker",
                    "--sender-session",
                    worker_session,
                    "--reply-transport",
                    "claude_send_message",
                    "--reply-address",
                    worker_session,
                    "--body",
                    "Review completed without changes.",
                    "--output",
                    str(result_path),
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, b"")
            correlated = cam1.validate_exact_bytes(
                result_path.read_bytes(), against_raw=request_path.read_bytes()
            )
            self.assertTrue(correlated.correlated)

    def test_cli_exposes_every_typed_builder(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "cam1.py"), "--help"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        for command in (
            "build-challenge",
            "build-verify",
            "build-request",
            "build-cancel",
            "build-status",
            "build-result",
            "build-error",
            "build-status-inquiry",
            "renew-request",
            "build-late-rejection",
        ):
            self.assertIn(command, completed.stdout)

    def test_builder_cli_requires_an_explicit_output_destination(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "tools" / "cam1.py"),
                "build-hello",
                "--sender-vendor",
                "codex",
                "--sender-name",
                "cli coordinator",
                "--sender-session",
                "00000000-0000-4000-8000-000000000201",
                "--recipient-vendor",
                "claude-code",
                "--recipient-name",
                "cli worker",
                "--reply-transport",
                "codex_queue",
                "--reply-address",
                "00000000-0000-4000-8000-000000000201",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(completed.stdout, b"")
        self.assertIn(b"--output", completed.stderr)
        self.assertIn(b"--stdout", completed.stderr)
        self.assertIn(b"one of the arguments", completed.stderr)

    def test_builder_cli_stdout_is_explicit_and_exact(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "tools" / "cam1.py"),
                "build-hello",
                "--sender-vendor",
                "codex",
                "--sender-name",
                "cli coordinator",
                "--sender-session",
                "00000000-0000-4000-8000-000000000201",
                "--recipient-vendor",
                "claude-code",
                "--recipient-name",
                "cli worker",
                "--reply-transport",
                "codex_queue",
                "--reply-address",
                "00000000-0000-4000-8000-000000000201",
                "--stdout",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertFalse(completed.stdout.endswith(b"\n"))
        self.assertEqual(completed.stderr, b"")
        cam1.validate_exact_bytes(completed.stdout)


if __name__ == "__main__":
    unittest.main()
