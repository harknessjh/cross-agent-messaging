# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import datetime as dt
import json
import sys
import unittest

from tools import cam1
from tools.cam1lib import journal, state

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


class ProjectTransportRosterTests(ProjectBoundTransportTestCase):
    def test_stale_claude_binding_fails_before_product_io(self) -> None:
        self.add_claude_participant()
        store = state.StateStore(self.binding)
        store.participant_invalidate("local-worker", reason="identity is questionable")
        marker = self.base / "stale-participant-product.called"
        self.approved_claude_bin.write_text(
            f"#!{sys.executable}\n"
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('called', encoding='utf-8')\n",
            encoding="utf-8",
        )
        self.approved_claude_bin.chmod(0o700)
        records_before = self.binding.journal_path.read_bytes()

        rejected = self.run_transport(
            "claude-preflight",
            "--participant",
            "local-worker",
            claude_bin=self.approved_claude_bin,
        )

        self.assertEqual(rejected.returncode, 2, rejected.stderr)
        self.assertEqual(
            json.loads(rejected.stderr)["error"]["code"],
            "roster.participant_stale",
        )
        self.assertFalse(marker.exists())
        self.assertEqual(self.binding.journal_path.read_bytes(), records_before)

    def test_binding_metadata_requirements_are_vendor_specific(self) -> None:
        store = state.StateStore(self.binding)
        event_now = dt.datetime.now(dt.UTC)
        bound_at = event_now.isoformat(timespec="microseconds").replace("+00:00", "Z")
        store.participant_add(
            common_name="local-worker",
            display_name="Local Claude worker",
            role="reviewer",
            vendor="claude-code",
            approved_product_executable=str(self.approved_claude_bin),
            now=event_now,
        )

        with self.assertRaises(cam1.CamUsageError) as missing_label:
            store.participant_bind(
                "local-worker",
                session_id=CLAUDE_SESSION,
                session_label=None,
                session_kind="interactive",
                operator_reference="synthetic incomplete binding",
                bound_at=bound_at,
                now=event_now,
            )

        self.assertEqual(missing_label.exception.code, "roster.session_label_required")

        with self.assertRaises(cam1.CamUsageError) as missing_kind:
            store.participant_bind(
                "local-worker",
                session_id=CLAUDE_SESSION,
                session_label="local-worker",
                session_kind=None,
                operator_reference="synthetic incomplete binding",
                bound_at=bound_at,
                now=event_now,
            )
        self.assertEqual(missing_kind.exception.code, "roster.session_kind_required")

        store.participant_add(
            common_name="example-coordinator",
            display_name="Codex coordinator",
            role=None,
            vendor="codex",
            approved_product_executable=str(self.approved_codex_bin),
            now=event_now,
        )
        codex = store.participant_bind(
            "example-coordinator",
            session_id=CODEX_THREAD,
            session_label=None,
            session_kind=None,
            operator_reference="Codex UUID confirmed",
            bound_at=bound_at,
            now=event_now,
        )
        self.assertIsNone(codex.binding.session_label)
        self.assertIsNone(codex.binding.session_kind)
        self.assertEqual(
            [record["event_type"] for record in journal.replay_records(self.binding)],
            [
                state.PARTICIPANT_ADDED,
                state.PARTICIPANT_ADDED,
                state.PARTICIPANT_BOUND,
            ],
        )

    def test_missing_approved_executables_fail_before_product_io(self) -> None:
        store = state.StateStore(self.binding)
        event_now = dt.datetime.now(dt.UTC)
        bound_at = event_now.isoformat(timespec="microseconds").replace("+00:00", "Z")
        store.participant_add(
            common_name="local-worker",
            display_name="Legacy Claude worker",
            role=None,
            vendor="claude-code",
            now=event_now,
        )
        store.participant_bind(
            "local-worker",
            session_id=CLAUDE_SESSION,
            session_label="local-worker",
            session_kind="interactive",
            operator_reference="historical binding",
            bound_at=bound_at,
            now=event_now,
        )
        store.participant_add(
            common_name="example-coordinator",
            display_name="Legacy Codex coordinator",
            role=None,
            vendor="codex",
            now=event_now,
        )
        store.participant_bind(
            "example-coordinator",
            session_id=CODEX_THREAD,
            session_label=None,
            session_kind=None,
            operator_reference="historical binding",
            bound_at=bound_at,
            now=event_now,
        )
        marker = self.base / "missing-approved-product.called"
        claude_bin = self.fake_claude(returned={"success": True}, marker=marker)
        self.approved_codex_bin.write_text(
            f"#!{sys.executable}\n"
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('called', encoding='utf-8')\n",
            encoding="utf-8",
        )
        self.approved_codex_bin.chmod(0o700)
        to_claude = self.private_envelope(
            "missing-approved-to-claude.json", build_first_contact()
        )
        to_codex = self.private_envelope(
            "missing-approved-to-codex.json",
            cam1.build_hello(
                sender_vendor="claude-code",
                sender_name="local-worker",
                sender_session=CLAUDE_SESSION,
                recipient_vendor="codex",
                recipient_name="example-coordinator",
                recipient_session=CODEX_THREAD,
                reply_transport="claude_send_message",
                reply_address=CLAUDE_SESSION,
            ),
        )
        records_before = self.binding.journal_path.read_bytes()

        rejected = (
            self.run_transport(
                "claude-preflight",
                "--participant",
                "local-worker",
                claude_bin=claude_bin,
            ),
            self.run_transport(
                "claude-send",
                "--participant",
                "local-worker",
                "--envelope",
                str(to_claude),
                claude_bin=claude_bin,
            ),
            self.run_transport(
                "codex-send",
                "--participant",
                "example-coordinator",
                "--thread",
                CODEX_THREAD,
                "--envelope",
                str(to_codex),
                codex_bin=self.approved_codex_bin,
            ),
        )

        for result in rejected:
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(
                json.loads(result.stderr)["error"]["code"],
                "roster.product_executable_missing",
            )
        self.assertFalse(marker.exists())
        self.assertEqual(self.binding.journal_path.read_bytes(), records_before)

    def test_mismatched_approved_executables_fail_before_product_io(self) -> None:
        self.add_claude_participant()
        self.add_codex_participant()
        marker = self.base / "mismatched-product.called"
        other_claude = self.base / "other-claude"
        other_codex = self.base / "other-codex"
        for executable in (other_claude, other_codex):
            executable.write_text(
                f"#!{sys.executable}\n"
                "from pathlib import Path\n"
                f"Path({str(marker)!r}).write_text('called', encoding='utf-8')\n",
                encoding="utf-8",
            )
            executable.chmod(0o700)
        to_claude = self.private_envelope(
            "mismatched-to-claude.json", build_first_contact()
        )
        to_codex = self.private_envelope(
            "mismatched-to-codex.json",
            cam1.build_hello(
                sender_vendor="claude-code",
                sender_name="local-worker",
                sender_session=CLAUDE_SESSION,
                recipient_vendor="codex",
                recipient_name="example-coordinator",
                recipient_session=CODEX_THREAD,
                reply_transport="claude_send_message",
                reply_address=CLAUDE_SESSION,
            ),
        )
        records_before = self.binding.journal_path.read_bytes()

        rejected_claude = self.run_transport(
            "claude-preflight",
            "--participant",
            "local-worker",
            claude_bin=other_claude,
        )
        rejected_claude_send = self.run_transport(
            "claude-send",
            "--participant",
            "local-worker",
            "--envelope",
            str(to_claude),
            claude_bin=other_claude,
        )
        rejected_codex = self.run_transport(
            "codex-send",
            "--participant",
            "example-coordinator",
            "--thread",
            CODEX_THREAD,
            "--envelope",
            str(to_codex),
            codex_bin=other_codex,
        )

        for rejected in (rejected_claude, rejected_claude_send, rejected_codex):
            self.assertEqual(rejected.returncode, 2, rejected.stderr)
            self.assertEqual(
                json.loads(rejected.stderr)["error"]["code"],
                "roster.product_executable_mismatch",
            )
        self.assertFalse(marker.exists())
        self.assertEqual(self.binding.journal_path.read_bytes(), records_before)

    def test_kindless_legacy_claude_binding_fails_before_discovery(self) -> None:
        store = state.StateStore(self.binding)
        event_now = dt.datetime.now(dt.UTC)
        bound_at = event_now.isoformat(timespec="microseconds").replace("+00:00", "Z")
        store.participant_add(
            common_name="local-worker",
            display_name="Local Claude worker",
            role="reviewer",
            vendor="claude-code",
            approved_product_executable=str(self.approved_claude_bin),
            now=event_now,
        )
        participant = store.snapshot().roster.select("local-worker")
        journal.append_record(
            self.binding,
            event_type=state.PARTICIPANT_BOUND,
            attributes={
                "participant_id": participant.participant_id,
                "session_id": CLAUDE_SESSION,
                "session_label": "local-worker",
                "session_kind": None,
                "operator_reference": "historical binding without session kind",
                "bound_at": bound_at,
            },
            now=event_now,
        )
        marker = self.base / "kindless-binding.called"
        claude_bin = self.fake_claude(
            returned={
                "success": True,
                "msg_id": "00000000-0000-4000-8000-000000000901",
            },
            marker=marker,
        )
        envelope = self.private_envelope("kindless.json", build_first_contact())

        for arguments in (
            ("claude-preflight", "--participant", "local-worker"),
            (
                "claude-send",
                "--participant",
                "local-worker",
                "--envelope",
                str(envelope),
            ),
        ):
            with self.subTest(command=arguments[0]):
                rejected = self.run_transport(*arguments, claude_bin=claude_bin)
                self.assertEqual(rejected.returncode, 2, rejected.stderr)
                self.assertEqual(
                    json.loads(rejected.stderr)["error"]["code"],
                    "claude.binding_incomplete",
                )

        self.assertFalse(marker.exists())
        records = journal.replay_records(self.binding)
        self.assertEqual(
            [record["event_type"] for record in records],
            [state.PARTICIPANT_ADDED, state.PARTICIPANT_BOUND],
        )


if __name__ == "__main__":
    unittest.main()
