# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import datetime as dt
import json
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tools.cam1lib import builders, journal, project, state

ROOT = Path(__file__).resolve().parents[1]
PROJECT_TOOL = ROOT / "tools" / "cam1_project.py"
CODEX_PARTICIPANT = "00000000-0000-4000-8000-000000000101"
CLAUDE_PARTICIPANT = "00000000-0000-4000-8000-000000000102"
CODEX_SESSION = "00000000-0000-4000-8000-000000000201"
CLAUDE_SESSION = "00000000-0000-4000-8000-000000000202"


class StdinCaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name).resolve()
        self.repo = self.base / "project"
        self.repo.mkdir(mode=0o700)
        subprocess.run(
            [project.DEFAULT_GIT_BIN, "-C", str(self.repo), "init", "--quiet"],
            check=True,
        )
        self.state_root = self.base / "state"
        self.binding = project.initialize_project(
            self.repo,
            state_root=self.state_root,
            now=dt.datetime.now(dt.UTC),
        )
        self.capture_dir = self.base / "captures"
        self.capture_dir.mkdir(mode=0o700)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def command(self, *arguments: str) -> list[str]:
        return [
            sys.executable,
            str(PROJECT_TOOL),
            "--project-root",
            str(self.repo),
            "--state-root",
            str(self.state_root),
            *arguments,
        ]

    def run_tool(
        self, *arguments: str, input_bytes: bytes | None = None
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            self.command(*arguments),
            input=input_bytes,
            check=False,
            capture_output=True,
            timeout=30,
        )

    def bind_participants(self) -> None:
        store = state.StateStore(self.binding)
        observed = dt.datetime.now(dt.UTC)
        observed_at = observed.isoformat(timespec="microseconds").replace("+00:00", "Z")
        for participant_id, common_name, vendor, session_id in (
            (CODEX_PARTICIPANT, "coordinator", "codex", CODEX_SESSION),
            (CLAUDE_PARTICIPANT, "reviewer", "claude-code", CLAUDE_SESSION),
        ):
            store.participant_add(
                participant_id=participant_id,
                common_name=common_name,
                display_name=common_name.title(),
                role="CAM test participant",
                vendor=vendor,
                now=observed,
            )
            store.participant_bind(
                common_name,
                session_id=session_id,
                session_label=f"{common_name}-session",
                session_kind="interactive",
                operator_reference="test operator correlation",
                bound_at=observed_at,
                now=observed,
            )

    def test_stdin_capture_preserves_exact_valid_bytes(self) -> None:
        self.bind_participants()
        canonical = builders.build_request(
            sender_vendor="codex",
            sender_name="coordinator",
            sender_session=CODEX_SESSION,
            recipient_vendor="claude-code",
            recipient_name="reviewer",
            recipient_session=CLAUDE_SESSION,
            reply_transport="codex_queue",
            reply_address=CODEX_SESSION,
            risk_class="informational",
            operation="review_unicode",
            intent="Review exact captured serialization",
            body='Review café ☕, the "quoted" value, and this line\nbreak.',
            authorization_basis="none",
            now=dt.datetime.now(dt.UTC),
        )
        envelope = json.loads(canonical)
        raw = (
            " \n" + json.dumps(envelope, ensure_ascii=False, indent=2) + "\n"
        ).encode()
        capture_path = self.capture_dir / "valid-request.cam1.json"

        result = self.run_tool(
            "message",
            "ingest",
            "--stdin",
            "--capture-to",
            str(capture_path),
            "--as-participant",
            "reviewer",
            input_bytes=raw,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(capture_path.read_bytes(), raw)
        self.assertEqual(stat.S_IMODE(capture_path.stat().st_mode), 0o600)
        observed = [
            record
            for record in journal.replay_records(self.binding)
            if record["event_type"] == "message.inbound.observed"
        ]
        self.assertEqual(len(observed), 1)
        self.assertEqual(journal.decode_exact_message(observed[0]), raw)
        self.assertEqual(observed[0]["attributes"]["source"], "binary_stdin_capture")

    def test_malformed_stdin_is_saved_and_journaled_before_rejection(self) -> None:
        raw = b' \n{"protocol":"CAM/1","body":"caf\xc3\xa9\\n",not-json}\n'
        capture_path = self.capture_dir / "malformed.cam1.json"

        result = self.run_tool(
            "message",
            "ingest",
            "--stdin",
            "--capture-to",
            str(capture_path),
            "--as-participant",
            "reviewer",
            input_bytes=raw,
        )

        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(capture_path.read_bytes(), raw)
        records = journal.replay_records(self.binding)
        self.assertEqual(
            [record["event_type"] for record in records],
            ["message.inbound.observed", "message.inbound.rejected"],
        )
        self.assertEqual(journal.decode_exact_message(records[0]), raw)

    def test_existing_file_and_symlink_are_never_replaced(self) -> None:
        existing = self.capture_dir / "existing.cam1.json"
        existing.write_bytes(b"original")
        existing.chmod(0o600)
        target = self.capture_dir / "target.cam1.json"
        target.write_bytes(b"target")
        target.chmod(0o600)
        link = self.capture_dir / "link.cam1.json"
        link.symlink_to(target)

        for capture_path in (existing, link):
            with self.subTest(capture_path=capture_path.name):
                result = self.run_tool(
                    "message",
                    "ingest",
                    "--stdin",
                    "--capture-to",
                    str(capture_path),
                    "--as-participant",
                    "reviewer",
                    input_bytes=b"not-used",
                )
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(
                    json.loads(result.stderr)["error"]["code"], "state.create"
                )

        self.assertEqual(existing.read_bytes(), b"original")
        self.assertTrue(link.is_symlink())
        self.assertEqual(target.read_bytes(), b"target")
        self.assertEqual(journal.verify_journal(self.binding).record_count, 0)

    def test_capture_rejects_symlinked_ancestor_without_redirecting_output(
        self,
    ) -> None:
        real_parent = self.base / "real-capture"
        nested = real_parent / "nested"
        nested.mkdir(parents=True, mode=0o700)
        alias = self.base / "capture-alias"
        alias.symlink_to(real_parent, target_is_directory=True)
        capture_path = alias / "nested" / "redirected.cam1.json"

        result = self.run_tool(
            "message",
            "ingest",
            "--stdin",
            "--capture-to",
            str(capture_path),
            "--as-participant",
            "reviewer",
            input_bytes=b"not-json",
        )

        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(json.loads(result.stderr)["error"]["code"], "path.symlink")
        self.assertFalse(nested.joinpath("redirected.cam1.json").exists())
        self.assertEqual(journal.verify_journal(self.binding).record_count, 0)

        safe_parent = self.base / "safe-capture"
        safe_parent.mkdir(mode=0o700)
        erased_symlink = alias / ".." / safe_parent.name / "erased-link.cam1.json"
        erased_result = self.run_tool(
            "message",
            "ingest",
            "--stdin",
            "--capture-to",
            str(erased_symlink),
            "--as-participant",
            "reviewer",
            input_bytes=b"not-json",
        )
        self.assertEqual(erased_result.returncode, 2, erased_result.stderr)
        self.assertEqual(
            json.loads(erased_result.stderr)["error"]["code"], "path.component"
        )
        self.assertFalse(safe_parent.joinpath("erased-link.cam1.json").exists())
        self.assertEqual(journal.verify_journal(self.binding).record_count, 0)

    def test_empty_and_oversize_stdin_create_no_artifact(self) -> None:
        cases = (
            ("empty", b"", "message.stdin_empty"),
            (
                "oversize",
                b"x" * (journal.MAX_EXACT_MESSAGE_BYTES + 1),
                "message.stdin_size",
            ),
        )
        for name, raw, error_code in cases:
            with self.subTest(name=name):
                capture_path = self.capture_dir / f"{name}.cam1.json"
                result = self.run_tool(
                    "message",
                    "ingest",
                    "--stdin",
                    "--capture-to",
                    str(capture_path),
                    "--as-participant",
                    "reviewer",
                    input_bytes=raw,
                )
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(json.loads(result.stderr)["error"]["code"], error_code)
                self.assertFalse(capture_path.exists())

        self.assertEqual(journal.verify_journal(self.binding).record_count, 0)

    def test_help_and_source_argument_validation(self) -> None:
        help_result = self.run_tool("message", "ingest", "--help")
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn(b"--stdin", help_result.stdout)
        self.assertIn(b"--capture-to", help_result.stdout)

        invalid_arguments = (
            ("--as-participant", "reviewer"),
            (
                "--message",
                str(self.capture_dir / "input.cam1.json"),
                "--stdin",
                "--capture-to",
                str(self.capture_dir / "both.cam1.json"),
                "--as-participant",
                "reviewer",
            ),
            ("--stdin", "--as-participant", "reviewer"),
            (
                "--stdin",
                "--capture-to",
                "relative.cam1.json",
                "--as-participant",
                "reviewer",
            ),
            (
                "--message",
                str(self.capture_dir / "input.cam1.json"),
                "--capture-to",
                str(self.capture_dir / "invalid.cam1.json"),
                "--as-participant",
                "reviewer",
            ),
        )
        for arguments in invalid_arguments:
            with self.subTest(arguments=arguments):
                result = self.run_tool("message", "ingest", *arguments)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(
                    json.loads(result.stderr)["error"]["code"], "argument.invalid"
                )


if __name__ == "__main__":
    unittest.main()
