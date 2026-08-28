# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import datetime as dt
import json
import unittest
from pathlib import Path
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


if __name__ == "__main__":
    unittest.main()
