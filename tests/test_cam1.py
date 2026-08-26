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
                    agent_name="different coordinator"
                ),
                "correlation.recipient",
                "/recipient",
            ),
            (
                "sender",
                lambda envelope: envelope["claimed_sender"].update(
                    agent_name="different worker"
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
        callback = [item for item in problems if item.code == "semantic.codex_callback"]
        self.assertEqual(len(callback), 1)

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
            reply_address="example worker",
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
            reply_address="example worker",
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
            "reply_address": "example worker",
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
                reply_address="example worker",
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
                reply_address="example worker",
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
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, b"")
        error = json.loads(completed.stderr)
        self.assertFalse(error["valid"])

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
        self.assertNotIn("trusted", summary)
        self.assertNotIn("authorized", summary)
        self.assertNotIn("safe", summary)
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
                    "cli worker",
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
