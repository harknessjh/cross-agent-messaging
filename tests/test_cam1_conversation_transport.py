# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import json
import sys
import textwrap
import unittest
import uuid

from tools import cam1, cam1_transport
from tools.cam1lib import journal, project, state, transport_audit

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
    def test_legacy_retry_history_survives_new_dispatch_and_other_conversations(self):
        self.add_codex_participant()
        self.add_claude_participant()
        claude_bin = self.fake_claude(
            returned={"success": True, "msg_id": str(uuid.uuid4())}
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
        root_raw = request()
        root_id = json.loads(root_raw)["message_id"]
        root_path = self.private_envelope("root.json", root_raw)
        sent = self.run_transport(
            "claude-send",
            "--participant",
            "local-worker",
            "--envelope",
            str(root_path),
            claude_bin=claude_bin,
        )
        self.assertEqual(sent.returncode, 0, sent.stderr)
        ingested = self.run_project(
            "message",
            "ingest",
            "--message",
            str(root_path),
            "--as-participant",
            "local-worker",
        )
        self.assertEqual(ingested.returncode, 0, ingested.stderr)

        child_raw = request(reverse=True)
        child_id = json.loads(child_raw)["message_id"]
        child_path = self.private_envelope("child.json", child_raw)
        validated = cam1_transport._validate_envelope(str(child_path), None)
        store = state.StateStore(self.binding)
        with project.project_transaction(self.binding) as transaction:
            recipient = store.snapshot(transaction=transaction).roster.select(
                "example-coordinator"
            )
            attempt = transport_audit._SendAttempt(
                participant_id=recipient.participant_id,
                transport="codex_queue",
                route_address=CODEX_THREAD,
            )
            transport_audit._prepare_and_journal_intent(
                self.binding,
                store,
                transaction,
                validated,
                attempt,
                recipient_participant=recipient,
                renewal_of=None,
                retry_after_intent=None,
                validation_profile={},
                dirty_validator_override=False,
                continues_message=root_id,
            )
            transport_audit._journal_failed_attempt(
                self.binding,
                transaction,
                attempt,
                transport_audit.TransportError(
                    "transport.synthetic_stop", "test pre-dispatch stop"
                ),
            )
        original = attempt.intent_record
        self.assertIsNotNone(original)
        expected_link = original["attributes"]["conversation_link"]

        # Fixture for the pre-link adapter's actual journal contract: identical
        # bytes, explicit eligible retry, but no unfamiliar optional attribute.
        with project.project_transaction(self.binding) as transaction:
            self.assertEqual(
                transport_audit._require_safe_retry(
                    self.binding,
                    validated,
                    retry_after_intent=original["record_id"],
                    known_renewal_roots=frozenset(),
                ),
                original["record_id"],
            )
            legacy_attributes = dict(original["attributes"])
            legacy_attributes.pop("conversation_link")
            legacy_attributes["retry_after_intent"] = original["record_id"]
            legacy = journal.append_record(
                self.binding,
                event_type="message.outbound.intent",
                exact_message=child_raw,
                attributes=legacy_attributes,
                transaction=transaction,
            )
            journal.append_record(
                self.binding,
                event_type="transport.not_accepted",
                attributes={
                    "intent_record_id": legacy["record_id"],
                    "delivery_state": "not_attempted",
                },
                transaction=transaction,
            )
        preserved_prefix = self.binding.journal_path.read_bytes()

        retried = self.run_transport(
            "codex-send",
            "--participant",
            "example-coordinator",
            "--envelope",
            str(child_path),
            "--retry-after-intent",
            legacy["record_id"],
            codex_bin=codex_bin,
        )
        self.assertEqual(retried.returncode, 0, retried.stderr)
        intents = [
            record
            for record in journal.replay_records(self.binding)
            if record["event_type"] == "message.outbound.intent"
        ]
        self.assertEqual(intents[-1]["attributes"]["conversation_link"], expected_link)
        self.assertEqual(journal.decode_exact_message(intents[-1]), child_raw)
        ingested = self.run_project(
            "message",
            "ingest",
            "--message",
            str(child_path),
            "--as-participant",
            "example-coordinator",
        )
        self.assertEqual(ingested.returncode, 0, ingested.stderr)

        other_raw = request(reverse=True)
        other_id = json.loads(other_raw)["message_id"]
        other_path = self.private_envelope("unrelated.json", other_raw)
        other_sent = self.run_transport(
            "codex-send",
            "--participant",
            "example-coordinator",
            "--envelope",
            str(other_path),
            codex_bin=codex_bin,
        )
        self.assertEqual(other_sent.returncode, 0, other_sent.stderr)
        other_ingested = self.run_project(
            "message",
            "ingest",
            "--message",
            str(other_path),
            "--as-participant",
            "example-coordinator",
        )
        self.assertEqual(other_ingested.returncode, 0, other_ingested.stderr)
        for index, (parent_id, conversation_id) in enumerate(
            ((child_id, root_id), (other_id, other_id))
        ):
            follow_up = self.private_envelope(f"follow-up-{index}.json", request())
            sent = self.run_transport(
                "claude-send",
                "--participant",
                "local-worker",
                "--envelope",
                str(follow_up),
                "--continues-message",
                parent_id,
                claude_bin=claude_bin,
            )
            self.assertEqual(sent.returncode, 0, sent.stderr)
            intents = [
                record
                for record in journal.replay_records(self.binding)
                if record["event_type"] == "message.outbound.intent"
            ]
            self.assertEqual(
                intents[-1]["attributes"]["conversation_link"]["conversation_id"],
                conversation_id,
            )
        self.assertTrue(
            self.binding.journal_path.read_bytes().startswith(preserved_prefix)
        )
        self.assertTrue(journal.verify_journal(self.binding).summary()["valid"])

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
