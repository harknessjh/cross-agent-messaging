# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import asyncio
import datetime as dt
import errno
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest import mock

from tools import cam1, cam1_transport, cam1_transport_native
from tools.cam1lib import journal, project, routing, state

ROOT = Path(__file__).resolve().parents[1]
TRANSPORT_CLI = ROOT / "tools" / "cam1_transport.py"
PROJECT_CLI = ROOT / "tools" / "cam1_project.py"
CODEX_THREAD = "00000000-0000-4000-8000-000000000101"
CLAUDE_SESSION = "00000000-0000-4000-8000-000000000102"
CODEX_PARTICIPANT = "00000000-0000-4000-8000-000000000201"
CLAUDE_PARTICIPANT = "00000000-0000-4000-8000-000000000202"


def live_validation_arguments() -> dict[str, object]:
    current = cam1.current_validation_profile()
    if current.source_control.kind == "git" and current.source_control.dirty:
        return {
            "allow_dirty_validator": True,
            "expected_validation_profile_sha256": (current.validation_profile_sha256),
        }
    return {}


def live_validation_cli_arguments() -> list[str]:
    arguments = live_validation_arguments()
    if not arguments:
        return []
    return [
        "--allow-dirty-validator",
        "--expected-validation-profile-sha256",
        str(arguments["expected_validation_profile_sha256"]),
    ]


def dirty_validator_override_used() -> bool:
    return bool(live_validation_arguments())


