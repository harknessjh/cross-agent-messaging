# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from tools import cam1, cam1_transport

ROOT = Path(__file__).resolve().parents[1]
TRANSPORT_CLI = ROOT / "tools" / "cam1_transport.py"
CODEX_THREAD = "00000000-0000-4000-8000-000000000101"
CLAUDE_SESSION = "00000000-0000-4000-8000-000000000102"


def build_first_contact(recipient_name: str = "local-worker") -> bytes:
    return cam1.build_hello(
        sender_vendor="codex",
        sender_name="example coordinator",
        sender_session=CODEX_THREAD,
        recipient_vendor="claude-code",
        recipient_name=recipient_name,
        recipient_session=CLAUDE_SESSION,
        reply_transport="codex_queue",
        reply_address=CODEX_THREAD,
    )


def write_private(path: Path, raw: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, raw)
    finally:
        os.close(descriptor)


class PeerParsingTests(unittest.TestCase):
    def test_non_finite_timeout_is_rejected(self) -> None:
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                with self.assertRaises(cam1_transport.TransportError) as context:
                    cam1_transport._bounded_timeout(value)
                self.assertEqual(context.exception.code, "argument.timeout")

    def test_oversized_structured_receipt_is_replaced_by_digest(self) -> None:
        value = {"payload": "x" * cam1_transport.MAX_RECEIPT_TEXT}
        bounded = cam1_transport._bounded_json_value(value)
        self.assertEqual(bounded["omitted"], "oversized transport result")
        self.assertEqual(len(bounded["sha256"]), 64)

    def test_mcp_sdk_check_enforces_declared_minimum(self) -> None:
        with mock.patch.object(
            cam1_transport.importlib.metadata, "version", return_value="2.0.9"
        ):
            supported, version = cam1_transport._mcp_sdk_check()
        self.assertFalse(supported)
        self.assertEqual(version, "2.0.9")

        with mock.patch.object(
            cam1_transport.importlib.metadata, "version", return_value="2.1.0"
        ):
            supported, version = cam1_transport._mcp_sdk_check()
        self.assertTrue(supported)
        self.assertEqual(version, "2.1.0")

    def test_doctor_fails_when_declared_mcp_minimum_is_not_met(self) -> None:
        successful_probe = {"ok": True, "exit_code": 0, "output": "test"}
        with (
            mock.patch.object(
                cam1_transport, "_resolve_binary", return_value="/bin/test"
            ),
            mock.patch.object(
                cam1_transport, "_run_probe_before", return_value=successful_probe
            ),
            mock.patch.object(
                cam1_transport, "_mcp_sdk_check", return_value=(False, "2.0.9")
            ),
        ):
            result = cam1_transport.doctor(
                claude_bin="claude", codex_bin="codex", timeout_seconds=1
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["checks"]["mcp_sdk"]["version"], "2.0.9")

    def test_parser_keeps_only_recognized_local_session_kinds(self) -> None:
        listing = """Peer sessions (4):
  local-worker [abcdef]  ·  interactive  ·  idle  ·  started now
  web-worker [123456]  ·  cloud  ·  idle  ·  started now
  desktop-worker [234567]  ·  interactive  ·  Remote Control  ·  idle
  future-worker [345678]  ·  unfamiliar  ·  idle  ·  started now
"""
        peers = cam1_transport.parse_peers(listing)
        self.assertEqual([peer.name for peer in peers if peer.local], ["local-worker"])
        self.assertEqual(
            [peer.name for peer in peers if not peer.local],
            ["web-worker", "desktop-worker", "future-worker"],
        )

    def test_target_requires_fresh_qualified_address(self) -> None:
        peers = (
            cam1_transport.Peer(
                name="worker",
                ref="aaaaaa",
                kind="interactive",
                state="idle",
                details=(),
                local=True,
            ),
            cam1_transport.Peer(
                name="worker",
                ref="bbbbbb",
                kind="interactive",
                state="idle",
                details=(),
                local=True,
            ),
        )
        with self.assertRaises(cam1_transport.TransportError) as context:
            cam1_transport._resolve_local_peer("worker", peers)
        self.assertEqual(context.exception.code, "claude.target_unqualified")
        resolved = cam1_transport._resolve_local_peer("worker [bbbbbb]", peers)
        self.assertEqual(resolved.ref, "bbbbbb")


