# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import argparse
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
from tools.cam1lib import cli as cam1_cli

if __package__:
    from .test_cam1 import FIXTURES, ROOT, changed, fixture
else:
    from test_cam1 import FIXTURES, ROOT, changed, fixture

CLI_SUBPROCESS_TIMEOUT_SECONDS = 10


class CamPublicSurfaceTests(unittest.TestCase):
    def test_request_ack_cli_uses_generic_defaults(self) -> None:
        now = dt.datetime.now(dt.UTC)
        root = cam1.build_request(
            sender_vendor="codex",
            sender_name="coordinator",
            sender_session="00000000-0000-4000-8000-000000000101",
            recipient_vendor="claude-code",
            recipient_name="worker",
            recipient_session="00000000-0000-4000-8000-000000000102",
            reply_transport="codex_queue",
            reply_address="00000000-0000-4000-8000-000000000101",
            risk_class="informational",
            operation="discuss",
            intent="Discuss an idea",
            body="What do you think?",
            authorization_basis="none",
            now=now,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request.json"
            path.write_bytes(root)
            path.chmod(0o600)
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "tools" / "cam1.py"),
                    "build-ack",
                    "--request",
                    str(path),
                    "--sender-vendor",
                    "claude-code",
                    "--sender-name",
                    "worker",
                    "--sender-session",
                    "00000000-0000-4000-8000-000000000102",
                    "--reply-transport",
                    "claude_send_message",
                    "--reply-address",
                    "00000000-0000-4000-8000-000000000102",
                    "--status",
                    "received",
                    "--stdout",
                ],
                capture_output=True,
                timeout=CLI_SUBPROCESS_TIMEOUT_SECONDS,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        ack = cam1.validate_exact_bytes(
            result.stdout, against_raw=root, now=now
        ).envelope
        self.assertEqual(ack["authorization"]["basis"], "none")
        self.assertEqual(ack["intent"], "Acknowledge CAM/1 request")

    def test_protocol_examples_equal_checked_fixtures(self) -> None:
        protocol = (ROOT / "PROTOCOL.md").read_text(encoding="utf-8")
        section = protocol.split("## 20. Minimal wire-envelope example", 1)[1]
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
                timeout=CLI_SUBPROCESS_TIMEOUT_SECONDS,
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
            timeout=CLI_SUBPROCESS_TIMEOUT_SECONDS,
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

    def test_refused_builder_does_not_create_or_replace_output(self) -> None:
        tool = str(ROOT / "tools" / "cam1.py")
        common = [
            sys.executable,
            tool,
            "build-request",
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
            "--recipient-session",
            "00000000-0000-4000-8000-000000000202",
            "--reply-transport",
            "codex_queue",
            "--reply-address",
            "00000000-0000-4000-8000-000000000201",
            "--risk-class",
            "informational",
            "--operation",
            "review",
            "--authorization-basis",
            "none",
            "--authority",
            "not allowed with authorization basis none",
            "--intent",
            "Exercise a builder refusal",
            "--body",
            "No action requested.",
        ]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "refused.cam1.json"
            refused = subprocess.run(
                [*common, "--output", str(output)],
                cwd=ROOT,
                check=False,
                capture_output=True,
            )

            self.assertEqual(refused.returncode, 2)
            self.assertEqual(refused.stdout, b"")
            self.assertFalse(output.exists())
            self.assertFalse(json.loads(refused.stderr)["valid"])

            sentinel = b"pre-existing operator file\n"
            output.write_bytes(sentinel)
            output.chmod(0o600)
            repeated = subprocess.run(
                [*common, "--output", str(output)],
                cwd=ROOT,
                check=False,
                capture_output=True,
            )

            self.assertEqual(repeated.returncode, 2)
            self.assertEqual(repeated.stdout, b"")
            self.assertEqual(output.read_bytes(), sentinel)

    def test_output_gate_rejects_diagnostic_json_before_publication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "diagnostic.cam1.json"
            args = argparse.Namespace(stdout=False, output=str(output))

            with self.assertRaises(cam1.CamValidationError):
                cam1_cli._write_built_envelope(args, b'{"valid":false}')

            self.assertFalse(output.exists())

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
