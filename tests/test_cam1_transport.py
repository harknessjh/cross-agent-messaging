# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest import mock

from tools import cam1, cam1_transport, cam1_transport_native
from tools.cam1lib import project, routing

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
            source_control=cam1.SourceControlState(
                "git",
                "b" * 40,
                False,
                profile_paths_match_head=True,
                profile_bytes_match_head=True,
                profile_index_flags_clean=True,
            ),
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


class ProjectBoundTransportTestCase(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