class TransportCliRoundTripTests(unittest.TestCase):
    def test_validated_envelope_round_trips_through_fake_claude_mcp(self) -> None:
        fake_server_source = textwrap.dedent(
            f"""\
            #!{sys.executable}
            import hashlib
            import sys
            from mcp.server import MCPServer

            print("FAKE CLAUDE WARNING", file=sys.stderr, flush=True)

            server = MCPServer("fake-claude")
            listed = False

            @server.tool()
            def ListAgents():
                global listed
                listed = True
                return {{"listing": "Peer sessions (1):\\n  local-worker [abcdef]  ·  interactive  ·  idle  ·  started now"}}

            @server.tool()
            def SendMessage(to: str, summary: str, message: str):
                if not listed:
                    raise RuntimeError("SendMessage must follow ListAgents in one MCP session")
                return {{
                    "success": True,
                    "msg_id": "00000000-0000-4000-8000-000000000900",
                    "to": to,
                    "summary": summary,
                    "message_sha256": hashlib.sha256(message.encode("utf-8")).hexdigest(),
                }}

            server.run(transport="stdio")
            """
        )
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            server = temp / "fake-claude"
            server.write_text(fake_server_source, encoding="utf-8")
            server.chmod(0o700)
            envelope = temp / "hello.cam1.json"
            raw = build_first_contact()
            write_private(envelope, raw)

            completed = subprocess.run(
                [
                    sys.executable,
                    str(TRANSPORT_CLI),
                    "--claude-bin",
                    str(server),
                    "claude-send",
                    "--to",
                    "local-worker [abcdef]",
                    "--envelope",
                    str(envelope),
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
                timeout=20,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        result = json.loads(completed.stdout)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "transport_accepted")
        self.assertFalse(result["application_ack"])
        self.assertEqual(result["target"], "local-worker [abcdef]")
        self.assertEqual(result["target_ref"], "abcdef")
        self.assertEqual(
            result["transport_message_id"],
            "00000000-0000-4000-8000-000000000900",
        )
        receipt = result["transport_receipt"]
        self.assertFalse(receipt["is_error"])
        receipt_objects = []
        if isinstance(receipt.get("structured_content"), dict):
            receipt_objects.append(receipt["structured_content"])
        for block in receipt.get("text_content", []):
            try:
                decoded = json.loads(block)
            except json.JSONDecodeError:
                continue
            if isinstance(decoded, dict):
                receipt_objects.append(decoded)
        unique_receipts = {
            json.dumps(value, separators=(",", ":"), sort_keys=True): value
            for value in receipt_objects
        }
        self.assertEqual(len(unique_receipts), 1)
        delivered = next(iter(unique_receipts.values()))
        self.assertIs(delivered["success"], True)
        self.assertEqual(delivered["to"], "local-worker [abcdef]")
        self.assertEqual(delivered["message_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(
            delivered["summary"],
            f"CAM/1 hello message {result['message_id']}",
        )

    def test_claude_send_rejects_negative_or_incomplete_receipts(self) -> None:
        for returned, expected_code in (
            ({"success": False, "message": "refused"}, "claude.send_rejected"),
            ({"success": True, "message": "sent"}, "claude.receipt_unrecognized"),
        ):
            with self.subTest(returned=returned):
                fake_server_source = textwrap.dedent(
                    f"""\
                    #!{sys.executable}
                    import sys
                    from mcp.server import MCPServer

                    print("FAKE CLAUDE WARNING", file=sys.stderr, flush=True)

                    server = MCPServer("fake-claude")

                    @server.tool()
                    def ListAgents():
                        return {{"listing": "Peer sessions (1):\\n  local-worker [abcdef]  ·  interactive  ·  idle  ·  started now"}}

                    @server.tool()
                    def SendMessage(to: str, summary: str, message: str):
                        return {returned!r}

                    server.run(transport="stdio")
                    """
                )
                with tempfile.TemporaryDirectory() as directory:
                    temp = Path(directory)
                    server = temp / "fake-claude"
                    server.write_text(fake_server_source, encoding="utf-8")
                    server.chmod(0o700)
                    envelope = temp / "hello.cam1.json"
                    write_private(envelope, build_first_contact())
                    completed = subprocess.run(
                        [
                            sys.executable,
                            str(TRANSPORT_CLI),
                            "--claude-bin",
                            str(server),
                            "claude-send",
                            "--to",
                            "local-worker [abcdef]",
                            "--envelope",
                            str(envelope),
                        ],
                        cwd=ROOT,
                        check=False,
                        capture_output=True,
                        text=True,
                        timeout=20,
                    )
                self.assertEqual(completed.returncode, 2)
                self.assertEqual(len(completed.stderr.splitlines()), 1)
                error = json.loads(completed.stderr)
                self.assertEqual(error["error"]["code"], expected_code)

    def test_claude_send_uses_one_overall_timeout(self) -> None:
        fake_server_source = textwrap.dedent(
            f"""\
            #!{sys.executable}
            import time
            from mcp.server import MCPServer

            server = MCPServer("fake-claude")

            @server.tool()
            def ListAgents():
                time.sleep(0.2)
                return {{"listing": "Peer sessions (0):"}}

            @server.tool()
            def SendMessage(to: str, summary: str, message: str):
                return {{"success": True, "msg_id": "00000000-0000-4000-8000-000000000900"}}

            server.run(transport="stdio")
            """
        )
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            server = temp / "fake-claude"
            server.write_text(fake_server_source, encoding="utf-8")
            server.chmod(0o700)
            envelope = temp / "hello.cam1.json"
            write_private(envelope, build_first_contact())
            completed = subprocess.run(
                [
                    sys.executable,
                    str(TRANSPORT_CLI),
                    "--claude-bin",
                    str(server),
                    "--timeout-seconds",
                    "0.05",
                    "claude-send",
                    "--to",
                    "local-worker [abcdef]",
                    "--envelope",
                    str(envelope),
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
                timeout=20,
            )
        self.assertEqual(completed.returncode, 2)
        error = json.loads(completed.stderr)
        self.assertEqual(error["error"]["code"], "claude.timeout")

    def test_argument_errors_use_the_json_error_channel(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(TRANSPORT_CLI), "not-a-command"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(completed.returncode, 2)
        result = json.loads(completed.stderr)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "argument.invalid")
        self.assertEqual(completed.stdout, "")

    def test_claude_send_rejects_recipient_target_mismatch_before_send(self) -> None:
        fake_server_source = textwrap.dedent(
            f"""\
            #!{sys.executable}
            from mcp.server import MCPServer

            server = MCPServer("fake-claude")

            @server.tool()
            def ListAgents():
                return {{"listing": "Peer sessions (1):\\n  other-worker [abcdef]  ·  interactive  ·  idle  ·  started now"}}

            @server.tool()
            def SendMessage(to: str, summary: str, message: str):
                raise RuntimeError("must not be called")

            server.run(transport="stdio")
            """
        )
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            server = temp / "fake-claude"
            server.write_text(fake_server_source, encoding="utf-8")
            server.chmod(0o700)
            envelope = temp / "hello.cam1.json"
            write_private(envelope, build_first_contact())
            completed = subprocess.run(
                [
                    sys.executable,
                    str(TRANSPORT_CLI),
                    "--claude-bin",
                    str(server),
                    "claude-send",
                    "--to",
                    "other-worker [abcdef]",
                    "--envelope",
                    str(envelope),
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
                timeout=20,
            )

        self.assertEqual(completed.returncode, 2)
        result = json.loads(completed.stderr)
        self.assertEqual(result["error"]["code"], "envelope.recipient_mismatch")

    def test_claude_send_rejects_freshly_listed_remote_target(self) -> None:
        fake_server_source = textwrap.dedent(
            f"""\
            #!{sys.executable}
            from mcp.server import MCPServer

            server = MCPServer("fake-claude")

            @server.tool()
            def ListAgents():
                return {{"listing": "Peer sessions (1):\\n  local-worker [abcdef]  ·  cloud  ·  idle  ·  started now"}}

            @server.tool()
            def SendMessage(to: str, summary: str, message: str):
                raise RuntimeError("must not be called")

            server.run(transport="stdio")
            """
        )
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            server = temp / "fake-claude"
            server.write_text(fake_server_source, encoding="utf-8")
            server.chmod(0o700)
            envelope = temp / "hello.cam1.json"
            write_private(envelope, build_first_contact())
            completed = subprocess.run(
                [
                    sys.executable,
                    str(TRANSPORT_CLI),
                    "--claude-bin",
                    str(server),
                    "claude-send",
                    "--to",
                    "local-worker [abcdef]",
                    "--envelope",
                    str(envelope),
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
                timeout=20,
            )
        self.assertEqual(completed.returncode, 2)
        result = json.loads(completed.stderr)
        self.assertEqual(result["error"]["code"], "claude.target_not_local")

    def test_reply_type_requires_preserved_original(self) -> None:
        original = build_first_contact()
        reply = cam1.build_ack(
            original,
            sender_vendor="claude-code",
            sender_name="local-worker",
            sender_session=CLAUDE_SESSION,
            reply_transport="claude_send_message",
            reply_address="local-worker",
            status_value="needs_human_confirmation",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "reply.cam1.json"
            write_private(path, reply)
            with self.assertRaises(cam1_transport.TransportError) as context:
                cam1_transport._validate_envelope(str(path), None)
        self.assertEqual(context.exception.code, "argument.against_required")

    def test_live_transport_rejects_stdin_envelopes(self) -> None:
        with self.assertRaises(cam1_transport.TransportError) as context:
            cam1_transport._validate_envelope("-", None)
        self.assertEqual(context.exception.code, "argument.envelope_file")

    def test_codex_reply_requires_the_original_callback(self) -> None:
        unrelated_thread = "00000000-0000-4000-8000-000000000199"
        original = cam1.build_hello(
            sender_vendor="codex",
            sender_name="example coordinator",
            sender_session=CODEX_THREAD,
            recipient_vendor="claude-code",
            recipient_name="local-worker",
            recipient_session=CLAUDE_SESSION,
            reply_transport="codex_queue",
            reply_address=unrelated_thread,
        )
        reply = cam1.build_ack(
            original,
            sender_vendor="claude-code",
            sender_name="local-worker",
            sender_session=CLAUDE_SESSION,
            reply_transport="claude_send_message",
            reply_address="local-worker [abcdef]",
            status_value="received",
        )
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            original_path = temp / "hello.cam1.json"
            reply_path = temp / "ack.cam1.json"
            write_private(original_path, original)
            write_private(reply_path, reply)
            with mock.patch.object(cam1_transport.subprocess, "run") as run:
                with self.assertRaises(cam1_transport.TransportError) as context:
                    cam1_transport.reply_to_codex(
                        codex_bin="/fake/codex",
                        thread=CODEX_THREAD,
                        envelope_path=str(reply_path),
                        against_path=str(original_path),
                        timeout_seconds=1,
                    )
        self.assertEqual(context.exception.code, "envelope.callback_mismatch")
        run.assert_not_called()

    def test_reply_to_one_way_original_has_an_actionable_error(self) -> None:
        original_envelope = json.loads(build_first_contact())
        original_envelope["reply_to"] = None
        original = cam1.serialize_envelope(original_envelope)
        reply = cam1.build_ack(
            original,
            sender_vendor="claude-code",
            sender_name="local-worker",
            sender_session=CLAUDE_SESSION,
            reply_transport="claude_send_message",
            reply_address="local-worker [abcdef]",
            status_value="received",
        )
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            original_path = temp / "hello.cam1.json"
            reply_path = temp / "ack.cam1.json"
            write_private(original_path, original)
            write_private(reply_path, reply)
            with mock.patch.object(cam1_transport.subprocess, "run") as run:
                with self.assertRaises(cam1_transport.TransportError) as context:
                    cam1_transport.reply_to_codex(
                        codex_bin="/fake/codex",
                        thread=CODEX_THREAD,
                        envelope_path=str(reply_path),
                        against_path=str(original_path),
                        timeout_seconds=1,
                    )
        self.assertEqual(context.exception.code, "envelope.callback_unavailable")
        run.assert_not_called()

    def test_claude_reply_requires_the_exact_original_callback_ref(self) -> None:
        fake_server_source = textwrap.dedent(
            f"""\
            #!{sys.executable}
            from mcp.server import MCPServer

            server = MCPServer("fake-claude")

            @server.tool()
            def ListAgents():
                return {{"listing": "Peer sessions (1):\\n  local-worker [abcdef]  \u00b7  interactive  \u00b7  idle  \u00b7  started now"}}

            @server.tool()
            def SendMessage(to: str, summary: str, message: str):
                raise RuntimeError("must not be called")

            server.run(transport="stdio")
            """
        )
        original = cam1.build_hello(
            sender_vendor="claude-code",
            sender_name="local-worker",
            sender_session=CLAUDE_SESSION,
            recipient_vendor="codex",
            recipient_name="example coordinator",
            recipient_session=CODEX_THREAD,
            reply_transport="claude_send_message",
            reply_address="local-worker [bbbbbb]",
        )
        reply = cam1.build_ack(
            original,
            sender_vendor="codex",
            sender_name="example coordinator",
            sender_session=CODEX_THREAD,
            reply_transport="codex_queue",
            reply_address=CODEX_THREAD,
            status_value="received",
        )
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            server = temp / "fake-claude"
            server.write_text(fake_server_source, encoding="utf-8")
            server.chmod(0o700)
            original_path = temp / "hello.cam1.json"
            reply_path = temp / "ack.cam1.json"
            write_private(original_path, original)
            write_private(reply_path, reply)
            with self.assertRaises(cam1_transport.TransportError) as context:
                asyncio.run(
                    cam1_transport.send_to_claude(
                        claude_bin=str(server),
                        target="local-worker [abcdef]",
                        envelope_path=str(reply_path),
                        against_path=str(original_path),
                        summary=None,
                        timeout_seconds=5,
                    )
                )
        self.assertEqual(context.exception.code, "envelope.callback_mismatch")

    def test_live_transport_rejects_schema_valid_oversized_envelope(self) -> None:
        raw = cam1.build_hello(
            sender_vendor="codex",
            sender_name="example coordinator",
            sender_session=CODEX_THREAD,
            recipient_vendor="claude-code",
            recipient_name="local-worker",
            recipient_session=CLAUDE_SESSION,
            reply_transport="codex_queue",
            reply_address=CODEX_THREAD,
            body="x" * 65_536,
        )
        self.assertGreater(len(raw), cam1_transport.MAX_TRANSPORT_ENVELOPE_BYTES)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "large.cam1.json"
            write_private(path, raw)
            with self.assertRaises(cam1_transport.TransportError) as context:
                cam1_transport._validate_envelope(str(path), None)
        self.assertEqual(context.exception.code, "transport.payload_too_large")

    def test_codex_e2big_has_specific_diagnostic(self) -> None:
        original = build_first_contact()
        reply = cam1.build_ack(
            original,
            sender_vendor="claude-code",
            sender_name="local-worker",
            sender_session=CLAUDE_SESSION,
            reply_transport="claude_send_message",
            reply_address="local-worker",
            status_value="received",
        )
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            original_path = temp / "hello.cam1.json"
            reply_path = temp / "ack.cam1.json"
            write_private(original_path, original)
            write_private(reply_path, reply)
            with mock.patch.object(
                cam1_transport.subprocess,
                "run",
                side_effect=OSError(errno.E2BIG, "argument list too long"),
            ):
                with self.assertRaises(cam1_transport.TransportError) as context:
                    cam1_transport.reply_to_codex(
                        codex_bin="/fake/codex",
                        thread=CODEX_THREAD,
                        envelope_path=str(reply_path),
                        against_path=str(original_path),
                        timeout_seconds=1,
                    )
        self.assertEqual(context.exception.code, "transport.payload_too_large")

    def test_codex_nonzero_and_timeout_are_rejected(self) -> None:
        original = build_first_contact()
        reply = cam1.build_ack(
            original,
            sender_vendor="claude-code",
            sender_name="local-worker",
            sender_session=CLAUDE_SESSION,
            reply_transport="claude_send_message",
            reply_address="local-worker",
            status_value="received",
        )
        cases = (
            (
                subprocess.CompletedProcess([], 7, stdout="", stderr="rejected"),
                "codex.queue_rejected",
            ),
            (subprocess.TimeoutExpired(["codex"], 1), "codex.queue_failure"),
        )
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            original_path = temp / "hello.cam1.json"
            reply_path = temp / "ack.cam1.json"
            write_private(original_path, original)
            write_private(reply_path, reply)
            for outcome, expected_code in cases:
                with self.subTest(expected_code=expected_code):
                    with mock.patch.object(
                        cam1_transport.subprocess,
                        "run",
                        side_effect=outcome
                        if isinstance(outcome, BaseException)
                        else None,
                        return_value=outcome
                        if isinstance(outcome, subprocess.CompletedProcess)
                        else None,
                    ):
                        with self.assertRaises(
                            cam1_transport.TransportError
                        ) as context:
                            cam1_transport.reply_to_codex(
                                codex_bin="/fake/codex",
                                thread=CODEX_THREAD,
                                envelope_path=str(reply_path),
                                against_path=str(original_path),
                                timeout_seconds=1,
                            )
                    self.assertEqual(context.exception.code, expected_code)

    def test_correlated_reply_round_trips_through_fake_codex_queue(self) -> None:
        original = build_first_contact()
        reply = cam1.build_ack(
            original,
            sender_vendor="claude-code",
            sender_name="local-worker",
            sender_session=CLAUDE_SESSION,
            reply_transport="claude_send_message",
            reply_address="local-worker",
            status_value="received",
        )
        fake_codex_source = textwrap.dedent(
            f"""\
            #!{sys.executable}
            import hashlib
            import json
            import sys

            thread = sys.argv[sys.argv.index("--thread") + 1]
            message = sys.argv[sys.argv.index("--message") + 1]
            digest = hashlib.sha256(message.encode("utf-8")).hexdigest()
            if digest != "{hashlib.sha256(reply).hexdigest()}":
                raise SystemExit(9)
            print(f"Queued message 00000000-0000-4000-8000-000000000901 for thread {{thread}}.")
            """
        )
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            codex = temp / "fake-codex"
            codex.write_text(fake_codex_source, encoding="utf-8")
            codex.chmod(0o700)
            original_path = temp / "hello.cam1.json"
            reply_path = temp / "ack.cam1.json"
            write_private(original_path, original)
            write_private(reply_path, reply)
            completed = subprocess.run(
                [
                    sys.executable,
                    str(TRANSPORT_CLI),
                    "--codex-bin",
                    str(codex),
                    "codex-reply",
                    "--thread",
                    CODEX_THREAD,
                    "--envelope",
                    str(reply_path),
                    "--against",
                    str(original_path),
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
                timeout=20,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "transport_accepted")
        self.assertFalse(result["application_ack"])
        self.assertEqual(result["target_thread"], CODEX_THREAD)
        receipt = result["transport_receipt"]
        self.assertEqual(receipt["queue_id"], "00000000-0000-4000-8000-000000000901")
        self.assertEqual(receipt["thread_id"], CODEX_THREAD)

    def test_codex_receipt_requires_exact_stdout_shape_and_thread(self) -> None:
        original = build_first_contact()
        reply = cam1.build_ack(
            original,
            sender_vendor="claude-code",
            sender_name="local-worker",
            sender_session=CLAUDE_SESSION,
            reply_transport="claude_send_message",
            reply_address="local-worker",
            status_value="received",
        )
        unrelated = "00000000-0000-4000-8000-000000000999"
        cases = (
            subprocess.CompletedProcess(
                [], 0, stdout="", stderr=f"warning {unrelated}"
            ),
            subprocess.CompletedProcess(
                [], 0, stdout=f"diagnostic {unrelated}", stderr=""
            ),
            subprocess.CompletedProcess(
                [],
                0,
                stdout=(
                    "Queued message 00000000-0000-4000-8000-000000000901 "
                    f"for thread {unrelated}."
                ),
                stderr="",
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            original_path = temp / "hello.cam1.json"
            reply_path = temp / "ack.cam1.json"
            write_private(original_path, original)
            write_private(reply_path, reply)
            for completed in cases:
                with self.subTest(completed=completed):
                    with mock.patch.object(
                        cam1_transport.subprocess, "run", return_value=completed
                    ):
                        with self.assertRaises(
                            cam1_transport.TransportError
                        ) as context:
                            cam1_transport.reply_to_codex(
                                codex_bin="/fake/codex",
                                thread=CODEX_THREAD,
                                envelope_path=str(reply_path),
                                against_path=str(original_path),
                                timeout_seconds=1,
                            )
                    self.assertEqual(
                        context.exception.code, "codex.receipt_unrecognized"
                    )


if __name__ == "__main__":
    unittest.main()
