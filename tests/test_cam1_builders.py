# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import datetime as dt
import json
import stat
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar
from unittest import mock

from tools import cam1

if __package__:
    from .test_cam1 import NOW, challenge_envelope, fixture
else:
    from test_cam1 import NOW, challenge_envelope, fixture


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

    def test_serializer_uses_two_space_indentation(self) -> None:
        raw = cam1.serialize_envelope(
            {
                "protocol": "CAM/1",
                "body": "First line.\nSecond line.",
            }
        )

        self.assertEqual(
            raw,
            b'{\n  "protocol": "CAM/1",\n  "body": "First line.\\nSecond line."\n}',
        )
        self.assertFalse(raw.endswith(b"\n"))

    def test_validator_still_accepts_compact_envelopes(self) -> None:
        envelope = json.loads(self.build_request())
        compact = json.dumps(
            envelope,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")

        self.assertNotIn(b"\n", compact)
        cam1.validate_exact_bytes(compact, now=NOW)

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


if __name__ == "__main__":
    unittest.main()