def build_first_contact(recipient_name: str = "local-worker") -> bytes:
    return cam1.build_hello(
        sender_vendor="codex",
        sender_name="example-coordinator",
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


def with_agent_view(
    source: str,
    *,
    name: str = "local-worker",
    kind: str = "interactive",
    state: str = "idle",
    cwd: str = "/example/project",
) -> str:
    """Add the real CLI's ``agents --json`` mode to a fake Claude executable."""
    shebang, body = source.split("\n", 1)
    listing = json.dumps(
        [
            {
                "id": CLAUDE_SESSION.split("-", 1)[0],
                "cwd": cwd,
                "kind": kind,
                "startedAt": 1_784_241_375_111,
                "sessionId": CLAUDE_SESSION,
                "name": name,
                "state": state,
                "peerAddress": "uds:/tmp/cam1-test-peer.sock",
            }
        ]
    )
    prelude = textwrap.dedent(
        f"""\
        import sys as _agent_view_sys
        if _agent_view_sys.argv[1:] == ["agents", "--json"]:
            print({listing!r})
            raise SystemExit(0)
        """
    )
    return f"{shebang}\n{prelude}{body}"


class PeerParsingTests(unittest.TestCase):
    def test_agent_view_refresh_rejects_stable_identity_changes(self) -> None:
        selected = routing.AgentViewSession(
            session_id=CLAUDE_SESSION,
            agent_view_id="00000000",
            product_name="local-worker",
            cwd="/example/project",
            kind="interactive",
            state="idle",
            started_at_ms=1_784_241_375_111,
        )
        changes = {
            "session_id": "00000000-0000-4000-8000-000000000103",
            "agent_view_id": "11111111",
            "product_name": "renamed-worker",
            "cwd": "/different/project",
            "kind": "background",
            "started_at_ms": selected.started_at_ms + 1,
        }
        for field_name, changed_value in changes.items():
            with self.subTest(field_name=field_name):
                refreshed = replace(selected, **{field_name: changed_value})
                with (
                    mock.patch.object(
                        cam1_transport_native,
                        "_discover_agent_view_sessions",
                        return_value={CLAUDE_SESSION: refreshed},
                    ),
                    self.assertRaises(cam1_transport.TransportError) as context,
                ):
                    asyncio.run(
                        cam1_transport_native._refresh_agent_view_session(
                            selected,
                            claude_bin="/not/executed/claude",
                            timeout_seconds=1,
                        )
                    )
                self.assertEqual(context.exception.code, "claude.session_changed")

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

    def test_accepted_state_incomplete_preserves_transport_receipt_id(self) -> None:
        message_id = "00000000-0000-4000-8000-000000000900"
        receipt_id = "00000000-0000-4000-8000-000000000901"
        intent_record = {
            "record_id": "00000000-0000-4000-8000-000000000902",
            "sequence": 1,
            "event_type": "message.outbound.intent",
            "record_sha256": "a" * 64,
        }
        accepted_record = {
            "record_id": "00000000-0000-4000-8000-000000000903",
            "sequence": 2,
            "event_type": "transport.accepted",
            "record_sha256": "b" * 64,
        }
        plan = mock.Mock()
        plan.preview.root_message_id = message_id
        plan.exact_message = b"preserved exact envelope"
        attempt = cam1_transport._SendAttempt(
            participant_id=CLAUDE_PARTICIPANT,
            transport="claude_send_message",
            route_address="local-worker [abcdef]",
            message_id=message_id,
            intent_record=intent_record,
            lifecycle_plan=plan,
            lifecycle_committed=True,
        )
        store = mock.Mock()
        store.snapshot.side_effect = cam1.CamUsageError(
            "state.committed_root_missing",
            "injected incomplete lifecycle state",
        )
        result = {
            "message_id": message_id,
            "transport_message_id": receipt_id,
        }

        with (
            mock.patch.object(
                cam1_transport.journal,
                "append_record",
                return_value=accepted_record,
            ),
            self.assertRaises(cam1_transport.TransportError) as context,
        ):
            cam1_transport._finalize_accepted_attempt(
                mock.sentinel.binding,
                store,
                mock.sentinel.transaction,
                attempt,
                result,
            )

        self.assertEqual(context.exception.code, "transport.accepted_state_incomplete")
        self.assertEqual(context.exception.audit["transport_receipt_id"], receipt_id)

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
                cam1_transport,
                "_agent_view_probe_before",
                return_value={"ok": True, "sessions": 1},
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

    def test_live_binary_resolution_requires_an_absolute_path(self) -> None:
        for supplied in ("claude", "./claude"):
            with self.subTest(supplied=supplied):
                with self.assertRaises(cam1_transport.TransportError) as context:
                    cam1_transport._resolve_binary(supplied, label="claude")
                self.assertEqual(
                    context.exception.code, "claude.absolute_path_required"
                )

    def test_doctor_requires_and_reports_explicit_live_binary_paths(self) -> None:
        successful_probe = {"ok": True, "exit_code": 0, "output": "test"}
        with (
            mock.patch.object(
                cam1_transport,
                "_resolve_binary",
                side_effect=("/opt/example/claude", "/opt/example/codex") * 2,
            ),
            mock.patch.object(
                cam1_transport, "_run_probe_before", return_value=successful_probe
            ),
            mock.patch.object(
                cam1_transport,
                "_agent_view_probe_before",
                return_value={"ok": True, "sessions": 1},
            ),
            mock.patch.object(
                cam1_transport, "_mcp_sdk_check", return_value=(True, "2.1.0")
            ),
        ):
            discovered = cam1_transport.doctor(
                claude_bin="claude", codex_bin="codex", timeout_seconds=1
            )
            explicit = cam1_transport.doctor(
                claude_bin="/opt/example/claude",
                codex_bin="/opt/example/codex",
                timeout_seconds=1,
            )

        self.assertTrue(discovered["prerequisites_ok"])
        self.assertFalse(discovered["ok"])
        self.assertEqual(discovered["status"], "operator_path_confirmation_required")
        self.assertFalse(
            discovered["live_path_configuration"]["explicit_absolute_paths_supplied"]
        )
        self.assertEqual(
            discovered["live_path_configuration"]["required_global_arguments"],
            [
                "--claude-bin",
                "/opt/example/claude",
                "--codex-bin",
                "/opt/example/codex",
            ],
        )
        self.assertEqual(
            discovered["live_path_configuration"]["copy_paste_flags"],
            "--claude-bin /opt/example/claude --codex-bin /opt/example/codex",
        )
        self.assertTrue(explicit["ok"])
        self.assertEqual(explicit["status"], "ready")
        self.assertTrue(explicit["live_path_configuration"]["ready"])

    def test_doctor_reports_dirty_validation_source_as_send_blocked(self) -> None:
        result = {
            "ok": True,
            "status": "ready",
            "checks": {},
            "live_path_configuration": {"ready": True},
        }
        with (
            mock.patch.object(cam1_transport, "doctor", return_value=result),
            mock.patch.object(
                cam1_transport,
                "_require_live_validation_profile",
                side_effect=cam1_transport.TransportError(
                    "profile.dirty_source",
                    "synthetic dirty source",
                ),
            ),
            mock.patch.object(cam1_transport, "_emit") as emit,
        ):
            returncode = cam1_transport.main(["doctor"])

        self.assertEqual(returncode, 2)
        payload = emit.call_args.args[0]
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "validation_profile_blocked")
        self.assertFalse(payload["live_path_configuration"]["ready"])
        self.assertEqual(
            payload["checks"]["validation_profile"]["code"],
            "profile.dirty_source",
        )

    def test_dirty_override_evidence_records_use_not_requested_flag(self) -> None:
        clean = cam1.ValidationProfile(
            validation_profile_sha256="a" * 64,
            component_count=1,
            source_control=cam1.SourceControlState("git", "b" * 40, False),
            python_implementation="TestPython",
            python_version="3.test",
            jsonschema_version="test",
            referencing_version="test",
            rpds_py_version="test",
            rfc3339_validator_version="test",
        )
        with mock.patch.object(cam1, "require_live_profile", return_value=clean):
            report, override_used = cam1_transport._require_live_validation_profile(
                allow_dirty=True,
                expected_sha256="a" * 64,
            )

        self.assertTrue(report["available"])
        self.assertFalse(override_used)

    def test_parser_keeps_only_recognized_local_session_kinds(self) -> None:
        listing = """Peer sessions (5):
  local-worker [abcdef]  ·  interactive  ·  idle  ·  started now
  web-worker [123456]  ·  cloud  ·  idle  ·  started now
  desktop-worker [234567]  ·  interactive  ·  Remote Control  ·  idle
  future-worker [345678]  ·  unfamiliar  ·  idle  ·  started now
  exited-worker [456789]  ·  interactive  ·  exited  ·  started earlier
"""
        peers = cam1_transport.parse_peers(listing)
        self.assertEqual([peer.name for peer in peers if peer.local], ["local-worker"])
        self.assertEqual(
            [peer.name for peer in peers if not peer.local],
            ["web-worker", "desktop-worker", "future-worker", "exited-worker"],
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

    def test_agent_view_parser_preserves_distinct_session_identifiers(self) -> None:
        raw = json.dumps(
            [
                {
                    "id": "00000000",
                    "cwd": "/example/project",
                    "kind": "interactive",
                    "startedAt": 1_784_241_375_111,
                    "sessionId": "00000000-0000-4000-8000-000000000102",
                    "name": "local-worker",
                    "state": "idle",
                    "peerAddress": "uds:/tmp/cc-socks/not-a-route.sock",
                }
            ]
        ).encode()
        sessions = cam1_transport.routing.parse_agent_view_sessions(raw)
        session = sessions[CLAUDE_SESSION]
        self.assertEqual(session.session_id, CLAUDE_SESSION)
        self.assertEqual(session.agent_view_id, "00000000")
        self.assertEqual(session.product_name, "local-worker")
        self.assertNotIn("peerAddress", session.as_dict())
        self.assertNotIn("uds:", json.dumps(session.as_dict()))

        peer = cam1_transport.Peer(
            name="local-worker",
            ref="abcdef",
            kind="interactive",
            state="idle",
            details=(),
            local=True,
        )
        route = cam1_transport.routing.correlate_route(session, (peer,))
        self.assertEqual(route.session.agent_view_id, "00000000")
        self.assertEqual(route.peer.ref, "abcdef")
        self.assertNotEqual(route.session.agent_view_id, route.peer.ref)

    def test_agent_view_selection_rejects_short_id_and_duplicate_names(self) -> None:
        rows = [
            {
                "id": "00000000",
                "cwd": "/example/one",
                "kind": "background",
                "startedAt": 1,
                "sessionId": CLAUDE_SESSION,
                "name": "duplicate-worker",
                "state": "idle",
            },
            {
                "id": "11111111",
                "cwd": "/example/two",
                "kind": "background",
                "startedAt": 2,
                "sessionId": "11111111-1111-4111-8111-111111111111",
                "name": "duplicate-worker",
                "state": "idle",
            },
        ]
        sessions = cam1_transport.routing.parse_agent_view_sessions(
            json.dumps(rows).encode()
        )
        with self.assertRaises(cam1_transport.routing.RoutingError) as short:
            cam1_transport.routing.select_agent_view_session(sessions, "00000000")
        self.assertEqual(short.exception.code, "claude.agents_format")
        with self.assertRaises(cam1_transport.routing.RoutingError) as duplicate:
            cam1_transport.routing.select_agent_view_session(sessions, CLAUDE_SESSION)
        self.assertEqual(duplicate.exception.code, "claude.agent_name_ambiguous")

    def test_agent_view_selection_rejects_nonlocal_and_terminal_sessions(self) -> None:
        for kind, product_state in (("cloud", "idle"), ("interactive", "exited")):
            with self.subTest(kind=kind, state=product_state):
                rows = [
                    {
                        "id": "00000000",
                        "cwd": "/example/project",
                        "kind": kind,
                        "startedAt": 1,
                        "sessionId": CLAUDE_SESSION,
                        "name": "local-worker",
                        "state": product_state,
                    }
                ]
                sessions = cam1_transport.routing.parse_agent_view_sessions(
                    json.dumps(rows).encode()
                )
                with self.assertRaises(cam1_transport.routing.RoutingError) as context:
                    cam1_transport.routing.select_agent_view_session(
                        sessions, CLAUDE_SESSION
                    )
                self.assertEqual(context.exception.code, "claude.session_not_local")

    def test_route_correlation_never_retargets_requested_address(self) -> None:
        session = cam1_transport.routing.AgentViewSession(
            session_id=CLAUDE_SESSION,
            agent_view_id="00000000",
            product_name="local-worker",
            cwd="/example/project",
            kind="interactive",
            state="idle",
            started_at_ms=1,
        )
        peer = cam1_transport.Peer(
            name="local-worker",
            ref="abcdef",
            kind="interactive",
            state="idle",
            details=(),
            local=True,
        )
        with self.assertRaises(cam1_transport.routing.RoutingError) as context:
            cam1_transport.routing.correlate_route(
                session,
                (peer,),
                requested_target="local-worker [bbbbbb]",
            )
        self.assertEqual(context.exception.code, "claude.target_session_mismatch")

    def test_discovery_parsers_are_bounded_and_detect_duplicate_addresses(self) -> None:
        with self.assertRaises(cam1_transport.routing.RoutingError) as oversized:
            cam1_transport.routing.parse_agent_view_sessions(
                b" " * (cam1_transport.routing.MAX_AGENT_VIEW_BYTES + 1)
            )
        self.assertEqual(oversized.exception.code, "claude.agents_size")

        duplicate_listing = (
            "Peer sessions (2):\n"
            "  worker [abcdef]  ·  interactive  ·  idle\n"
            "  worker [abcdef]  ·  cloud  ·  idle\n"
        )
        with self.assertRaises(cam1_transport.TransportError) as context:
            cam1_transport.parse_peers(duplicate_listing)
        self.assertEqual(context.exception.code, "claude.target_ambiguous")

    def test_agent_view_parser_rejects_ambiguous_json(self) -> None:
        duplicate_key = (
            b'[{"id":"00000000","cwd":"/example","kind":"interactive",'
            b'"startedAt":1,"sessionId":"00000000-0000-4000-8000-000000000102",'
            b'"name":"local-worker","name":"other-worker","state":"idle"}]'
        )
        nonfinite = (
            b'[{"id":"00000000","cwd":"/example","kind":"interactive",'
            b'"startedAt":NaN,"sessionId":"00000000-0000-4000-8000-000000000102",'
            b'"name":"local-worker","state":"idle"}]'
        )
        nul_path = json.dumps(
            [
                {
                    "id": "00000000",
                    "cwd": "/example/\x00project",
                    "kind": "interactive",
                    "startedAt": 1,
                    "sessionId": CLAUDE_SESSION,
                    "name": "local-worker",
                    "state": "idle",
                }
            ]
        ).encode()
        surrogate_path = json.dumps(
            [
                {
                    "id": "00000000",
                    "cwd": "/example/\ud800project",
                    "kind": "interactive",
                    "startedAt": 1,
                    "sessionId": CLAUDE_SESSION,
                    "name": "local-worker",
                    "state": "idle",
                }
            ]
        ).encode()
        for raw in (duplicate_key, nonfinite, nul_path, surrogate_path):
            with self.subTest(raw=raw):
                with self.assertRaises(cam1_transport.routing.RoutingError) as context:
                    cam1_transport.routing.parse_agent_view_sessions(raw)
                self.assertEqual(context.exception.code, "claude.agents_format")

    def test_notify_when_idle_requires_a_live_boolean_schema(self) -> None:
        self.assertTrue(
            cam1_transport._supports_notify_when_idle(
                {"properties": {"notify_when_idle": {"type": "boolean"}}}
            )
        )
        self.assertFalse(
            cam1_transport._supports_notify_when_idle(
                {"properties": {"notify_when_idle": {"type": "string"}}}
            )
        )
        self.assertFalse(cam1_transport._supports_notify_when_idle({}))


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
            with mock.patch.object(
                cam1_transport.subprocess, "run", return_value=receipt
            ) as run:
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


class ProjectBoundTransportTests(unittest.TestCase):
    """Exercise the supported journal-first live transport commands."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name).resolve()
        self.repo = self.base / "project"
        self.repo.mkdir(mode=0o700)
        subprocess.run(
            [project.DEFAULT_GIT_BIN, "-C", str(self.repo), "init", "--quiet"],
            check=True,
            capture_output=True,
            text=True,
        )
        self.state_root = self.base / "state"
        initialized = self.run_project("project", "init")
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        self.binding = project.resolve_project(
            self.repo,
            state_root=self.state_root,
            git_bin=project.DEFAULT_GIT_BIN,
        )
        self.fake_index = 0

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def project_command(self, *arguments: str) -> list[str]:
        return [
            sys.executable,
            str(PROJECT_CLI),
            "--project-root",
            str(self.repo),
            "--state-root",
            str(self.state_root),
            "--git-bin",
            project.DEFAULT_GIT_BIN,
            *arguments,
        ]

    def run_project(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            self.project_command(*arguments),
            cwd=self.repo,
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )

    def transport_command(
        self,
        *arguments: str,
        claude_bin: Path | None = None,
        codex_bin: Path | None = None,
        project_root: Path | None = None,
    ) -> list[str]:
        command = [
            sys.executable,
            str(TRANSPORT_CLI),
            *live_validation_cli_arguments(),
        ]
        if claude_bin is not None:
            command.extend(("--claude-bin", str(claude_bin)))
        if codex_bin is not None:
            command.extend(("--codex-bin", str(codex_bin)))
        command.extend(
            (
                "--project-root",
                str(project_root or self.repo),
                "--state-root",
                str(self.state_root),
                "--git-bin",
                project.DEFAULT_GIT_BIN,
                *arguments,
            )
        )
        return command

    def run_transport(
        self,
        *arguments: str,
        claude_bin: Path | None = None,
        codex_bin: Path | None = None,
        project_root: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            self.transport_command(
                *arguments,
                claude_bin=claude_bin,
                codex_bin=codex_bin,
                project_root=project_root,
            ),
            cwd=project_root or self.repo,
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )

    def add_claude_participant(self) -> None:
        added = self.run_project(
            "participant",
            "add",
            "--common-name",
            "local-worker",
            "--display-name",
            "Local Claude worker",
            "--role",
            "reviewer",
            "--vendor",
            "claude-code",
            "--participant-id",
            CLAUDE_PARTICIPANT,
        )
        bound = self.run_project(
            "participant",
            "bind",
            "--participant",
            "local-worker",
            "--session-id",
            CLAUDE_SESSION,
            "--session-label",
            "Local Claude worker",
            "--session-kind",
            "interactive",
            "--operator-reference",
            "test operator matched Claude status output",
        )
        self.assertEqual(added.returncode, 0, added.stderr)
        self.assertEqual(bound.returncode, 0, bound.stderr)

    def add_codex_participant(self) -> None:
        added = self.run_project(
            "participant",
            "add",
            "--common-name",
            "example-coordinator",
            "--display-name",
            "Local Codex coordinator",
            "--role",
            "coordinator",
            "--vendor",
            "codex",
            "--participant-id",
            CODEX_PARTICIPANT,
        )
        bound = self.run_project(
            "participant",
            "bind",
            "--participant",
            "example-coordinator",
            "--session-id",
            CODEX_THREAD,
            "--session-label",
            "Local Codex coordinator",
            "--operator-reference",
            "test operator copied the full Codex thread UUID",
        )
        self.assertEqual(added.returncode, 0, added.stderr)
        self.assertEqual(bound.returncode, 0, bound.stderr)

    def private_envelope(self, name: str, raw: bytes) -> Path:
        path = self.base / name
        write_private(path, raw)
        return path

    def fake_claude(
        self,
        *,
        returned: dict[str, object],
        cwd: Path | None = None,
        peer_name: str = "local-worker",
        peer_ref: str = "abcdef",
        expected_message: bytes | None = None,
        marker: Path | None = None,
        during_send_command: list[str] | None = None,
    ) -> Path:
        self.fake_index += 1
        executable = self.base / f"fake-claude-{self.fake_index}"
        expected_digest = (
            hashlib.sha256(expected_message).hexdigest()
            if expected_message is not None
            else None
        )
        source = textwrap.dedent(
            f"""\
            #!{sys.executable}
            import base64
            import hashlib
            import json
            import subprocess
            from pathlib import Path

            from mcp.server import MCPServer

            server = MCPServer("fake-claude")

            @server.tool()
            def ListAgents():
                return {{"listing": "Peer sessions (1):\\n  {peer_name} [{peer_ref}]  ·  interactive  ·  idle  ·  started now"}}

            @server.tool()
            def SendMessage(
                to: str,
                summary: str,
                message: str,
                notify_when_idle: bool = False,
            ):
                expected_digest = {expected_digest!r}
                if expected_digest is not None:
                    records = Path({str(self.binding.journal_path)!r}).read_text(encoding="utf-8").splitlines()
                    decoded = [json.loads(record) for record in records]
                    intents = [record for record in decoded if record.get("event_type") == "message.outbound.intent"]
                    if not intents:
                        raise RuntimeError("journal intent was not durable before SendMessage")
                    exact = base64.b64decode(intents[-1]["message"]["content"], validate=True)
                    if json.loads(exact)["type"] in {"hello", "challenge", "request", "cancel"} and not any(
                        record.get("event_type") == "state.lifecycle.root_registered"
                        and record.get("sequence", 0) > intents[-1].get("sequence", 0)
                        for record in decoded
                    ):
                        raise RuntimeError("provisional lifecycle root was not durable before SendMessage")
                    if hashlib.sha256(exact).hexdigest() != expected_digest:
                        raise RuntimeError("journaled intent did not preserve exact bytes")
                    if hashlib.sha256(message.encode("utf-8")).hexdigest() != expected_digest:
                        raise RuntimeError("SendMessage body differs from the journaled intent")
                marker = {str(marker) if marker is not None else None!r}
                if marker is not None:
                    marker_path = Path(marker)
                    prior = marker_path.read_text(encoding="utf-8") if marker_path.exists() else ""
                    marker_path.write_text(prior + "called\\n", encoding="utf-8")
                during_send_command = {during_send_command!r}
                if during_send_command is not None:
                    completed = subprocess.run(during_send_command, capture_output=True, text=True, timeout=10)
                    if completed.returncode != 0:
                        raise RuntimeError("concurrent ingest failed: " + completed.stderr)
                return {returned!r}

            server.run(transport="stdio")
            """
        )
        executable.write_text(
            with_agent_view(
                source,
                name=peer_name,
                cwd=str(cwd or self.repo),
            ),
            encoding="utf-8",
        )
        executable.chmod(0o700)
        return executable

    def preflight_and_confirm(self, claude_bin: Path) -> None:
        preflight = self.run_transport(
            "claude-preflight",
            "--participant",
            "local-worker",
            "--session-id",
            CLAUDE_SESSION,
            "--to",
            "local-worker [abcdef]",
            claude_bin=claude_bin,
        )
        self.assertEqual(preflight.returncode, 0, preflight.stderr)
        self.assertTrue(json.loads(preflight.stdout)["operator_correlation_required"])
        confirmed = self.run_project(
            "participant",
            "confirm-route",
            "--participant",
            "local-worker",
            "--expected-address",
            "local-worker [abcdef]",
            "--operator-reference",
            "test operator correlated Agent View with ListAgents",
        )
        self.assertEqual(confirmed.returncode, 0, confirmed.stderr)

    def _post_attempt_lock_failure_transaction(self):
        real_transaction = project.project_transaction
        calls = 0

        @contextmanager
        def selective_transaction(binding):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise project.ProjectError(
                    "transaction.busy", "injected post-attempt contention"
                )
            with real_transaction(binding) as transaction:
                yield transaction

        return selective_transaction

    def test_known_acceptance_survives_post_attempt_lock_contention(self) -> None:
        self.add_codex_participant()
        self.add_claude_participant()
        raw = cam1.build_hello(
            sender_vendor="claude-code",
            sender_name="local-worker",
            sender_session=CLAUDE_SESSION,
            recipient_vendor="codex",
            recipient_name="example-coordinator",
            recipient_session=CODEX_THREAD,
            reply_transport="claude_send_message",
            reply_address=CLAUDE_SESSION,
        )
        envelope = self.private_envelope("accepted-lock.cam1.json", raw)

        def accepted_send(**arguments):
            validated = cam1_transport._validate_envelope(
                arguments["envelope_path"], arguments["against_path"]
            )
            arguments["before_send"](validated)
            return {
                "ok": True,
                "status": "transport_accepted",
                "application_ack": False,
                "target_thread": CODEX_THREAD,
                "message_id": validated.envelope["message_id"],
                "transport_receipt": {
                    "queue_id": "00000000-0000-4000-8000-000000000901"
                },
            }

        with (
            mock.patch.object(
                cam1_transport.project,
                "project_transaction",
                self._post_attempt_lock_failure_transaction(),
            ),
            mock.patch.object(
                cam1_transport,
                "_send_to_codex_queue",
                side_effect=accepted_send,
            ),
            self.assertRaises(cam1_transport.TransportError) as context,
        ):
            cam1_transport.send_project_codex(
                self.binding,
                codex_bin="/not/executed/codex",
                participant_selector="example-coordinator",
                thread_guard=CODEX_THREAD,
                envelope_path=str(envelope),
                against_path=None,
                renewal_of=None,
                retry_after_intent=None,
                timeout_seconds=1,
                **live_validation_arguments(),
            )

        self.assertEqual(context.exception.code, "transport.acceptance_unjournaled")
        self.assertEqual(context.exception.audit["delivery_state"], "accepted")
        self.assertEqual(
            context.exception.audit["transport_receipt_id"],
            "00000000-0000-4000-8000-000000000901",
        )

    def test_unknown_outcome_survives_post_attempt_lock_contention(self) -> None:
        self.add_codex_participant()
        self.add_claude_participant()
        raw = cam1.build_hello(
            sender_vendor="claude-code",
            sender_name="local-worker",
            sender_session=CLAUDE_SESSION,
            recipient_vendor="codex",
            recipient_name="example-coordinator",
            recipient_session=CODEX_THREAD,
            reply_transport="claude_send_message",
            reply_address=CLAUDE_SESSION,
        )
        envelope = self.private_envelope("unknown-lock.cam1.json", raw)

        def failed_send(**arguments):
            validated = cam1_transport._validate_envelope(
                arguments["envelope_path"], arguments["against_path"]
            )
            arguments["before_send"](validated)
            raise cam1_transport.TransportError(
                "codex.queue_failed", "injected ambiguous queue failure"
            )

        with (
            mock.patch.object(
                cam1_transport.project,
                "project_transaction",
                self._post_attempt_lock_failure_transaction(),
            ),
            mock.patch.object(
                cam1_transport,
                "_send_to_codex_queue",
                side_effect=failed_send,
            ),
            self.assertRaises(cam1_transport.TransportError) as context,
        ):
            cam1_transport.send_project_codex(
                self.binding,
                codex_bin="/not/executed/codex",
                participant_selector="example-coordinator",
                thread_guard=CODEX_THREAD,
                envelope_path=str(envelope),
                against_path=None,
                renewal_of=None,
                retry_after_intent=None,
                timeout_seconds=1,
                **live_validation_arguments(),
            )

        self.assertEqual(context.exception.code, "transport.outcome_unjournaled")
        self.assertEqual(context.exception.audit["delivery_state"], "unknown")
        self.assertEqual(
            context.exception.audit["transport_error_code"], "codex.queue_failed"
        )

    def test_claude_route_requires_confirmation_then_send_is_fully_audited(
        self,
    ) -> None:
        self.add_claude_participant()
        self.add_codex_participant()
        raw = build_first_contact()
        envelope = self.private_envelope("hello.cam1.json", raw)
        marker = self.base / "claude-send.called"
        claude_bin = self.fake_claude(
            returned={
                "success": True,
                "msg_id": "00000000-0000-4000-8000-000000000900",
            },
            expected_message=raw,
            marker=marker,
        )

        preflight = self.run_transport(
            "claude-preflight",
            "--participant",
            "local-worker",
            "--session-id",
            CLAUDE_SESSION,
            "--to",
            "local-worker [abcdef]",
            claude_bin=claude_bin,
        )
        self.assertEqual(preflight.returncode, 0, preflight.stderr)
        preflight_payload = json.loads(preflight.stdout)
        self.assertTrue(preflight_payload["operator_correlation_required"])
        self.assertEqual(preflight_payload["participant"]["route_status"], "candidate")
        preflight_records = journal.replay_records(self.binding)
        self.assertEqual(
            preflight_records[-1]["event_type"], state.PARTICIPANT_ROUTE_OBSERVED
        )
        route_evidence = preflight_records[-1]["attributes"]
        self.assertEqual(route_evidence["agent_view_kind"], "interactive")
        self.assertEqual(route_evidence["agent_view_started_at_ms"], 1_784_241_375_111)
        self.assertEqual(
            route_evidence["session_git_top_level"], str(self.binding.git_top_level)
        )
        self.assertEqual(
            route_evidence["session_git_common_dir"],
            str(self.binding.git_common_dir),
        )
        self.assertNotIn("uds:", json.dumps(preflight_records[-1]))

        refused = self.run_transport(
            "claude-send",
            "--participant",
            "local-worker",
            "--envelope",
            str(envelope),
            claude_bin=claude_bin,
        )
        self.assertEqual(refused.returncode, 2)
        self.assertEqual(
            json.loads(refused.stderr)["error"]["code"], "roster.route_not_ready"
        )
        self.assertFalse(marker.exists())
        self.assertNotIn(
            "message.outbound.intent",
            [record["event_type"] for record in journal.replay_records(self.binding)],
        )

        confirmed = self.run_project(
            "participant",
            "confirm-route",
            "--participant",
            "local-worker",
            "--expected-address",
            "local-worker [abcdef]",
            "--operator-reference",
            "test operator confirmed fresh route",
        )
        self.assertEqual(confirmed.returncode, 0, confirmed.stderr)
        sent = self.run_transport(
            "claude-send",
            "--participant",
            "local-worker",
            "--session-id",
            CLAUDE_SESSION,
            "--to",
            "local-worker [abcdef]",
            "--envelope",
            str(envelope),
            claude_bin=claude_bin,
        )
        self.assertEqual(sent.returncode, 0, sent.stderr)
        payload = json.loads(sent.stdout)
        self.assertEqual(payload["status"], "transport_accepted")
        self.assertFalse(payload["application_ack"])
        self.assertEqual(payload["lifecycle"]["state"], "pending")
        self.assertTrue(marker.exists())

        records = journal.replay_records(self.binding)
        event_types = [record["event_type"] for record in records]
        intent_index = event_types.index("message.outbound.intent")
        accepted_index = event_types.index("transport.accepted")
        lifecycle_index = event_types.index(state.LIFECYCLE_ROOT_REGISTERED)
        self.assertLess(intent_index, lifecycle_index)
        self.assertLess(lifecycle_index, accepted_index)
        self.assertEqual(journal.decode_exact_message(records[intent_index]), raw)
        intent_attributes = records[intent_index]["attributes"]
        self.assertTrue(intent_attributes["validation_profile"]["available"])
        self.assertEqual(
            intent_attributes["dirty_validator_override"],
            dirty_validator_override_used(),
        )
        self.assertTrue(payload["validation_profile"]["available"])
        self.assertEqual(journal.decode_exact_message(records[lifecycle_index]), raw)
        self.assertEqual(
            state.StateStore(self.binding)
            .snapshot()
            .lifecycle.entries[payload["message_id"]]
            .state.value,
            "pending",
        )

        repeated = self.run_transport(
            "claude-send",
            "--participant",
            "local-worker",
            "--envelope",
            str(envelope),
            claude_bin=claude_bin,
        )
        self.assertEqual(repeated.returncode, 2, repeated.stderr)
        self.assertEqual(
            json.loads(repeated.stderr)["error"]["code"],
            "transport.already_accepted",
        )
        self.assertEqual(marker.read_text(encoding="utf-8").splitlines(), ["called"])
        self.assertEqual(
            sum(
                record["event_type"] == "message.outbound.intent"
                for record in journal.replay_records(self.binding)
            ),
            1,
        )

    def test_fast_application_ack_is_ingested_while_transport_is_in_flight(
        self,
    ) -> None:
        self.add_claude_participant()
        self.add_codex_participant()
        raw = build_first_contact()
        envelope = self.private_envelope("fast-hello.cam1.json", raw)
        ack = cam1.build_ack(
            raw,
            sender_vendor="claude-code",
            sender_name="local-worker",
            sender_session=CLAUDE_SESSION,
            reply_transport="claude_send_message",
            reply_address=CLAUDE_SESSION,
            status_value="received",
        )
        ack_path = self.private_envelope("fast-ack.cam1.json", ack)
        ingest_command = self.project_command(
            "message",
            "ingest",
            "--message",
            str(ack_path),
            "--as-participant",
            "example-coordinator",
        )
        claude_bin = self.fake_claude(
            returned={
                "success": True,
                "msg_id": "00000000-0000-4000-8000-000000000900",
            },
            expected_message=raw,
            during_send_command=ingest_command,
        )
        self.preflight_and_confirm(claude_bin)

        sent = self.run_transport(
            "claude-send",
            "--participant",
            "local-worker",
            "--envelope",
            str(envelope),
            claude_bin=claude_bin,
        )

        self.assertEqual(sent.returncode, 0, sent.stderr)
        payload = json.loads(sent.stdout)
        self.assertEqual(payload["lifecycle"]["state"], "handled")
        records = journal.replay_records(self.binding)
        event_types = [record["event_type"] for record in records]
        intent_index = event_types.index("message.outbound.intent")
        root_index = event_types.index(state.LIFECYCLE_ROOT_REGISTERED)
        inbound_index = event_types.index("message.inbound.validated")
        accepted_index = event_types.index("transport.accepted")
        self.assertLess(intent_index, root_index)
        self.assertLess(root_index, inbound_index)
        self.assertLess(inbound_index, accepted_index)
        self.assertEqual(
            sum(event == state.LIFECYCLE_ROOT_REGISTERED for event in event_types),
            1,
        )

        duplicate = self.run_project(
            "message",
            "ingest",
            "--message",
            str(ack_path),
            "--as-participant",
            "example-coordinator",
        )
        self.assertEqual(duplicate.returncode, 0, duplicate.stderr)
        self.assertEqual(json.loads(duplicate.stdout)["status"], "duplicate")

    def test_claude_product_failure_is_unknown_and_not_retriable(self) -> None:
        self.add_claude_participant()
        self.add_codex_participant()
        raw = build_first_contact()
        envelope = self.private_envelope("rejected.cam1.json", raw)
        marker = self.base / "rejected-send.called"
        claude_bin = self.fake_claude(
            returned={"success": False, "message": "refused"},
            expected_message=raw,
            marker=marker,
        )
        self.preflight_and_confirm(claude_bin)

        completed = self.run_transport(
            "claude-send",
            "--participant",
            "local-worker",
            "--envelope",
            str(envelope),
            claude_bin=claude_bin,
        )

        self.assertEqual(completed.returncode, 2)
        payload = json.loads(completed.stderr)
        self.assertEqual(payload["error"]["code"], "claude.send_failed")
        self.assertEqual(payload["audit"]["delivery_state"], "unknown")
        records = journal.replay_records(self.binding)
        self.assertEqual(
            [record["event_type"] for record in records[-3:]],
            [
                "message.outbound.intent",
                state.LIFECYCLE_ROOT_REGISTERED,
                "transport.not_accepted",
            ],
        )
        self.assertEqual(journal.decode_exact_message(records[-3]), raw)
        self.assertEqual(records[-1]["attributes"]["delivery_state"], "unknown")
        lifecycle_entries = state.StateStore(self.binding).snapshot().lifecycle.entries
        self.assertEqual(len(lifecycle_entries), 1)
        self.assertEqual(next(iter(lifecycle_entries.values())).state.value, "pending")

        repeated = self.run_transport(
            "claude-send",
            "--participant",
            "local-worker",
            "--retry-after-intent",
            records[-3]["record_id"],
            "--envelope",
            str(envelope),
            claude_bin=claude_bin,
        )
        self.assertEqual(repeated.returncode, 2, repeated.stderr)
        self.assertEqual(
            json.loads(repeated.stderr)["error"]["code"], "transport.retry_unsafe"
        )
        self.assertEqual(marker.read_text(encoding="utf-8").splitlines(), ["called"])

    def test_root_expiring_during_discovery_never_reaches_send_message(self) -> None:
        self.add_claude_participant()
        self.add_codex_participant()
        marker = self.base / "expired-race.called"
        claude_bin = self.fake_claude(
            returned={
                "success": True,
                "msg_id": "00000000-0000-4000-8000-000000000900",
            },
            marker=marker,
        )
        self.preflight_and_confirm(claude_bin)
        raw = cam1.build_hello(
            sender_vendor="codex",
            sender_name="example-coordinator",
            sender_session=CODEX_THREAD,
            recipient_vendor="claude-code",
            recipient_name="local-worker",
            recipient_session=CLAUDE_SESSION,
            reply_transport="codex_queue",
            reply_address=CODEX_THREAD,
            expires_in=60,
        )
        envelope = self.private_envelope("expiring.cam1.json", raw)
        expires_at = dt.datetime.fromisoformat(
            json.loads(raw)["expires_at"].replace("Z", "+00:00")
        )
        after_expiry = expires_at + dt.timedelta(seconds=1)
        after_expiry_text = after_expiry.isoformat().replace("+00:00", "Z")

        with (
            mock.patch.object(
                cam1_transport,
                "_utc_now",
                return_value=(after_expiry, after_expiry_text),
            ),
            self.assertRaises(cam1_transport.TransportError) as context,
        ):
            asyncio.run(
                cam1_transport.send_project_claude(
                    self.binding,
                    claude_bin=str(claude_bin),
                    participant_selector="local-worker",
                    session_id_guard=CLAUDE_SESSION,
                    target_guard="local-worker [abcdef]",
                    envelope_path=str(envelope),
                    against_path=None,
                    renewal_of=None,
                    retry_after_intent=None,
                    summary=None,
                    timeout_seconds=10,
                    **live_validation_arguments(),
                )
            )

        self.assertEqual(context.exception.code, "state.root_not_sendable")
        self.assertFalse(marker.exists())
        self.assertNotIn(
            "message.outbound.intent",
            [record["event_type"] for record in journal.replay_records(self.binding)],
        )
        self.assertEqual(
            state.StateStore(self.binding).snapshot().lifecycle.entries, {}
        )

    def test_root_expiring_during_intent_journaling_is_not_dispatched(self) -> None:
        self.add_claude_participant()
        self.add_codex_participant()
        marker = self.base / "expired-during-journal.called"
        claude_bin = self.fake_claude(
            returned={
                "success": True,
                "msg_id": "00000000-0000-4000-8000-000000000900",
            },
            marker=marker,
        )
        self.preflight_and_confirm(claude_bin)
        raw = cam1.build_hello(
            sender_vendor="codex",
            sender_name="example-coordinator",
            sender_session=CODEX_THREAD,
            recipient_vendor="claude-code",
            recipient_name="local-worker",
            recipient_session=CLAUDE_SESSION,
            reply_transport="codex_queue",
            reply_address=CODEX_THREAD,
            expires_in=60,
        )
        envelope = self.private_envelope("expires-while-journaling.json", raw)
        expires_at = dt.datetime.fromisoformat(
            json.loads(raw)["expires_at"].replace("Z", "+00:00")
        )
        before_expiry = expires_at - dt.timedelta(seconds=1)
        after_expiry = expires_at + dt.timedelta(seconds=1)

        def observed(value: dt.datetime) -> tuple[dt.datetime, str]:
            return value, value.isoformat().replace("+00:00", "Z")

        with (
            mock.patch.object(
                cam1_transport,
                "_utc_now",
                side_effect=[
                    observed(before_expiry),
                    observed(before_expiry),
                    observed(before_expiry),
                    observed(after_expiry),
                    observed(after_expiry),
                ],
            ),
            self.assertRaises(cam1_transport.TransportError) as context,
        ):
            asyncio.run(
                cam1_transport.send_project_claude(
                    self.binding,
                    claude_bin=str(claude_bin),
                    participant_selector="local-worker",
                    session_id_guard=CLAUDE_SESSION,
                    target_guard="local-worker [abcdef]",
                    envelope_path=str(envelope),
                    against_path=None,
                    renewal_of=None,
                    retry_after_intent=None,
                    summary=None,
                    timeout_seconds=10,
                    **live_validation_arguments(),
                )
            )

        self.assertEqual(
            context.exception.code,
            "state.observation_expired",
        )
        self.assertFalse(marker.exists())
        records = journal.replay_records(self.binding)
        self.assertEqual(
            [record["event_type"] for record in records[-3:]],
            [
                "message.outbound.intent",
                state.LIFECYCLE_ROOT_REGISTERED,
                "transport.not_accepted",
            ],
        )
        self.assertEqual(records[-1]["attributes"]["delivery_state"], "not_attempted")

    def test_unknown_claude_outcome_keeps_provisional_root_and_blocks_retry(
        self,
    ) -> None:
        self.add_claude_participant()
        self.add_codex_participant()
        raw = build_first_contact()
        envelope = self.private_envelope("unknown.cam1.json", raw)
        marker = self.base / "unknown-send.called"
        claude_bin = self.fake_claude(
            returned={"success": True, "message": "sent"},
            expected_message=raw,
            marker=marker,
        )
        self.preflight_and_confirm(claude_bin)

        completed = self.run_transport(
            "claude-send",
            "--participant",
            "local-worker",
            "--envelope",
            str(envelope),
            claude_bin=claude_bin,
        )

        self.assertEqual(completed.returncode, 2)
        payload = json.loads(completed.stderr)
        self.assertEqual(payload["error"]["code"], "claude.receipt_unrecognized")
        self.assertEqual(payload["audit"]["delivery_state"], "unknown")
        records = journal.replay_records(self.binding)
        self.assertEqual(records[-1]["event_type"], "transport.not_accepted")
        self.assertEqual(records[-1]["attributes"]["delivery_state"], "unknown")
        lifecycle_entries = state.StateStore(self.binding).snapshot().lifecycle.entries
        self.assertEqual(len(lifecycle_entries), 1)
        self.assertEqual(next(iter(lifecycle_entries.values())).state.value, "pending")

        intent_id = next(
            record["record_id"]
            for record in reversed(records)
            if record["event_type"] == "message.outbound.intent"
        )
        repeated = self.run_transport(
            "claude-send",
            "--participant",
            "local-worker",
            "--retry-after-intent",
            intent_id,
            "--envelope",
            str(envelope),
            claude_bin=claude_bin,
        )
        self.assertEqual(repeated.returncode, 2, repeated.stderr)
        self.assertEqual(
            json.loads(repeated.stderr)["error"]["code"], "transport.retry_unsafe"
        )
        self.assertEqual(marker.read_text(encoding="utf-8").splitlines(), ["called"])
        self.assertEqual(
            sum(
                record["event_type"] == "message.outbound.intent"
                for record in journal.replay_records(self.binding)
            ),
            1,
        )

        rewrapped = json.loads(raw)
        rewrapped["message_id"] = "00000000-0000-4000-8000-000000000998"
        rewrapped["nonce"] = "AAAAAAAAAAAAAAAAAAAAAA"
        rewrapped_path = self.private_envelope(
            "unknown-rewrapped.cam1.json", cam1.serialize_envelope(rewrapped)
        )
        bypass = self.run_transport(
            "claude-send",
            "--participant",
            "local-worker",
            "--envelope",
            str(rewrapped_path),
            claude_bin=claude_bin,
        )
        self.assertEqual(bypass.returncode, 2, bypass.stderr)
        self.assertEqual(
            json.loads(bypass.stderr)["error"]["code"],
            "lifecycle.idempotency_conflict",
        )
        self.assertEqual(marker.read_text(encoding="utf-8").splitlines(), ["called"])
        self.assertEqual(
            sum(
                record["event_type"] == "message.outbound.intent"
                for record in journal.replay_records(self.binding)
            ),
            1,
        )

    def test_orphaned_outbound_intent_blocks_automatic_rerun(self) -> None:
        self.add_claude_participant()
        self.add_codex_participant()
        raw = build_first_contact()
        envelope = self.private_envelope("orphaned.cam1.json", raw)
        marker = self.base / "orphaned-send.called"
        claude_bin = self.fake_claude(
            returned={
                "success": True,
                "msg_id": "00000000-0000-4000-8000-000000000900",
            },
            marker=marker,
        )
        self.preflight_and_confirm(claude_bin)
        message_id = json.loads(raw)["message_id"]
        orphaned = journal.append_record(
            self.binding,
            event_type="message.outbound.intent",
            exact_message=raw,
            attributes={"message_id": message_id, "simulated_crash": True},
        )

        completed = self.run_transport(
            "claude-send",
            "--participant",
            "local-worker",
            "--retry-after-intent",
            orphaned["record_id"],
            "--envelope",
            str(envelope),
            claude_bin=claude_bin,
        )

        self.assertEqual(completed.returncode, 2, completed.stderr)
        self.assertEqual(
            json.loads(completed.stderr)["error"]["code"], "transport.retry_unsafe"
        )
        self.assertFalse(marker.exists())
        self.assertEqual(
            sum(
                record["event_type"] == "message.outbound.intent"
                for record in journal.replay_records(self.binding)
            ),
            1,
        )

        rewrapped = json.loads(raw)
        rewrapped["message_id"] = "00000000-0000-4000-8000-000000000998"
        rewrapped["nonce"] = "AAAAAAAAAAAAAAAAAAAAAA"
        rewrapped_path = self.private_envelope(
            "orphaned-rewrapped.cam1.json", cam1.serialize_envelope(rewrapped)
        )
        bypass = self.run_transport(
            "claude-send",
            "--participant",
            "local-worker",
            "--envelope",
            str(rewrapped_path),
            claude_bin=claude_bin,
        )
        self.assertEqual(bypass.returncode, 2, bypass.stderr)
        self.assertEqual(
            json.loads(bypass.stderr)["error"]["code"],
            "transport.retry_requires_identical_envelope",
        )
        self.assertFalse(marker.exists())
        self.assertEqual(
            sum(
                record["event_type"] == "message.outbound.intent"
                for record in journal.replay_records(self.binding)
            ),
            1,
        )

    def test_proven_not_attempted_send_requires_exact_retry_intent(self) -> None:
        self.add_claude_participant()
        self.add_codex_participant()
        raw = build_first_contact()
        envelope = self.private_envelope("not-attempted.cam1.json", raw)
        marker = self.base / "not-attempted-send.called"
        claude_bin = self.fake_claude(
            returned={
                "success": True,
                "msg_id": "00000000-0000-4000-8000-000000000900",
            },
            expected_message=raw,
            marker=marker,
        )
        self.preflight_and_confirm(claude_bin)
        message_id = json.loads(raw)["message_id"]
        prior_intent = journal.append_record(
            self.binding,
            event_type="message.outbound.intent",
            exact_message=raw,
            attributes={"message_id": message_id, "simulated_pre_dispatch_stop": True},
        )
        journal.append_record(
            self.binding,
            event_type="transport.not_accepted",
            attributes={
                "intent_record_id": prior_intent["record_id"],
                "delivery_state": "not_attempted",
                "error_code": "transport.payload_too_large",
            },
        )

        unconfirmed = self.run_transport(
            "claude-send",
            "--participant",
            "local-worker",
            "--envelope",
            str(envelope),
            claude_bin=claude_bin,
        )
        self.assertEqual(unconfirmed.returncode, 2, unconfirmed.stderr)
        self.assertEqual(
            json.loads(unconfirmed.stderr)["error"]["code"],
            "transport.retry_confirmation_required",
        )
        self.assertFalse(marker.exists())

        retried = self.run_transport(
            "claude-send",
            "--participant",
            "local-worker",
            "--retry-after-intent",
            prior_intent["record_id"],
            "--envelope",
            str(envelope),
            claude_bin=claude_bin,
        )
        self.assertEqual(retried.returncode, 0, retried.stderr)
        self.assertEqual(marker.read_text(encoding="utf-8").splitlines(), ["called"])
        records = journal.replay_records(self.binding)
        retry_intents = [
            record
            for record in records
            if record["event_type"] == "message.outbound.intent"
        ]
        self.assertEqual(len(retry_intents), 2)
        self.assertEqual(
            retry_intents[-1]["attributes"]["retry_after_intent"],
            prior_intent["record_id"],
        )
        self.assertEqual(json.loads(retried.stdout)["lifecycle"]["state"], "pending")

    def test_codex_send_uses_roster_route_and_journals_before_queue(self) -> None:
        self.add_codex_participant()
        self.add_claude_participant()
        raw = cam1.build_hello(
            sender_vendor="claude-code",
            sender_name="local-worker",
            sender_session=CLAUDE_SESSION,
            recipient_vendor="codex",
            recipient_name="example-coordinator",
            recipient_session=CODEX_THREAD,
            reply_transport="claude_send_message",
            reply_address=CLAUDE_SESSION,
        )
        envelope = self.private_envelope("codex-hello.cam1.json", raw)
        marker = self.base / "codex-queue.called"
        fake_codex = self.base / "fake-codex"
        fake_codex.write_text(
            textwrap.dedent(
                f"""\
                #!{sys.executable}
                import base64
                import hashlib
                import json
                import sys
                from pathlib import Path

                records = Path({str(self.binding.journal_path)!r}).read_text(encoding="utf-8").splitlines()
                decoded = [json.loads(record) for record in records]
                intents = [record for record in decoded if record.get("event_type") == "message.outbound.intent"]
                if not intents:
                    raise SystemExit(8)
                if not any(
                    record.get("event_type") == "state.lifecycle.root_registered"
                    and record.get("sequence", 0) > intents[-1].get("sequence", 0)
                    for record in decoded
                ):
                    raise SystemExit(10)
                exact = base64.b64decode(intents[-1]["message"]["content"], validate=True)
                message = sys.argv[sys.argv.index("--message") + 1].encode("utf-8")
                if hashlib.sha256(exact).digest() != hashlib.sha256(message).digest():
                    raise SystemExit(9)
                Path({str(marker)!r}).write_text("called", encoding="utf-8")
                thread = sys.argv[sys.argv.index("--thread") + 1]
                print("Queued message 00000000-0000-4000-8000-000000000901 for thread " + thread + ".")
                """
            ),
            encoding="utf-8",
        )
        fake_codex.chmod(0o700)

        completed = self.run_transport(
            "codex-send",
            "--participant",
            "example-coordinator",
            "--thread",
            CODEX_THREAD,
            "--envelope",
            str(envelope),
            codex_bin=fake_codex,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["target_thread"], CODEX_THREAD)
        self.assertEqual(payload["lifecycle"]["state"], "pending")
        self.assertTrue(marker.exists())
        records = journal.replay_records(self.binding)
        event_types = [record["event_type"] for record in records]
        intent_index = event_types.index("message.outbound.intent")
        accepted_index = event_types.index("transport.accepted")
        lifecycle_index = event_types.index(state.LIFECYCLE_ROOT_REGISTERED)
        self.assertLess(intent_index, lifecycle_index)
        self.assertLess(lifecycle_index, accepted_index)
        self.assertEqual(journal.decode_exact_message(records[intent_index]), raw)

    def test_project_sends_reject_invalid_envelopes_before_intent_or_dispatch(
        self,
    ) -> None:
        self.add_claude_participant()
        self.add_codex_participant()
        marker = self.base / "invalid-envelope-product.called"
        product = self.base / "invalid-envelope-product"
        product.write_text(
            f"#!{sys.executable}\n"
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('called', encoding='utf-8')\n",
            encoding="utf-8",
        )
        product.chmod(0o700)

        to_claude = json.loads(build_first_contact())
        to_codex = json.loads(
            cam1.build_hello(
                sender_vendor="claude-code",
                sender_name="local-worker",
                sender_session=CLAUDE_SESSION,
                recipient_vendor="codex",
                recipient_name="example-coordinator",
                recipient_session=CODEX_THREAD,
                reply_transport="claude_send_message",
                reply_address=CLAUDE_SESSION,
            )
        )
        cases: list[tuple[str, str, dict[str, object], str]] = []
        for transport, valid in (("claude-send", to_claude), ("codex-send", to_codex)):
            schema_invalid = json.loads(json.dumps(valid))
            del schema_invalid["evidence"]
            cases.append((transport, "schema", schema_invalid, "schema.required"))

            semantic_invalid = json.loads(json.dumps(valid))
            semantic_invalid["reply_to"]["address"] = (
                "00000000-0000-4000-8000-000000000999"
            )
            cases.append(
                (
                    transport,
                    "semantic",
                    semantic_invalid,
                    "semantic.callback_identity",
                )
            )

        for index, (
            transport,
            invalidity,
            envelope_value,
            expected_problem,
        ) in enumerate(cases):
            with self.subTest(transport=transport, invalidity=invalidity):
                envelope = self.private_envelope(
                    f"invalid-{index}.cam1.json",
                    cam1.serialize_envelope(envelope_value),
                )
                if transport == "claude-send":
                    completed = self.run_transport(
                        transport,
                        "--participant",
                        "local-worker",
                        "--envelope",
                        str(envelope),
                        claude_bin=product,
                    )
                else:
                    completed = self.run_transport(
                        transport,
                        "--participant",
                        "example-coordinator",
                        "--thread",
                        CODEX_THREAD,
                        "--envelope",
                        str(envelope),
                        codex_bin=product,
                    )

                self.assertEqual(completed.returncode, 2, completed.stderr)
                error = json.loads(completed.stderr)
                self.assertEqual(error["error"]["code"], "envelope.invalid")
                self.assertIn(
                    expected_problem,
                    {problem["code"] for problem in error["error"]["problems"]},
                )
                self.assertFalse(marker.exists())
                self.assertNotIn(
                    "message.outbound.intent",
                    [
                        record["event_type"]
                        for record in journal.replay_records(self.binding)
                    ],
                )

    def test_dirty_validator_blocks_live_send_before_project_or_product(self) -> None:
        raw = cam1.build_hello(
            sender_vendor="claude-code",
            sender_name="local-worker",
            sender_session=CLAUDE_SESSION,
            recipient_vendor="codex",
            recipient_name="example-coordinator",
            recipient_session=CODEX_THREAD,
            reply_transport="claude_send_message",
            reply_address=CLAUDE_SESSION,
        )
        envelope = self.private_envelope("dirty-validator.cam1.json", raw)
        records_before = journal.replay_records(self.binding)

        with (
            mock.patch.object(
                cam1,
                "require_live_profile",
                side_effect=cam1.ValidationProfileError(
                    "profile.dirty_source", "synthetic dirty source"
                ),
            ),
            mock.patch.object(cam1_transport, "_resolve_binary") as resolve_binary,
            mock.patch.object(cam1_transport, "_resolve_project") as resolve_project,
            mock.patch.object(
                cam1_transport, "send_project_codex"
            ) as send_project_codex,
            mock.patch.object(cam1_transport, "_emit") as emit,
        ):
            returncode = cam1_transport.main(
                [
                    "--codex-bin",
                    "/synthetic/codex",
                    "--project-root",
                    str(self.repo),
                    "--state-root",
                    str(self.state_root),
                    "--git-bin",
                    project.DEFAULT_GIT_BIN,
                    "codex-send",
                    "--participant",
                    "example-coordinator",
                    "--thread",
                    CODEX_THREAD,
                    "--envelope",
                    str(envelope),
                ]
            )

        self.assertEqual(returncode, 2)
        resolve_binary.assert_not_called()
        resolve_project.assert_not_called()
        send_project_codex.assert_not_called()
        emit.assert_called_once()
        self.assertEqual(
            emit.call_args.args[0]["error"]["code"], "profile.dirty_source"
        )
        self.assertEqual(journal.replay_records(self.binding), records_before)

    def test_direct_send_functions_cannot_bypass_validation_source_guard(self) -> None:
        blocked = cam1.ValidationProfileError(
            "profile.dirty_source", "synthetic dirty source"
        )
        with (
            mock.patch.object(cam1, "require_live_profile", side_effect=blocked),
            mock.patch.object(cam1_transport.state, "StateStore") as state_store,
        ):
            with self.assertRaises(cam1_transport.TransportError) as codex_error:
                cam1_transport.send_project_codex(
                    self.binding,
                    codex_bin="/not/executed/codex",
                    participant_selector="example-coordinator",
                    thread_guard=CODEX_THREAD,
                    envelope_path="/not/read/envelope.json",
                    against_path=None,
                    renewal_of=None,
                    retry_after_intent=None,
                    timeout_seconds=1,
                )
            with self.assertRaises(cam1_transport.TransportError) as claude_error:
                asyncio.run(
                    cam1_transport.send_project_claude(
                        self.binding,
                        claude_bin="/not/executed/claude",
                        participant_selector="local-worker",
                        session_id_guard=CLAUDE_SESSION,
                        target_guard=None,
                        envelope_path="/not/read/envelope.json",
                        against_path=None,
                        renewal_of=None,
                        retry_after_intent=None,
                        summary=None,
                        timeout_seconds=1,
                    )
                )

        self.assertEqual(codex_error.exception.code, "profile.dirty_source")
        self.assertEqual(claude_error.exception.code, "profile.dirty_source")
        state_store.assert_not_called()

    def test_claude_root_to_codex_reply_returns_through_project_claude_send(
        self,
    ) -> None:
        self.add_claude_participant()
        self.add_codex_participant()
        root = cam1.build_hello(
            sender_vendor="claude-code",
            sender_name="local-worker",
            sender_session=CLAUDE_SESSION,
            recipient_vendor="codex",
            recipient_name="example-coordinator",
            recipient_session=CODEX_THREAD,
            reply_transport="claude_send_message",
            reply_address=CLAUDE_SESSION,
        )
        root_path = self.private_envelope("claude-root.cam1.json", root)
        codex_marker = self.base / "round-trip-codex.called"
        fake_codex = self.base / "round-trip-codex"
        fake_codex.write_text(
            textwrap.dedent(
                f"""\
                #!{sys.executable}
                import sys
                from pathlib import Path

                Path({str(codex_marker)!r}).write_text("called", encoding="utf-8")
                thread = sys.argv[sys.argv.index("--thread") + 1]
                print("Queued message 00000000-0000-4000-8000-000000000901 for thread " + thread + ".")
                """
            ),
            encoding="utf-8",
        )
        fake_codex.chmod(0o700)

        root_send = self.run_transport(
            "codex-send",
            "--participant",
            "example-coordinator",
            "--thread",
            CODEX_THREAD,
            "--envelope",
            str(root_path),
            codex_bin=fake_codex,
        )
        self.assertEqual(root_send.returncode, 0, root_send.stderr)
        self.assertTrue(codex_marker.exists())

        reply = cam1.build_ack(
            root,
            sender_vendor="codex",
            sender_name="example-coordinator",
            sender_session=CODEX_THREAD,
            reply_transport="codex_queue",
            reply_address=CODEX_THREAD,
            status_value="received",
        )
        reply_path = self.private_envelope("codex-reply.cam1.json", reply)
        claude_marker = self.base / "round-trip-claude.called"
        fake_claude = self.fake_claude(
            returned={
                "success": True,
                "msg_id": "00000000-0000-4000-8000-000000000902",
            },
            expected_message=reply,
            marker=claude_marker,
        )
        self.preflight_and_confirm(fake_claude)

        reply_send = self.run_transport(
            "claude-send",
            "--participant",
            "local-worker",
            "--session-id",
            CLAUDE_SESSION,
            "--to",
            "local-worker [abcdef]",
            "--envelope",
            str(reply_path),
            "--against",
            str(root_path),
            claude_bin=fake_claude,
        )

        self.assertEqual(reply_send.returncode, 0, reply_send.stderr)
        reply_result = json.loads(reply_send.stdout)
        self.assertEqual(reply_result["status"], "transport_accepted")
        self.assertEqual(reply_result["target_session_id"], CLAUDE_SESSION)
        self.assertEqual(
            json.loads(reply)["in_reply_to"], json.loads(root)["message_id"]
        )
        self.assertTrue(claude_marker.exists())
        intents = [
            record
            for record in journal.replay_records(self.binding)
            if record["event_type"] == "message.outbound.intent"
        ]
        self.assertEqual(len(intents), 2)
        self.assertEqual(journal.decode_exact_message(intents[0]), root)
        self.assertEqual(journal.decode_exact_message(intents[1]), reply)
        for intent in intents:
            self.assertTrue(intent["attributes"]["validation_profile"]["available"])
            self.assertEqual(
                intent["attributes"]["dirty_validator_override"],
                dirty_validator_override_used(),
            )

    def test_concurrent_competing_replies_reserve_one_transport_slot(self) -> None:
        self.add_codex_participant()
        self.add_claude_participant()
        now = dt.datetime.now(dt.UTC)
        root = cam1.build_request(
            sender_vendor="codex",
            sender_name="example-coordinator",
            sender_session=CODEX_THREAD,
            recipient_vendor="claude-code",
            recipient_name="local-worker",
            recipient_session=CLAUDE_SESSION,
            reply_transport="codex_queue",
            reply_address=CODEX_THREAD,
            risk_class="informational",
            operation="review_structure",
            intent="Request one local structure review",
            body="Review the project structure without making changes.",
            authorization_basis="none",
            now=now,
        )
        state.StateStore(self.binding).lifecycle_root(root, now=now)
        root_path = self.private_envelope("reserved-root.json", root)
        first_reply = cam1.build_ack(
            root,
            sender_vendor="claude-code",
            sender_name="local-worker",
            sender_session=CLAUDE_SESSION,
            reply_transport="claude_send_message",
            reply_address=CLAUDE_SESSION,
            status_value="accepted",
            now=now,
        )
        second_reply = cam1.build_ack(
            root,
            sender_vendor="claude-code",
            sender_name="local-worker",
            sender_session=CLAUDE_SESSION,
            reply_transport="claude_send_message",
            reply_address=CLAUDE_SESSION,
            status_value="rejected",
            now=now,
        )
        first_path = self.private_envelope("reserved-first.json", first_reply)
        second_path = self.private_envelope("reserved-second.json", second_reply)
        entered = self.base / "reserved-entered"
        release = self.base / "reserved-release"
        marker = self.base / "reserved-calls"
        fake_codex = self.base / "blocking-codex"
        fake_codex.write_text(
            textwrap.dedent(
                f"""\
                #!{sys.executable}
                import time
                from pathlib import Path

                marker = Path({str(marker)!r})
                with marker.open("a", encoding="utf-8") as stream:
                    stream.write("called\\n")
                Path({str(entered)!r}).write_text("entered", encoding="utf-8")
                deadline = time.monotonic() + 10
                while not Path({str(release)!r}).exists():
                    if time.monotonic() >= deadline:
                        raise SystemExit(7)
                    time.sleep(0.02)
                print("Queued message 00000000-0000-4000-8000-000000000901 for thread {CODEX_THREAD}.")
                """
            ),
            encoding="utf-8",
        )
        fake_codex.chmod(0o700)
        first = subprocess.Popen(
            self.transport_command(
                "codex-send",
                "--participant",
                "example-coordinator",
                "--thread",
                CODEX_THREAD,
                "--envelope",
                str(first_path),
                "--against",
                str(root_path),
                codex_bin=fake_codex,
            ),
            cwd=self.repo,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.monotonic() + 10
            while not entered.exists() and first.poll() is None:
                if time.monotonic() >= deadline:
                    self.fail("first reply transport did not reach its blocking point")
                time.sleep(0.02)

            competing = self.run_transport(
                "codex-send",
                "--participant",
                "example-coordinator",
                "--thread",
                CODEX_THREAD,
                "--envelope",
                str(second_path),
                "--against",
                str(root_path),
                codex_bin=fake_codex,
            )
            self.assertEqual(competing.returncode, 2, competing.stderr)
            self.assertEqual(
                json.loads(competing.stderr)["error"]["code"],
                "transport.reply_transition_reserved",
            )
        finally:
            release.write_text("release", encoding="utf-8")
            first_stdout, first_stderr = first.communicate(timeout=20)

        self.assertEqual(first.returncode, 0, first_stderr or first_stdout)
        self.assertEqual(marker.read_text(encoding="utf-8").splitlines(), ["called"])
        self.assertEqual(
            sum(
                record["event_type"] == "message.outbound.intent"
                for record in journal.replay_records(self.binding)
            ),
            1,
        )

    def test_uninitialized_project_cannot_bypass_required_journal(self) -> None:
        uninitialized = self.base / "uninitialized"
        uninitialized.mkdir(mode=0o700)
        subprocess.run(
            [project.DEFAULT_GIT_BIN, "-C", str(uninitialized), "init", "--quiet"],
            check=True,
            capture_output=True,
            text=True,
        )
        marker = self.base / "unbound-product.called"
        executable = self.base / "must-not-run"
        executable.write_text(
            f"#!{sys.executable}\nfrom pathlib import Path\nPath({str(marker)!r}).write_text('called')\n",
            encoding="utf-8",
        )
        executable.chmod(0o700)
        envelope = self.private_envelope("unbound.cam1.json", build_first_contact())

        completed = self.run_transport(
            "claude-send",
            "--participant",
            "local-worker",
            "--envelope",
            str(envelope),
            claude_bin=executable,
            project_root=uninitialized,
        )

        self.assertEqual(completed.returncode, 2)
        self.assertFalse(marker.exists())
        self.assertEqual(json.loads(completed.stderr)["error"]["code"], "path.missing")

    def test_agent_view_cwd_outside_project_is_rejected(self) -> None:
        self.add_claude_participant()
        outside = self.base / "different-project"
        outside.mkdir(mode=0o700)
        nested_unrelated = self.repo / "nested-unrelated"
        nested_unrelated.mkdir(mode=0o700)
        subprocess.run(
            [
                project.DEFAULT_GIT_BIN,
                "-C",
                str(nested_unrelated),
                "init",
                "--quiet",
            ],
            check=True,
            capture_output=True,
            text=True,
        )

        for cwd in (outside, nested_unrelated):
            with self.subTest(cwd=cwd):
                claude_bin = self.fake_claude(
                    returned={
                        "success": True,
                        "msg_id": "00000000-0000-4000-8000-000000000900",
                    },
                    cwd=cwd,
                )
                completed = self.run_transport(
                    "claude-preflight",
                    "--participant",
                    "local-worker",
                    claude_bin=claude_bin,
                )
                self.assertEqual(completed.returncode, 2, completed.stderr)
                self.assertEqual(
                    json.loads(completed.stderr)["error"]["code"],
                    "claude.project_mismatch",
                )
        self.assertEqual(
            [record["event_type"] for record in journal.replay_records(self.binding)],
            [state.PARTICIPANT_ADDED, state.PARTICIPANT_BOUND],
        )

    def test_wire_endpoints_must_match_bound_roster_before_send(self) -> None:
        self.add_claude_participant()
        self.add_codex_participant()
        marker = self.base / "endpoint-mismatch.called"
        claude_bin = self.fake_claude(
            returned={
                "success": True,
                "msg_id": "00000000-0000-4000-8000-000000000900",
            },
            marker=marker,
        )
        self.preflight_and_confirm(claude_bin)
        cases = (
            (
                "roster.sender_unknown",
                cam1.build_hello(
                    sender_vendor="codex",
                    sender_name="unknown-sender",
                    sender_session=CODEX_THREAD,
                    recipient_vendor="claude-code",
                    recipient_name="local-worker",
                    recipient_session=CLAUDE_SESSION,
                    reply_transport="codex_queue",
                    reply_address=CODEX_THREAD,
                ),
            ),
            (
                "roster.recipient_mismatch",
                cam1.build_hello(
                    sender_vendor="codex",
                    sender_name="example-coordinator",
                    sender_session=CODEX_THREAD,
                    recipient_vendor="claude-code",
                    recipient_name="wrong-worker",
                    recipient_session=CLAUDE_SESSION,
                    reply_transport="codex_queue",
                    reply_address=CODEX_THREAD,
                ),
            ),
        )

        for index, (expected_code, raw) in enumerate(cases):
            with self.subTest(expected_code=expected_code):
                envelope = self.private_envelope(f"mismatch-{index}.json", raw)
                completed = self.run_transport(
                    "claude-send",
                    "--participant",
                    "local-worker",
                    "--envelope",
                    str(envelope),
                    claude_bin=claude_bin,
                )
                self.assertEqual(completed.returncode, 2, completed.stderr)
                self.assertEqual(
                    json.loads(completed.stderr)["error"]["code"], expected_code
                )
        self.assertFalse(marker.exists())
        self.assertNotIn(
            "message.outbound.intent",
            [record["event_type"] for record in journal.replay_records(self.binding)],
        )

    def test_changed_claude_route_requires_fresh_operator_confirmation(self) -> None:
        self.add_claude_participant()
        self.add_codex_participant()
        initial = self.fake_claude(
            returned={
                "success": True,
                "msg_id": "00000000-0000-4000-8000-000000000900",
            }
        )
        self.preflight_and_confirm(initial)
        marker = self.base / "changed-route.called"
        changed = self.fake_claude(
            returned={
                "success": True,
                "msg_id": "00000000-0000-4000-8000-000000000901",
            },
            peer_name="renamed-worker",
            peer_ref="fedcba",
            marker=marker,
        )
        envelope = self.private_envelope("changed-route.json", build_first_contact())

        completed = self.run_transport(
            "claude-send",
            "--participant",
            "local-worker",
            "--envelope",
            str(envelope),
            claude_bin=changed,
        )

        self.assertEqual(completed.returncode, 2, completed.stderr)
        self.assertEqual(
            json.loads(completed.stderr)["error"]["code"], "roster.route_not_ready"
        )
        self.assertFalse(marker.exists())
        participant = (
            state.StateStore(self.binding).snapshot().roster.select("local-worker")
        )
        self.assertEqual(participant.route.status.value, "candidate")
        self.assertEqual(participant.route.address, "renamed-worker [fedcba]")
        self.assertNotIn(
            "message.outbound.intent",
            [record["event_type"] for record in journal.replay_records(self.binding)],
        )

    def test_optional_session_and_thread_guards_fail_before_product_call(self) -> None:
        self.add_claude_participant()
        self.add_codex_participant()
        marker = self.base / "guarded-product.called"
        executable = self.base / "guarded-product"
        executable.write_text(
            f"#!{sys.executable}\nfrom pathlib import Path\nPath({str(marker)!r}).write_text('called')\n",
            encoding="utf-8",
        )
        executable.chmod(0o700)
        claude_envelope = self.private_envelope(
            "guard-claude.json", build_first_contact()
        )
        codex_raw = cam1.build_hello(
            sender_vendor="claude-code",
            sender_name="local-worker",
            sender_session=CLAUDE_SESSION,
            recipient_vendor="codex",
            recipient_name="example-coordinator",
            recipient_session=CODEX_THREAD,
            reply_transport="claude_send_message",
            reply_address=CLAUDE_SESSION,
        )
        codex_envelope = self.private_envelope("guard-codex.json", codex_raw)
        wrong = "00000000-0000-4000-8000-000000000999"

        claude_result = self.run_transport(
            "claude-send",
            "--participant",
            "local-worker",
            "--session-id",
            wrong,
            "--envelope",
            str(claude_envelope),
            claude_bin=executable,
        )
        codex_result = self.run_transport(
            "codex-send",
            "--participant",
            "example-coordinator",
            "--thread",
            wrong,
            "--envelope",
            str(codex_envelope),
            codex_bin=executable,
        )

        self.assertEqual(claude_result.returncode, 2)
        self.assertEqual(codex_result.returncode, 2)
        self.assertEqual(
            json.loads(claude_result.stderr)["error"]["code"],
            "argument.session_id_mismatch",
        )
        self.assertEqual(
            json.loads(codex_result.stderr)["error"]["code"],
            "argument.thread_mismatch",
        )
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
