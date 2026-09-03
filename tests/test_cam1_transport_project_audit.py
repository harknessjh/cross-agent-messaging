# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import datetime as dt
import json
import sys
import textwrap
import unittest
from unittest import mock

from tools import cam1, cam1_transport
from tools.cam1lib import causal, journal, project, state, transport_audit

if __package__:
    from .test_cam1_transport import (
        CLAUDE_PARTICIPANT,
        CLAUDE_SESSION,
        CODEX_PARTICIPANT,
        CODEX_THREAD,
        ProjectBoundTransportTestCase,
    )
else:
    from test_cam1_transport import (
        CLAUDE_PARTICIPANT,
        CLAUDE_SESSION,
        CODEX_PARTICIPANT,
        CODEX_THREAD,
        ProjectBoundTransportTestCase,
    )


class ProjectTransportAuditTests(ProjectBoundTransportTestCase):
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
        fake_codex = self.approved_codex_bin
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

    def test_send_preparation_replays_only_relevant_history_once(self) -> None:
        self.add_codex_participant()
        self.add_claude_participant()

        def new_hello() -> bytes:
            return cam1.build_hello(
                sender_vendor="claude-code",
                sender_name="local-worker",
                sender_session=CLAUDE_SESSION,
                recipient_vendor="codex",
                recipient_name="example-coordinator",
                recipient_session=CODEX_THREAD,
                reply_transport="claude_send_message",
                reply_address=CLAUDE_SESSION,
            )

        prior_raw = new_hello()
        prior_envelope = cam1.parse_exact_bytes(prior_raw)
        with project.project_transaction(self.binding) as transaction:
            for index in range(16):
                journal.append_record(
                    self.binding,
                    event_type="message.inbound.observed",
                    exact_message=(f"unrelated-{index:02d}:".encode() + b"x" * 8_192),
                    attributes={"source": "unrelated_scale_fixture"},
                    transaction=transaction,
                )
            journal.append_record(
                self.binding,
                event_type="message.outbound.intent",
                exact_message=prior_raw,
                attributes={
                    "participant_id": CODEX_PARTICIPANT,
                    "sender_participant_id": CLAUDE_PARTICIPANT,
                    "recipient_participant_id": CODEX_PARTICIPANT,
                    "message_id": prior_envelope["message_id"],
                    "renewal_of": None,
                    "causal_context": None,
                },
                transaction=transaction,
            )

        current_raw = new_hello()
        current_path = self.private_envelope("filtered-history.json", current_raw)
        validated = cam1_transport._validate_envelope(str(current_path), None)
        store = state.StateStore(self.binding)
        real_replay = journal.replay_records
        real_decode = journal.decode_exact_message

        with project.project_transaction(self.binding) as transaction:
            recipient = store.snapshot(transaction=transaction).roster.select(
                "example-coordinator"
            )
            attempt = transport_audit._SendAttempt(
                participant_id=recipient.participant_id,
                transport="codex_queue",
                route_address=CODEX_THREAD,
            )
            with (
                mock.patch.object(
                    transport_audit.journal,
                    "replay_records",
                    wraps=real_replay,
                ) as replay,
                mock.patch.object(
                    transport_audit,
                    "_require_reply_slot_available",
                    wraps=transport_audit._require_reply_slot_available,
                ) as reply_slot,
                mock.patch.object(
                    transport_audit,
                    "_require_safe_retry",
                    wraps=transport_audit._require_safe_retry,
                ) as retry,
                mock.patch.object(
                    transport_audit.causal,
                    "build_outbound_context",
                    wraps=transport_audit.causal.build_outbound_context,
                ) as causal_context,
                mock.patch.object(
                    transport_audit.journal,
                    "decode_exact_message",
                    wraps=real_decode,
                ) as decode,
            ):
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
                )

        self.assertEqual(replay.call_count, 1)
        self.assertEqual(
            replay.call_args.kwargs["event_types"],
            transport_audit.causal.CAUSAL_JOURNAL_EVENT_TYPES,
        )
        history = reply_slot.call_args.kwargs["records"]
        self.assertIs(history, retry.call_args.kwargs["records"])
        self.assertIs(history, causal_context.call_args.args[0])
        self.assertEqual(decode.call_count, 1)

    def test_send_journals_canonical_renewal_reference_for_causal_replay(
        self,
    ) -> None:
        self.add_codex_participant()
        self.add_claude_participant()
        now = dt.datetime.now(dt.UTC).replace(microsecond=0)
        prior_time = now - dt.timedelta(minutes=12)
        idempotency_key = "00000000-0000-4000-8000-000000000997"

        def request(at: dt.datetime) -> bytes:
            return cam1.build_request(
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
                idempotency_key=idempotency_key,
                now=at,
            )

        first = request(prior_time)
        store = state.StateStore(self.binding)
        first_entry = store.lifecycle_root(first, now=prior_time)
        store.lifecycle_expired(
            first_entry.root_message_id,
            now=prior_time + dt.timedelta(minutes=11),
        )
        renewal = request(now)
        renewal_path = self.private_envelope("canonical-renewal.json", renewal)
        validated = cam1_transport._validate_envelope(str(renewal_path), None)

        with project.project_transaction(self.binding) as transaction:
            recipient = store.snapshot(transaction=transaction).roster.select(
                "local-worker"
            )
            attempt = transport_audit._SendAttempt(
                participant_id=recipient.participant_id,
                transport="claude_send_message",
                route_address="local-worker [abcdef]",
            )
            transport_audit._prepare_and_journal_intent(
                self.binding,
                store,
                transaction,
                validated,
                attempt,
                recipient_participant=recipient,
                renewal_of=first_entry.root_message_id.upper(),
                retry_after_intent=None,
                validation_profile={},
                dirty_validator_override=False,
            )

        records = journal.replay_records(self.binding)
        intent_records = tuple(
            record
            for record in records
            if record["event_type"] == "message.outbound.intent"
        )
        self.assertEqual(
            intent_records[-1]["attributes"]["renewal_of"],
            first_entry.root_message_id,
        )
        self.assertEqual(
            causal._all_intents(intent_records)[-1].renewal_of,
            first_entry.root_message_id,
        )


if __name__ == "__main__":
    unittest.main()
