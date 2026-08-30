# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import asyncio
import errno
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from tools import cam1, cam1_transport, cam1_transport_native

if __package__:
    from .test_cam1_transport import (
        CLAUDE_SESSION,
        CODEX_THREAD,
        ROOT,
        TRANSPORT_CLI,
        build_first_contact,
        with_agent_view,
        write_private,
    )
else:
    from test_cam1_transport import (
        CLAUDE_SESSION,
        CODEX_THREAD,
        ROOT,
        TRANSPORT_CLI,
        build_first_contact,
        with_agent_view,
        write_private,
    )


class TransportCliRoundTripTests(unittest.TestCase):
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

    def test_reply_type_requires_preserved_original(self) -> None:
        original = build_first_contact()
        reply = cam1.build_ack(
            original,
            sender_vendor="claude-code",
            sender_name="local-worker",
            sender_session=CLAUDE_SESSION,
            reply_transport="claude_send_message",
            reply_address=CLAUDE_SESSION,
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

    def test_live_transport_requires_private_single_link_envelopes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            envelope = temp / "hello.cam1.json"
            write_private(envelope, build_first_contact())
            envelope.chmod(0o644)
            with self.assertRaises(cam1.CliError) as public:
                cam1_transport._validate_envelope(str(envelope), None)
            self.assertEqual(public.exception.code, "input.private")

            envelope.chmod(0o600)
            linked = temp / "linked.cam1.json"
            os.link(envelope, linked)
            with self.assertRaises(cam1.CliError) as hard_link:
                cam1_transport._validate_envelope(str(envelope), None)
            self.assertEqual(hard_link.exception.code, "input.private")

    def test_codex_sender_callback_must_match_its_session(self) -> None:
        unrelated_thread = "00000000-0000-4000-8000-000000000199"
        envelope = json.loads(build_first_contact())
        envelope["reply_to"]["address"] = unrelated_thread
        with self.assertRaises(cam1.CamValidationError) as context:
            cam1.validate_exact_bytes(cam1.serialize_envelope(envelope))
        self.assertIn(
            "semantic.callback_identity",
            {problem.code for problem in context.exception.problems},
        )

    def test_codex_reply_accepts_equivalent_uppercase_thread_ids(self) -> None:
        canonical_thread = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        uppercase_thread = canonical_thread.upper()
        original = cam1.build_hello(
            sender_vendor="codex",
            sender_name="example coordinator",
            sender_session=uppercase_thread,
            recipient_vendor="claude-code",
            recipient_name="local-worker",
            recipient_session=CLAUDE_SESSION,
            reply_transport="codex_queue",
            reply_address=uppercase_thread,
        )
        reply = cam1.build_ack(
            original,
            sender_vendor="claude-code",
            sender_name="local-worker",
            sender_session=CLAUDE_SESSION,
            reply_transport="claude_send_message",
            reply_address=CLAUDE_SESSION,
            status_value="received",
        )
        receipt = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=(
                "Queued message 00000000-0000-4000-8000-000000000901 "
                f"for thread {canonical_thread}.\n"
            ),
            stderr="",
        )
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            original_path = temp / "hello.cam1.json"
            reply_path = temp / "ack.cam1.json"
            write_private(original_path, original)
            write_private(reply_path, reply)
            with (
                mock.patch.object(
                    cam1_transport_native, "_require_codex_state_write_access"
                ),
                mock.patch.object(
                    cam1_transport.subprocess, "run", return_value=receipt
                ) as run,
            ):
                result = cam1_transport._send_to_codex_queue(
                    codex_bin="/fake/codex",
                    thread=uppercase_thread,
                    envelope_path=str(reply_path),
                    against_path=str(original_path),
                    timeout_seconds=1,
                    before_send=lambda _validated: None,
                )

        self.assertTrue(result["ok"])
        self.assertEqual(result["target_thread"], canonical_thread)
        run.assert_called_once()
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--thread") + 1], canonical_thread)

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
            reply_address=CLAUDE_SESSION,
            status_value="received",
        )
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            original_path = temp / "hello.cam1.json"
            reply_path = temp / "ack.cam1.json"
            write_private(original_path, original)
            write_private(reply_path, reply)
            with (
                mock.patch.object(cam1_transport.subprocess, "run") as run,
                self.assertRaises(cam1_transport.TransportError) as context,
            ):
                cam1_transport._send_to_codex_queue(
                    codex_bin="/fake/codex",
                    thread=CODEX_THREAD,
                    envelope_path=str(reply_path),
                    against_path=str(original_path),
                    timeout_seconds=1,
                    before_send=lambda _validated: None,
                )
        self.assertEqual(context.exception.code, "envelope.callback_unavailable")
        run.assert_not_called()

    def test_claude_reply_resolves_fresh_route_from_stable_session_id(self) -> None:
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
                return {{
                    "success": True,
                    "msg_id": "00000000-0000-4000-8000-000000000900",
                    "to": to,
                }}

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
            reply_address=CLAUDE_SESSION,
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
            server.write_text(with_agent_view(fake_server_source), encoding="utf-8")
            server.chmod(0o700)
            original_path = temp / "hello.cam1.json"
            reply_path = temp / "ack.cam1.json"
            write_private(original_path, original)
            write_private(reply_path, reply)
            with mock.patch.object(
                cam1_transport_native,
                "_discover_agent_view_sessions",
                wraps=cam1_transport_native._discover_agent_view_sessions,
            ) as discover:
                result = asyncio.run(
                    cam1_transport._send_to_claude(
                        claude_bin=str(server),
                        session_id=CLAUDE_SESSION,
                        target="local-worker [abcdef]",
                        envelope_path=str(reply_path),
                        against_path=str(original_path),
                        summary=None,
                        timeout_seconds=5,
                        before_send=lambda _validated, _route: None,
                    )
                )
                preflight = asyncio.run(
                    cam1_transport._preflight_claude_session(
                        claude_bin=str(server),
                        session_id=CLAUDE_SESSION,
                        target="local-worker [abcdef]",
                        timeout_seconds=5,
                    )
                )
            self.assertEqual(discover.call_count, 4)
        self.assertEqual(result["target_session_id"], CLAUDE_SESSION)
        self.assertEqual(result["target"], "local-worker [abcdef]")
        self.assertEqual(preflight["identity"]["session_id"], CLAUDE_SESSION)

    def test_live_transport_rejects_schema_valid_oversized_envelope(self) -> None:
        raw = cam1.build_request(
            sender_vendor="codex",
            sender_name="example coordinator",
            sender_session=CODEX_THREAD,
            recipient_vendor="claude-code",
            recipient_name="local-worker",
            recipient_session=CLAUDE_SESSION,
            reply_transport="codex_queue",
            reply_address=CODEX_THREAD,
            risk_class="informational",
            operation="test_transport_limit",
            intent="Exercise the bounded live-transport limit",
            body="x" * 65_536,
            authorization_basis="none",
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
            reply_address=CLAUDE_SESSION,
            status_value="received",
        )
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            original_path = temp / "hello.cam1.json"
            reply_path = temp / "ack.cam1.json"
            write_private(original_path, original)
            write_private(reply_path, reply)
            with (
                mock.patch.object(
                    cam1_transport_native, "_require_codex_state_write_access"
                ),
                mock.patch.object(
                    cam1_transport.subprocess,
                    "run",
                    side_effect=OSError(errno.E2BIG, "argument list too long"),
                ),
                self.assertRaises(cam1_transport.TransportError) as context,
            ):
                cam1_transport._send_to_codex_queue(
                    codex_bin="/fake/codex",
                    thread=CODEX_THREAD,
                    envelope_path=str(reply_path),
                    against_path=str(original_path),
                    timeout_seconds=1,
                    before_send=lambda _validated: None,
                )
        self.assertEqual(context.exception.code, "transport.payload_too_large")

    def test_codex_read_only_state_fails_before_journal_or_dispatch(self) -> None:
        original = build_first_contact()
        reply = cam1.build_ack(
            original,
            sender_vendor="claude-code",
            sender_name="local-worker",
            sender_session=CLAUDE_SESSION,
            reply_transport="claude_send_message",
            reply_address=CLAUDE_SESSION,
            status_value="received",
        )
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            original_path = temp / "hello.cam1.json"
            reply_path = temp / "ack.cam1.json"
            write_private(original_path, original)
            write_private(reply_path, reply)
            fake_bin = temp / "bin" / "codex"
            fake_bin.parent.mkdir()
            fake_bin.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
            fake_bin.chmod(0o700)
            state_home = temp / "state"
            state_home.mkdir(mode=0o700)
            state_db = state_home / cam1_transport_native.CODEX_STATE_DB_NAME
            state_db.write_bytes(b"not opened by sqlite")
            state_db.chmod(0o600)
            before_send = mock.Mock()

            original_open = os.open

            def deny_state_write(path, flags, *args, **kwargs):
                if (
                    path == cam1_transport_native.CODEX_STATE_DB_NAME
                    and flags & os.O_RDWR
                ):
                    raise PermissionError("simulated read-only Codex state")
                return original_open(path, flags, *args, **kwargs)

            with (
                mock.patch.dict(os.environ, {"CODEX_HOME": str(state_home)}),
                mock.patch.object(os, "open", side_effect=deny_state_write),
                mock.patch.object(cam1_transport.subprocess, "run") as run,
                self.assertRaises(cam1_transport.TransportError) as context,
            ):
                cam1_transport._send_to_codex_queue(
                    codex_bin=str(fake_bin),
                    thread=CODEX_THREAD,
                    envelope_path=str(reply_path),
                    against_path=str(original_path),
                    timeout_seconds=1,
                    before_send=before_send,
                )

        self.assertEqual(context.exception.code, "codex.state_write_access")
        before_send.assert_not_called()
        run.assert_not_called()

    def test_codex_state_probe_rejects_unsafe_shapes_and_preserves_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            missing = temp / "missing"
            missing.mkdir(mode=0o700)

            symlinked = temp / "symlinked"
            symlinked.mkdir(mode=0o700)
            target = temp / "target.sqlite"
            target.write_bytes(b"target")
            (symlinked / cam1_transport_native.CODEX_STATE_DB_NAME).symlink_to(target)

            nonregular = temp / "nonregular"
            nonregular.mkdir(mode=0o700)
            (nonregular / cam1_transport_native.CODEX_STATE_DB_NAME).mkdir()

            for state_home in (missing, symlinked, nonregular):
                with self.subTest(state_home=state_home):
                    with (
                        mock.patch.dict(os.environ, {"CODEX_HOME": str(state_home)}),
                        self.assertRaises(cam1_transport.TransportError) as context,
                    ):
                        cam1_transport_native._require_codex_state_write_access()
                    self.assertEqual(context.exception.code, "codex.state_write_access")

            writable = temp / "writable"
            writable.mkdir(mode=0o700)
            state_db = writable / cam1_transport_native.CODEX_STATE_DB_NAME
            before = b"byte-stable state probe"
            state_db.write_bytes(before)
            state_db.chmod(0o600)
            with mock.patch.dict(os.environ, {"CODEX_HOME": str(writable)}):
                cam1_transport_native._require_codex_state_write_access()
            self.assertEqual(state_db.read_bytes(), before)

    def test_codex_nonzero_and_timeout_are_failures(self) -> None:
        original = build_first_contact()
        reply = cam1.build_ack(
            original,
            sender_vendor="claude-code",
            sender_name="local-worker",
            sender_session=CLAUDE_SESSION,
            reply_transport="claude_send_message",
            reply_address=CLAUDE_SESSION,
            status_value="received",
        )
        cases = (
            (
                subprocess.CompletedProcess([], 7, stdout="", stderr="rejected"),
                "codex.queue_failed",
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
                    with (
                        mock.patch.object(
                            cam1_transport_native,
                            "_require_codex_state_write_access",
                        ),
                        mock.patch.object(
                            cam1_transport.subprocess,
                            "run",
                            side_effect=outcome
                            if isinstance(outcome, BaseException)
                            else None,
                            return_value=outcome
                            if isinstance(outcome, subprocess.CompletedProcess)
                            else None,
                        ),
                        self.assertRaises(cam1_transport.TransportError) as context,
                    ):
                        cam1_transport._send_to_codex_queue(
                            codex_bin="/fake/codex",
                            thread=CODEX_THREAD,
                            envelope_path=str(reply_path),
                            against_path=str(original_path),
                            timeout_seconds=1,
                            before_send=lambda _validated: None,
                        )
                    self.assertEqual(context.exception.code, expected_code)

    def test_codex_receipt_requires_exact_stdout_shape_and_thread(self) -> None:
        original = build_first_contact()
        reply = cam1.build_ack(
            original,
            sender_vendor="claude-code",
            sender_name="local-worker",
            sender_session=CLAUDE_SESSION,
            reply_transport="claude_send_message",
            reply_address=CLAUDE_SESSION,
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
                    with (
                        mock.patch.object(
                            cam1_transport_native,
                            "_require_codex_state_write_access",
                        ),
                        mock.patch.object(
                            cam1_transport.subprocess, "run", return_value=completed
                        ),
                        self.assertRaises(cam1_transport.TransportError) as context,
                    ):
                        cam1_transport._send_to_codex_queue(
                            codex_bin="/fake/codex",
                            thread=CODEX_THREAD,
                            envelope_path=str(reply_path),
                            against_path=str(original_path),
                            timeout_seconds=1,
                            before_send=lambda _validated: None,
                        )
                    self.assertEqual(
                        context.exception.code, "codex.receipt_unrecognized"
                    )


if __name__ == "__main__":
    unittest.main()
