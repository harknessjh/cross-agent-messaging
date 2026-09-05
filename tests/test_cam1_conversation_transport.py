# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import json
import sys
import textwrap
import unittest
import uuid

from tools import cam1
from tools.cam1lib import journal, state

if __package__:
    from .test_cam1_transport import (
        CLAUDE_SESSION,
        CODEX_THREAD,
        ProjectBoundTransportTestCase,
    )
else:
    from test_cam1_transport import (
        CLAUDE_SESSION,
        CODEX_THREAD,
        ProjectBoundTransportTestCase,
    )


def request(*, reverse=False):
    return cam1.build_request(
        sender_vendor="claude-code" if reverse else "codex",
        sender_name="local-worker" if reverse else "example-coordinator",
        sender_session=CLAUDE_SESSION if reverse else CODEX_THREAD,
        recipient_vendor="codex" if reverse else "claude-code",
        recipient_name="example-coordinator" if reverse else "local-worker",
        recipient_session=CODEX_THREAD if reverse else CLAUDE_SESSION,
        reply_transport="claude_send_message" if reverse else "codex_queue",
        reply_address=CLAUDE_SESSION if reverse else CODEX_THREAD,
        risk_class="informational",
        operation="discuss",
        intent="Discuss one idea",
        body="What do you think of this approach?",
        authorization_basis="none",
    )


class ConversationTransportTests(ProjectBoundTransportTestCase):
    def test_three_independent_roots_link_across_both_adapters(self):
        self.add_codex_participant()
        self.add_claude_participant()
        marker = self.base / "claude-sent"
        claude_bin = self.fake_claude(
            returned={"success": True, "msg_id": str(uuid.uuid4())}, marker=marker
        )
        codex_bin = self.approved_codex_bin
        codex_bin.write_text(
            textwrap.dedent(f"""\
            #!{sys.executable}
            import sys
            thread = sys.argv[sys.argv.index('--thread') + 1]
            print('Queued message 00000000-0000-4000-8000-000000000901 for thread ' + thread + '.')
            """),
            encoding="utf-8",
        )
        codex_bin.chmod(0o700)
        ids = []
        raws = []
        for index in range(3):
            reverse = index == 1
            raw = request(reverse=reverse)
            raws.append(raw)
            ids.append(json.loads(raw)["message_id"])
            path = self.private_envelope(f"discussion-{index}.json", raw)
            recipient = "example-coordinator" if reverse else "local-worker"
            arguments = [
                "codex-send" if reverse else "claude-send",
                "--participant",
                recipient,
                "--envelope",
                str(path),
            ]
            if index:
                arguments += ["--continues-message", ids[index - 1]]
            result = self.run_transport(
                *arguments,
                codex_bin=codex_bin,
                claude_bin=claude_bin,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["lifecycle"]["state"], "pending")
            ingest = self.run_project(
                "message",
                "ingest",
                "--message",
                str(path),
                "--as-participant",
                recipient,
            )
            self.assertEqual(ingest.returncode, 0, ingest.stderr)
            self.assertFalse(json.loads(ingest.stdout)["action_authorized"])
        records = journal.replay_records(self.binding)
        intents = [
            record
            for record in records
            if record["event_type"] == "message.outbound.intent"
        ]
        self.assertEqual(
            [journal.decode_exact_message(record) for record in intents], raws
        )
        self.assertIsNone(intents[0]["attributes"]["conversation_link"])
        for index in (1, 2):
            self.assertEqual(
                intents[index]["attributes"]["conversation_link"],
                {
                    "format": "CAM-CONVERSATION/1",
                    "conversation_id": ids[0],
                    "parent_message_id": ids[index - 1],
                },
            )
            self.assertIsNone(intents[index]["attributes"]["causal_context"])
        entries = state.StateStore(self.binding).snapshot().lifecycle.entries
        self.assertEqual({entry.state.value for entry in entries.values()}, {"pending"})
        self.assertEqual(set(entries), set(ids))
        self.assertTrue(journal.verify_journal(self.binding).summary()["valid"])

        # An unknown parent cannot create an intent or dispatch a fourth root.
        before = self.binding.journal_path.read_bytes()
        marker_before = marker.read_bytes()
        refused = self.run_transport(
            "claude-send",
            "--participant",
            "local-worker",
            "--envelope",
            str(self.private_envelope("unknown-parent.json", request())),
            "--continues-message",
            str(uuid.uuid4()),
            claude_bin=claude_bin,
        )
        self.assertEqual(refused.returncode, 2)
        self.assertEqual(
            json.loads(refused.stderr)["error"]["code"], "conversation.parent_unknown"
        )
        self.assertEqual(marker.read_bytes(), marker_before)
        after = journal.replay_records(self.binding)
        self.assertEqual(
            len(
                [
                    record
                    for record in after
                    if record["event_type"] == "message.outbound.intent"
                ]
            ),
            3,
        )
        # Fresh route observations are allowed; application history is untouched.
        self.assertTrue(self.binding.journal_path.read_bytes().startswith(before))

    def test_conflicting_cli_flags_fail_before_project_or_product_lookup(self):
        for command in ("claude-send", "codex-send"):
            for conflicting in ("--renewal-of", "--retry-after-intent"):
                result = self.run_transport(
                    command,
                    "--participant",
                    "absent",
                    "--envelope",
                    "/absent.json",
                    "--continues-message",
                    str(uuid.uuid4()),
                    conflicting,
                    str(uuid.uuid4()),
                )
                self.assertEqual(result.returncode, 2)
                self.assertEqual(
                    json.loads(result.stderr)["error"]["code"],
                    "conversation.argument_conflict",
                )


if __name__ == "__main__":
    unittest.main()
