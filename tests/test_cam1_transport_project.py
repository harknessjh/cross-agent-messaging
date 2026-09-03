# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import asyncio
import datetime as dt
import json
import subprocess
import sys
import textwrap
import time
import unittest
from unittest import mock

from tools import cam1, cam1_transport
from tools.cam1lib import journal, project, state, transport_audit

if __package__:
    from .test_cam1_transport import (
        CLAUDE_PARTICIPANT,
        CLAUDE_SESSION,
        CODEX_PARTICIPANT,
        CODEX_THREAD,
        ProjectBoundTransportTestCase,
        build_first_contact,
        dirty_validator_override_used,
    )
else:
    from test_cam1_transport import (
        CLAUDE_PARTICIPANT,
        CLAUDE_SESSION,
        CODEX_PARTICIPANT,
        CODEX_THREAD,
        ProjectBoundTransportTestCase,
        build_first_contact,
        dirty_validator_override_used,
    )


class ProjectTransportGuardTests(ProjectBoundTransportTestCase):
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

    def test_changed_claude_ref_is_freshly_tool_correlated(self) -> None:
        self.add_claude_participant()
        self.add_codex_participant()
        initial = self.fake_claude(
            returned={
                "success": True,
                "msg_id": "00000000-0000-4000-8000-000000000900",
            }
        )
        self.preflight_tool_correlated_route(initial)
        confirmed = self.run_project(
            "participant",
            "confirm-route",
            "--participant",
            "local-worker",
            "--expected-address",
            "local-worker [abcdef]",
            "--operator-reference",
            "historical operator-confirmed route",
        )
        self.assertEqual(confirmed.returncode, 0, confirmed.stderr)
        marker = self.base / "changed-route.called"
        changed = self.fake_claude(
            returned={
                "success": True,
                "msg_id": "00000000-0000-4000-8000-000000000901",
            },
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

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["status"], "transport_accepted")
        self.assertTrue(marker.exists())
        participant = (
            state.StateStore(self.binding).snapshot().roster.select("local-worker")
        )
        self.assertEqual(participant.route.status.value, "tool_correlated")
        self.assertEqual(participant.route.address, "local-worker [fedcba]")
        self.assertIn(
            "message.outbound.intent",
            [record["event_type"] for record in journal.replay_records(self.binding)],
        )

    def test_changed_claude_product_name_requires_stable_rebinding(self) -> None:
        self.add_claude_participant()
        self.add_codex_participant()
        marker = self.base / "changed-name.called"
        changed = self.fake_claude(
            returned={
                "success": True,
                "msg_id": "00000000-0000-4000-8000-000000000901",
            },
            peer_name="renamed-worker",
            marker=marker,
        )
        envelope = self.private_envelope("changed-name.json", build_first_contact())

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
            json.loads(completed.stderr)["error"]["code"],
            "claude.session_label_mismatch",
        )
        self.assertFalse(marker.exists())
        self.assertIsNone(
            state.StateStore(self.binding)
            .snapshot()
            .roster.select("local-worker")
            .route
        )
        self.assertNotIn(
            "message.outbound.intent",
            [record["event_type"] for record in journal.replay_records(self.binding)],
        )

    def test_changed_claude_session_kind_requires_stable_rebinding(self) -> None:
        self.add_claude_participant()
        self.add_codex_participant()
        marker = self.base / "changed-kind.called"
        changed = self.fake_claude(
            returned={
                "success": True,
                "msg_id": "00000000-0000-4000-8000-000000000901",
            },
            peer_kind="background",
            marker=marker,
        )
        envelope = self.private_envelope("changed-kind.json", build_first_contact())

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
            json.loads(completed.stderr)["error"]["code"],
            "claude.session_kind_mismatch",
        )
        self.assertFalse(marker.exists())
        self.assertIsNone(
            state.StateStore(self.binding)
            .snapshot()
            .roster.select("local-worker")
            .route
        )
        self.assertNotIn(
            "message.outbound.intent",
            [record["event_type"] for record in journal.replay_records(self.binding)],
        )

    def test_same_uuid_rebind_during_preflight_fails_closed(self) -> None:
        self.add_claude_participant()

        async def discover_after_rebind(**_kwargs):
            event_now = dt.datetime.now(dt.UTC)
            observed_at = event_now.isoformat(timespec="microseconds").replace(
                "+00:00", "Z"
            )
            state.StateStore(self.binding).participant_bind(
                "local-worker",
                session_id=CLAUDE_SESSION,
                session_label="local-worker",
                session_kind="interactive",
                operator_reference="test operator refreshed the stable binding",
                bound_at=observed_at,
                now=event_now,
            )
            return {
                "ok": True,
                "status": "route_preflight",
                "local_only": True,
                "mcp_protocol": "2025-06-18",
                "identity": {
                    "session_id": CLAUDE_SESSION,
                    "agent_view_id": None,
                    "product_name": "local-worker",
                    "cwd": str(self.repo),
                    "kind": "interactive",
                    "state": "idle",
                    "started_at_ms": 1_784_241_375_111,
                },
                "route": {
                    "list_agents_name": "local-worker",
                    "list_agents_ref": "abcdef",
                    "kind": "interactive",
                    "state": "idle",
                },
                "notify_when_idle_supported": True,
                "operator_correlation_required": False,
            }

        with (
            mock.patch.object(
                cam1_transport,
                "_preflight_claude_session",
                new=discover_after_rebind,
            ),
            self.assertRaises(cam1_transport.TransportError) as context,
        ):
            asyncio.run(
                cam1_transport.preflight_project_claude(
                    self.binding,
                    claude_bin=str(self.approved_claude_bin),
                    participant_selector="local-worker",
                    session_id_guard=CLAUDE_SESSION,
                    target_guard=None,
                    timeout_seconds=1,
                )
            )

        self.assertEqual(context.exception.code, "claude.binding_changed")
        participant = (
            state.StateStore(self.binding).snapshot().roster.select("local-worker")
        )
        self.assertEqual(participant.binding.generation, 2)
        self.assertIsNone(participant.route)

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
