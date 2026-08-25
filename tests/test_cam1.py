from __future__ import annotations

import ast
import datetime as dt
import hashlib
import json
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
NOW = dt.datetime(2026, 8, 21, 20, 5, tzinfo=dt.timezone.utc)


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def changed(name: str, mutate) -> bytes:
    envelope = json.loads(fixture(name))
    mutate(envelope)
    return cam1.serialize_envelope(envelope)


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
            "/audit_ref",
            "/claimed_sender/host_id",
            "/recipient/agent_name",
            "/receipt/detail",
        }
        self.assertTrue(expected.issubset(missing), missing)

    def test_duplicate_key_is_rejected_before_schema(self) -> None:
        problems = self.problem_codes(b'{"protocol":"CAM/1","protocol":"CAM/1"}')
        self.assertEqual(problems[0].code, "wire.duplicate_key")

    def test_malformed_utf8_is_rejected(self) -> None:
        problems = self.problem_codes(b'{"body":"\xff"}')
        self.assertEqual(problems[0].code, "wire.utf8")

    def test_non_finite_json_number_is_rejected(self) -> None:
        problems = self.problem_codes(b'{"value":NaN}')
        self.assertEqual(problems[0].code, "wire.json")

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

    def test_invalid_calendar_time_is_rejected(self) -> None:
        raw = changed(
            "valid-hello.json",
            lambda envelope: envelope.update(expires_at="2026-02-30T20:10:00Z"),
        )
        problems = self.problem_codes(raw)
        self.assertIn("semantic.timestamp", {item.code for item in problems})

    def test_expired_message_fails_closed_but_can_be_inspected(self) -> None:
        later = dt.datetime(2026, 8, 21, 21, 0, tzinfo=dt.timezone.utc)
        with self.assertRaises(cam1.CamValidationError):
            cam1.validate_exact_bytes(fixture("valid-hello.json"), now=later)
        result = cam1.validate_exact_bytes(
            fixture("valid-hello.json"),
            now=later,
            allow_expired=True,
        )
        self.assertFalse(result.fresh)

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
                cam1._read_bounded(str(link))


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


if __name__ == "__main__":
    unittest.main()
