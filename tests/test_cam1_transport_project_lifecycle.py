# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
import textwrap
import time
import unittest

from tools import cam1
from tools.cam1lib import journal, state

if __package__:
    from .test_cam1_transport import (
        CLAUDE_SESSION,
        CODEX_THREAD,
        ProjectBoundTransportTestCase,
        dirty_validator_override_used,
    )
else:
    from test_cam1_transport import (
        CLAUDE_SESSION,
        CODEX_THREAD,
        ProjectBoundTransportTestCase,
        dirty_validator_override_used,
    )


class ProjectTransportLifecycleTests(ProjectBoundTransportTestCase):
    def test_expired_not_attempted_reply_can_be_rebuilt_with_fresh_identity(
        self,
    ) -> None:
        self._check_rebuilt_expired_reply("not_attempted")

    def test_expired_unknown_reply_still_reserves_slot_against_fresh_identity(
        self,
    ) -> None:
        self._check_rebuilt_expired_reply("unknown")

    def test_expired_orphaned_reply_still_reserves_slot_against_fresh_identity(
        self,
    ) -> None:
        self._check_rebuilt_expired_reply(None)

    def _check_rebuilt_expired_reply(self, delivery_state: str | None) -> None:
        self.add_codex_participant()
        self.add_claude_participant()
        now = dt.datetime.now(dt.UTC)
        root_time = now - dt.timedelta(minutes=5)
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
            expires_in=120,
            now=root_time,
        )
        sender = {
            "sender_vendor": "claude-code",
            "sender_name": "local-worker",
            "sender_session": CLAUDE_SESSION,
            "reply_transport": "claude_send_message",
            "reply_address": CLAUDE_SESSION,
        }
        accepted_time = root_time + dt.timedelta(seconds=1)
        accepted = cam1.build_ack(
            root, **sender, status_value="accepted", now=accepted_time
        )
        store = state.StateStore(self.binding)
        store.lifecycle_root(root, now=root_time)
        store.lifecycle_reply(accepted, now=accepted_time)
        stale_time = root_time + dt.timedelta(seconds=30)
        stale = cam1.build_result(
            root, **sender, body="Review complete.", expires_in=60, now=stale_time
        )
        stale_message = json.loads(stale)
        # Seed only audit evidence, not a committed result or a product send.
        intent = journal.append_record(
            self.binding,
            event_type="message.outbound.intent",
            exact_message=stale,
            attributes={"message_id": stale_message["message_id"]},
            now=stale_time,
        )
        if delivery_state is not None:
            journal.append_record(
                self.binding,
                event_type="transport.not_accepted",
                attributes={
                    "intent_record_id": intent["record_id"],
                    "delivery_state": delivery_state,
                },
                now=stale_time + dt.timedelta(seconds=1),
            )
        with self.assertRaises(cam1.CamValidationError):
            cam1.validate_exact_bytes(stale, against_raw=root, now=now)

        fresh = cam1.build_result(root, **sender, body="Review complete.", now=now)
        fresh_message = json.loads(fresh)
        self.assertNotEqual(fresh_message["message_id"], stale_message["message_id"])
        self.assertNotEqual(
            fresh_message["action"]["idempotency_key"],
            stale_message["action"]["idempotency_key"],
        )
        self.assertEqual(fresh_message["in_reply_to"], stale_message["in_reply_to"])
        cam1.validate_exact_bytes(fresh, against_raw=root, now=now)
        root_path = self.private_envelope("fresh-reply-root.json", root)
        fresh_path = self.private_envelope("fresh-reply.json", fresh)
        marker = self.base / "fresh-reply-called"
        self.approved_codex_bin.write_text(
            f"#!{sys.executable}\nfrom pathlib import Path\n"
            f"with Path({str(marker)!r}).open('a') as stream: stream.write('called\\n')\n"
            "print('Queued message 00000000-0000-4000-8000-000000000903 "
            f"for thread {CODEX_THREAD}.')\n",
            encoding="utf-8",
        )
        self.approved_codex_bin.chmod(0o700)
        result = self.run_transport(
            "codex-send",
            "--participant",
            "example-coordinator",
            "--envelope",
            str(fresh_path),
            "--against",
            str(root_path),
            codex_bin=self.approved_codex_bin,
        )
        intents = [
            record
            for record in journal.replay_records(self.binding)
            if record["event_type"] == "message.outbound.intent"
        ]
        self.assertEqual(journal.decode_exact_message(intents[0]), stale)
        if delivery_state == "not_attempted":
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "transport_accepted")
            self.assertEqual(payload["lifecycle"]["state"], "completed")
            self.assertEqual(marker.read_text().splitlines(), ["called"])
            self.assertEqual(len(intents), 2)
            self.assertEqual(journal.decode_exact_message(intents[1]), fresh)
            self.assertIsNone(intents[1]["attributes"].get("retry_after_intent"))
        else:
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(
                json.loads(result.stderr)["error"]["code"],
                "transport.reply_transition_reserved",
            )
            self.assertFalse(marker.exists())
            self.assertEqual(len(intents), 1)

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
        fake_codex = self.approved_codex_bin
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
        self.preflight_tool_correlated_route(fake_claude)

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
        fake_codex = self.approved_codex_bin
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
            env=self.transport_environment(),
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


if __name__ == "__main__":
    unittest.main()
