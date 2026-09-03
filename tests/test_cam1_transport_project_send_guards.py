# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import unittest
from unittest import mock

from tools import cam1, cam1_transport
from tools.cam1lib import journal, project

if __package__:
    from .test_cam1_transport import (
        CLAUDE_SESSION,
        CODEX_THREAD,
        ProjectBoundTransportTestCase,
        build_first_contact,
    )
else:
    from test_cam1_transport import (
        CLAUDE_SESSION,
        CODEX_THREAD,
        ProjectBoundTransportTestCase,
        build_first_contact,
    )


class ProjectTransportSendGuardTests(ProjectBoundTransportTestCase):
    def test_codex_state_preflight_precedes_journal_and_product_invocation(
        self,
    ) -> None:
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
        envelope = self.private_envelope("codex-state-blocked.cam1.json", raw)
        marker = self.base / "codex-state-preflight-product.called"
        fake_product = self.approved_codex_bin
        fake_product.write_text(
            f"#!{sys.executable}\n"
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('called', encoding='utf-8')\n",
            encoding="utf-8",
        )
        fake_product.chmod(0o700)
        missing_state_home = self.base / "codex-home-without-state"
        missing_state_home.mkdir(mode=0o700)
        journal_before = self.binding.journal_path.read_bytes()

        completed = self.run_transport(
            "codex-send",
            "--participant",
            "example-coordinator",
            "--thread",
            CODEX_THREAD,
            "--envelope",
            str(envelope),
            codex_bin=fake_product,
            codex_home=missing_state_home,
        )

        self.assertEqual(completed.returncode, 2, completed.stderr)
        self.assertEqual(
            json.loads(completed.stderr)["error"]["code"],
            "codex.state_write_access",
        )
        self.assertEqual(self.binding.journal_path.read_bytes(), journal_before)
        self.assertFalse(marker.exists())

    def test_project_sends_reject_invalid_envelopes_before_intent_or_dispatch(
        self,
    ) -> None:
        self.add_claude_participant()
        self.add_codex_participant()
        marker = self.base / "invalid-envelope-product.called"
        for product in (self.approved_claude_bin, self.approved_codex_bin):
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
                        claude_bin=self.approved_claude_bin,
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
                        codex_bin=self.approved_codex_bin,
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
            "profile.path_set_mismatch", "synthetic profile path mismatch"
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

        self.assertEqual(codex_error.exception.code, "profile.path_set_mismatch")
        self.assertEqual(claude_error.exception.code, "profile.path_set_mismatch")
        state_store.assert_not_called()

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
        self.preflight_tool_correlated_route(claude_bin)
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

    def test_optional_session_and_thread_guards_fail_before_product_call(self) -> None:
        self.add_claude_participant()
        self.add_codex_participant()
        marker = self.base / "guarded-product.called"
        for executable in (self.approved_claude_bin, self.approved_codex_bin):
            executable.write_text(
                f"#!{sys.executable}\n"
                "from pathlib import Path\n"
                f"Path({str(marker)!r}).write_text('called')\n",
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
            claude_bin=self.approved_claude_bin,
        )
        codex_result = self.run_transport(
            "codex-send",
            "--participant",
            "example-coordinator",
            "--thread",
            wrong,
            "--envelope",
            str(codex_envelope),
            codex_bin=self.approved_codex_bin,
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
