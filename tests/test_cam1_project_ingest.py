# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import datetime as dt
import json
import unittest
from pathlib import Path
from unittest import mock

from tools import cam1_project
from tools.cam1lib import builders, journal, project, state, state_projection

if __package__:
    from .test_cam1_project import (
        CLAUDE_PARTICIPANT,
        CLAUDE_SESSION,
        CODEX_PARTICIPANT,
        CODEX_SESSION,
        NOW,
        ProjectTestCase,
    )
else:
    from test_cam1_project import (
        CLAUDE_PARTICIPANT,
        CLAUDE_SESSION,
        CODEX_PARTICIPANT,
        CODEX_SESSION,
        NOW,
        ProjectTestCase,
    )


class ProjectMessageIngestTests(ProjectTestCase):
    def test_message_ingest_retains_malformed_bytes_before_rejection(self) -> None:
        binding = self.initialize()
        raw = b'{"protocol":"CAM/1",not-valid-json\n\xff'
        message_path = self.private_message_file("malformed.cam1.json", raw)

        result = self.run_tool(
            "message",
            "ingest",
            "--message",
            str(message_path),
            "--as-participant",
            "local-receiver",
        )

        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stderr)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "rejected")
        self.assertLessEqual(len(payload["error"]["problem_codes"]), 16)
        records = journal.replay_records(binding)
        self.assertEqual(
            [record["event_type"] for record in records],
            ["message.inbound.observed", "message.inbound.rejected"],
        )
        self.assertEqual(journal.decode_exact_message(records[0]), raw)
        self.assertIsNone(records[1]["message"])
        self.assertEqual(
            records[1]["attributes"]["observed_record_id"],
            records[0]["record_id"],
        )
        self.assertTrue(records[1]["attributes"]["validation_profile"]["available"])
        self.assertTrue(payload["validation_profile"]["available"])
        self.assertEqual(state.StateStore(binding).snapshot().lifecycle.entries, {})

    def test_message_ingest_commits_valid_root_and_reply_lifecycle(self) -> None:
        binding = self.initialize()
        self.bind_ingest_participants(binding)
        root = builders.build_request(
            sender_vendor="codex",
            sender_name="project-coordinator",
            sender_session=CODEX_SESSION,
            recipient_vendor="claude-code",
            recipient_name="bob-reviewer",
            recipient_session=CLAUDE_SESSION,
            reply_transport="codex_queue",
            reply_address=CODEX_SESSION,
            risk_class="informational",
            operation="review_structure",
            intent="Request one local structure review",
            body="Review the project structure without making changes.",
            authorization_basis="none",
            now=dt.datetime.now(dt.UTC),
        )
        root_result = self.run_tool(
            "message",
            "ingest",
            "--message",
            str(self.private_message_file("request.cam1.json", root)),
            "--as-participant",
            "bob-reviewer",
        )
        self.assertEqual(root_result.returncode, 0, root_result.stderr)
        root_payload = json.loads(root_result.stdout)
        self.assertEqual(root_payload["status"], "validated")
        self.assertFalse(root_payload["authorization_evaluated"])
        self.assertFalse(root_payload["action_authorized"])

        reply = builders.build_ack(
            root,
            sender_vendor="claude-code",
            sender_name="bob-reviewer",
            sender_session=CLAUDE_SESSION,
            reply_transport="claude_send_message",
            reply_address=CLAUDE_SESSION,
            status_value="received",
            now=dt.datetime.now(dt.UTC),
        )
        reply_result = self.run_tool(
            "message",
            "ingest",
            "--message",
            str(self.private_message_file("ack.cam1.json", reply)),
            "--as-participant",
            "project-coordinator",
        )
        self.assertEqual(reply_result.returncode, 0, reply_result.stderr)
        self.assertEqual(
            json.loads(reply_result.stdout)["lifecycle"]["state"], "received"
        )

        records = tuple(
            record
            for record in journal.replay_records(binding)
            if record["event_type"].startswith("message.")
            or record["event_type"].startswith("state.lifecycle.")
        )
        self.assertEqual(
            [record["event_type"] for record in records],
            [
                "message.inbound.observed",
                state.LIFECYCLE_ROOT_REGISTERED,
                "message.inbound.validated",
                "message.inbound.observed",
                state.LIFECYCLE_REPLY_APPLIED,
                "message.inbound.validated",
            ],
        )
        self.assertEqual(journal.decode_exact_message(records[0]), root)
        self.assertEqual(journal.decode_exact_message(records[1]), root)
        self.assertEqual(journal.decode_exact_message(records[3]), reply)
        self.assertEqual(journal.decode_exact_message(records[4]), reply)
        for record in (records[2], records[5]):
            self.assertTrue(record["attributes"]["validation_profile"]["available"])
        self.assertTrue(root_payload["validation_profile"]["available"])

    def test_message_ingest_marks_exact_root_and_reply_retransmissions_duplicate(
        self,
    ) -> None:
        binding = self.initialize()
        self.bind_ingest_participants(binding)
        root = builders.build_request(
            sender_vendor="codex",
            sender_name="project-coordinator",
            sender_session=CODEX_SESSION,
            recipient_vendor="claude-code",
            recipient_name="bob-reviewer",
            recipient_session=CLAUDE_SESSION,
            reply_transport="codex_queue",
            reply_address=CODEX_SESSION,
            risk_class="informational",
            operation="review_structure",
            intent="Request one local structure review",
            body="Review the project structure without making changes.",
            authorization_basis="none",
            now=dt.datetime.now(dt.UTC),
        )
        root_path = self.private_message_file("duplicate-request.cam1.json", root)
        state.StateStore(binding).lifecycle_root(root, now=dt.datetime.now(dt.UTC))
        first_root = self.run_tool(
            "message",
            "ingest",
            "--message",
            str(root_path),
            "--as-participant",
            "bob-reviewer",
        )
        duplicate_root = self.run_tool(
            "message",
            "ingest",
            "--message",
            str(root_path),
            "--as-participant",
            "bob-reviewer",
        )
        self.assertEqual(first_root.returncode, 0, first_root.stderr)
        self.assertEqual(duplicate_root.returncode, 0, duplicate_root.stderr)
        root_payload = json.loads(duplicate_root.stdout)
        self.assertEqual(root_payload["status"], "duplicate")
        self.assertTrue(root_payload["duplicate"])

        reply = builders.build_ack(
            root,
            sender_vendor="claude-code",
            sender_name="bob-reviewer",
            sender_session=CLAUDE_SESSION,
            reply_transport="claude_send_message",
            reply_address=CLAUDE_SESSION,
            status_value="received",
            now=dt.datetime.now(dt.UTC),
        )
        reply_path = self.private_message_file("duplicate-ack.cam1.json", reply)
        state.StateStore(binding).lifecycle_reply(reply, now=dt.datetime.now(dt.UTC))
        first_reply = self.run_tool(
            "message",
            "ingest",
            "--message",
            str(reply_path),
            "--as-participant",
            "project-coordinator",
        )
        duplicate_reply = self.run_tool(
            "message",
            "ingest",
            "--message",
            str(reply_path),
            "--as-participant",
            "project-coordinator",
        )
        self.assertEqual(first_reply.returncode, 0, first_reply.stderr)
        self.assertEqual(duplicate_reply.returncode, 0, duplicate_reply.stderr)
        reply_payload = json.loads(duplicate_reply.stdout)
        self.assertEqual(reply_payload["status"], "duplicate")
        self.assertTrue(reply_payload["duplicate"])

        records = journal.replay_records(binding)
        self.assertEqual(
            sum(
                record["event_type"] == state.LIFECYCLE_ROOT_REGISTERED
                for record in records
            ),
            1,
        )
        self.assertEqual(
            sum(
                record["event_type"] == state.LIFECYCLE_REPLY_APPLIED
                for record in records
            ),
            1,
        )
        self.assertEqual(
            sum(
                record["event_type"] == "message.inbound.duplicate"
                for record in records
            ),
            2,
        )
        for record in records:
            if record["event_type"] == "message.inbound.duplicate":
                self.assertTrue(record["attributes"]["validation_profile"]["available"])

    def test_message_ingest_survives_projection_refresh_failure(self) -> None:
        binding = self.initialize()
        self.bind_ingest_participants(binding)
        raw = builders.build_request(
            sender_vendor="codex",
            sender_name="project-coordinator",
            sender_session=CODEX_SESSION,
            recipient_vendor="claude-code",
            recipient_name="bob-reviewer",
            recipient_session=CLAUDE_SESSION,
            reply_transport="codex_queue",
            reply_address=CODEX_SESSION,
            risk_class="informational",
            operation="review_structure",
            intent="Request one local structure review",
            body="Review the project structure without making changes.",
            authorization_basis="none",
            now=dt.datetime.now(dt.UTC),
        )
        path = self.private_message_file("projection-failure.cam1.json", raw)

        with mock.patch.object(
            state_projection,
            "replace_private_json",
            side_effect=project.ProjectError("state.replace", "injected failure"),
        ):
            return_code, payload = cam1_project._ingest_message(
                binding,
                message_path=str(path),
                as_participant="bob-reviewer",
                renewal_of=None,
            )

        self.assertEqual(return_code, 0, payload)
        self.assertEqual(payload["status"], "validated")
        self.assertFalse(payload["state_projection"]["current"])
        self.assertTrue(payload["state_projection"]["rebuild_required"])
        self.assertEqual(
            [record["event_type"] for record in journal.replay_records(binding)[-3:]],
            [
                "message.inbound.observed",
                state.LIFECYCLE_ROOT_REGISTERED,
                "message.inbound.validated",
            ],
        )
        rebuilt = state.StateStore(binding).rebuild()
        self.assertEqual(len(rebuilt.lifecycle.entries), 1)

    def test_message_ingest_rejects_non_private_file_before_journaling(self) -> None:
        binding = self.initialize()
        message_path = self.base / "public-message.json"
        message_path.write_bytes(b"{}")
        message_path.chmod(0o644)

        result = self.run_tool(
            "message",
            "ingest",
            "--message",
            str(message_path),
            "--as-participant",
            "local-receiver",
        )

        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stderr)["error"]["code"], "state.file.mode")
        self.assertEqual(journal.verify_journal(binding).record_count, 0)

    def test_message_ingest_rejects_wrong_local_recipient_after_retention(self) -> None:
        binding = self.initialize()
        self.bind_ingest_participants(binding)
        raw = builders.build_request(
            sender_vendor="codex",
            sender_name="project-coordinator",
            sender_session=CODEX_SESSION,
            recipient_vendor="claude-code",
            recipient_name="bob-reviewer",
            recipient_session="00000000-0000-4000-8000-000000000999",
            reply_transport="codex_queue",
            reply_address=CODEX_SESSION,
            risk_class="informational",
            operation="review_structure",
            intent="Request one local structure review",
            body="Review the project structure without making changes.",
            authorization_basis="none",
            now=dt.datetime.now(dt.UTC),
        )

        result = self.run_tool(
            "message",
            "ingest",
            "--message",
            str(self.private_message_file("wrong-recipient.cam1.json", raw)),
            "--as-participant",
            "bob-reviewer",
        )

        self.assertEqual(result.returncode, 2)
        self.assertEqual(
            json.loads(result.stderr)["error"]["code"],
            "roster.recipient_mismatch",
        )
        records = journal.replay_records(binding)
        self.assertEqual(
            [record["event_type"] for record in records[-2:]],
            ["message.inbound.observed", "message.inbound.rejected"],
        )
        self.assertEqual(journal.decode_exact_message(records[-2]), raw)

    def test_message_ingest_reports_wrapper_validation_before_roster_mismatch(
        self,
    ) -> None:
        binding = self.initialize()
        self.bind_ingest_participants(binding)
        envelope = json.loads(
            builders.build_request(
                sender_vendor="codex",
                sender_name="project-coordinator",
                sender_session=CODEX_SESSION,
                recipient_vendor="claude-code",
                recipient_name="bob-reviewer",
                recipient_session=CLAUDE_SESSION,
                reply_transport="codex_queue",
                reply_address=CODEX_SESSION,
                risk_class="informational",
                operation="review_structure",
                intent="Request one local structure review",
                body="Review the project structure without making changes.",
                authorization_basis="none",
                now=dt.datetime.now(dt.UTC),
            )
        )
        del envelope["action"]["operation"]
        envelope["recipient"]["agent_name"] = "wrong-recipient"
        raw = json.dumps(envelope, separators=(",", ":"), sort_keys=True).encode()
        path = self.private_message_file("invalid-before-roster.cam1.json", raw)

        result = self.run_tool(
            "message",
            "ingest",
            "--message",
            str(path),
            "--as-participant",
            "bob-reviewer",
        )

        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stderr)
        self.assertEqual(payload["error"]["code"], "validation.failed")
        self.assertIn("schema.required", payload["error"]["problem_codes"])

    def test_message_ingest_records_expired_root_as_rejected_not_accepted(self) -> None:
        binding = self.initialize()
        self.bind_ingest_participants(binding)
        raw = builders.build_request(
            sender_vendor="codex",
            sender_name="project-coordinator",
            sender_session=CODEX_SESSION,
            recipient_vendor="claude-code",
            recipient_name="bob-reviewer",
            recipient_session=CLAUDE_SESSION,
            reply_transport="codex_queue",
            reply_address=CODEX_SESSION,
            risk_class="informational",
            operation="review_structure",
            intent="Request one local structure review",
            body="Review the project structure without making changes.",
            authorization_basis="none",
            now=dt.datetime.now(dt.UTC) - dt.timedelta(hours=2),
        )

        result = self.run_tool(
            "message",
            "ingest",
            "--message",
            str(self.private_message_file("expired.cam1.json", raw)),
            "--as-participant",
            "bob-reviewer",
        )

        self.assertEqual(result.returncode, 2)
        self.assertEqual(
            json.loads(result.stderr)["error"]["code"], "state.root_expired"
        )
        snapshot = state.StateStore(binding).snapshot()
        entry = next(iter(snapshot.lifecycle.entries.values()))
        self.assertEqual(entry.state.value, "expired_unconfirmed")
        self.assertEqual(
            [record["event_type"] for record in journal.replay_records(binding)[-3:]],
            [
                "message.inbound.observed",
                state.LIFECYCLE_ROOT_REGISTERED,
                "message.inbound.rejected",
            ],
        )

    def test_message_ingest_rejects_expired_duplicate_of_accepted_root(self) -> None:
        binding = self.initialize()
        self.bind_ingest_participants(binding)
        raw = builders.build_request(
            sender_vendor="codex",
            sender_name="project-coordinator",
            sender_session=CODEX_SESSION,
            recipient_vendor="claude-code",
            recipient_name="bob-reviewer",
            recipient_session=CLAUDE_SESSION,
            reply_transport="codex_queue",
            reply_address=CODEX_SESSION,
            risk_class="informational",
            operation="review_structure",
            intent="Request one local structure review",
            body="Review the project structure without making changes.",
            authorization_basis="none",
            now=NOW,
        )
        store = state.StateStore(binding)
        store.lifecycle_root(raw, now=NOW + dt.timedelta(seconds=1))
        accepted = builders.build_ack(
            raw,
            sender_vendor="claude-code",
            sender_name="bob-reviewer",
            sender_session=CLAUDE_SESSION,
            reply_transport="claude_send_message",
            reply_address=CLAUDE_SESSION,
            status_value="accepted",
            now=NOW + dt.timedelta(seconds=2),
        )
        store.lifecycle_reply(accepted, now=NOW + dt.timedelta(seconds=3))
        path = self.private_message_file("expired-duplicate.cam1.json", raw)
        after_expiry = NOW + dt.timedelta(minutes=11)

        with (
            mock.patch.object(
                cam1_project,
                "_utc_now",
                return_value=(
                    after_expiry,
                    after_expiry.isoformat().replace("+00:00", "Z"),
                ),
            ),
            mock.patch.object(
                state_projection,
                "_current_utc_time",
                return_value=after_expiry,
            ),
        ):
            return_code, payload = cam1_project._ingest_message(
                binding,
                message_path=str(path),
                as_participant="bob-reviewer",
                renewal_of=None,
            )

        self.assertEqual(return_code, 2)
        self.assertEqual(payload["error"]["code"], "lifecycle.duplicate_expired")
        self.assertEqual(
            [record["event_type"] for record in journal.replay_records(binding)[-2:]],
            ["message.inbound.observed", "message.inbound.rejected"],
        )
        root_id = json.loads(raw)["message_id"]
        self.assertEqual(
            store.snapshot().lifecycle.entries[root_id].state.value, "accepted"
        )

    def test_message_ingest_checks_expiry_after_journal_append(self) -> None:
        binding = self.initialize()
        self.bind_ingest_participants(binding)
        raw = builders.build_request(
            sender_vendor="codex",
            sender_name="project-coordinator",
            sender_session=CODEX_SESSION,
            recipient_vendor="claude-code",
            recipient_name="bob-reviewer",
            recipient_session=CLAUDE_SESSION,
            reply_transport="codex_queue",
            reply_address=CODEX_SESSION,
            risk_class="informational",
            operation="review_structure",
            intent="Request one local structure review",
            body="Review the project structure without making changes.",
            authorization_basis="none",
            now=NOW,
        )
        path = self.private_message_file("expires-during-journal.cam1.json", raw)
        before_expiry = NOW + dt.timedelta(minutes=9, seconds=59)
        after_expiry = NOW + dt.timedelta(minutes=10, seconds=1)

        def observed(value: dt.datetime) -> tuple[dt.datetime, str]:
            return value, value.isoformat().replace("+00:00", "Z")

        with mock.patch.object(
            cam1_project,
            "_utc_now",
            side_effect=[
                observed(before_expiry),
                observed(before_expiry),
                observed(after_expiry),
                observed(after_expiry),
            ],
        ):
            return_code, payload = cam1_project._ingest_message(
                binding,
                message_path=str(path),
                as_participant="bob-reviewer",
                renewal_of=None,
            )

        self.assertEqual(return_code, 2)
        self.assertEqual(payload["error"]["code"], "state.root_expired")

    def test_first_recipient_ingest_rechecks_precommitted_reply_expiry(self) -> None:
        binding = self.initialize()
        self.bind_ingest_participants(binding)
        store = state.StateStore(binding)
        root = builders.build_request(
            sender_vendor="codex",
            sender_name="project-coordinator",
            sender_session=CODEX_SESSION,
            recipient_vendor="claude-code",
            recipient_name="bob-reviewer",
            recipient_session=CLAUDE_SESSION,
            reply_transport="codex_queue",
            reply_address=CODEX_SESSION,
            risk_class="informational",
            operation="review_structure",
            intent="Request one local structure review",
            body="Review the project structure without making changes.",
            authorization_basis="none",
            now=NOW,
        )
        store.lifecycle_root(root, now=NOW)
        before_expiry = NOW + dt.timedelta(minutes=9, seconds=59)
        reply = builders.build_ack(
            root,
            sender_vendor="claude-code",
            sender_name="bob-reviewer",
            sender_session=CLAUDE_SESSION,
            reply_transport="claude_send_message",
            reply_address=CLAUDE_SESSION,
            status_value="accepted",
            now=before_expiry,
        )
        store.lifecycle_reply(reply, now=before_expiry)
        reply_path = self.private_message_file("late-precommitted-ack.json", reply)
        after_expiry = NOW + dt.timedelta(minutes=10, seconds=1)

        with mock.patch.object(
            cam1_project,
            "_utc_now",
            return_value=(
                after_expiry,
                after_expiry.isoformat().replace("+00:00", "Z"),
            ),
        ):
            return_code, payload = cam1_project._ingest_message(
                binding,
                message_path=str(reply_path),
                as_participant="project-coordinator",
                renewal_of=None,
            )

        self.assertEqual(return_code, 2)
        self.assertEqual(
            payload["error"]["code"],
            "lifecycle.root_expired_before_reply",
        )
        self.assertNotIn(
            "message.inbound.validated",
            [record["event_type"] for record in journal.replay_records(binding)],
        )

    def test_late_callback_accepts_prior_locally_delivered_reply(self) -> None:
        binding = self.initialize()
        self.bind_ingest_participants(binding)
        store = state.StateStore(binding)
        root = builders.build_request(
            sender_vendor="codex",
            sender_name="project-coordinator",
            sender_session=CODEX_SESSION,
            recipient_vendor="claude-code",
            recipient_name="bob-reviewer",
            recipient_session=CLAUDE_SESSION,
            reply_transport="codex_queue",
            reply_address=CODEX_SESSION,
            risk_class="informational",
            operation="review_structure",
            intent="Request one local structure review",
            body="Review the project structure without making changes.",
            authorization_basis="none",
            now=NOW,
        )
        store.lifecycle_root(root, now=NOW)
        before_expiry = NOW + dt.timedelta(minutes=9, seconds=59)
        reply = builders.build_ack(
            root,
            sender_vendor="claude-code",
            sender_name="bob-reviewer",
            sender_session=CLAUDE_SESSION,
            reply_transport="claude_send_message",
            reply_address=CLAUDE_SESSION,
            status_value="accepted",
            now=before_expiry,
        )
        intent = journal.append_record(
            binding,
            event_type="message.outbound.intent",
            exact_message=reply,
            attributes={"message_id": json.loads(reply)["message_id"]},
            now=before_expiry,
        )
        store.lifecycle_reply(reply, now=before_expiry)
        journal.append_record(
            binding,
            event_type="transport.accepted",
            attributes={
                "intent_record_id": intent["record_id"],
                "message_id": json.loads(reply)["message_id"],
                "lifecycle_state_committed": True,
            },
            now=before_expiry,
        )
        reply_path = self.private_message_file("delivered-late-ack.json", reply)
        after_expiry = NOW + dt.timedelta(minutes=10, seconds=1)

        with (
            mock.patch.object(
                cam1_project,
                "_utc_now",
                return_value=(
                    after_expiry,
                    after_expiry.isoformat().replace("+00:00", "Z"),
                ),
            ),
            mock.patch.object(
                state_projection,
                "_current_utc_time",
                return_value=after_expiry,
            ),
        ):
            return_code, payload = cam1_project._ingest_message(
                binding,
                message_path=str(reply_path),
                as_participant="project-coordinator",
                renewal_of=None,
            )

        self.assertEqual(return_code, 0, payload)
        self.assertEqual(payload["status"], "validated")
        self.assertEqual(payload["lifecycle"]["state"], "accepted")

    def private_message_file(self, name: str, raw: bytes) -> Path:
        path = self.base / name
        path.write_bytes(raw)
        path.chmod(0o600)
        return path

    def bind_ingest_participants(self, binding: project.ProjectBinding) -> None:
        store = state.StateStore(binding)
        observed = dt.datetime.now(dt.UTC)
        timestamp = observed.isoformat(timespec="microseconds").replace("+00:00", "Z")
        for participant_id, common_name, display_name, vendor, session_id in (
            (
                CODEX_PARTICIPANT,
                "project-coordinator",
                "Project coordinator",
                "codex",
                CODEX_SESSION,
            ),
            (
                CLAUDE_PARTICIPANT,
                "bob-reviewer",
                "Bob reviewer",
                "claude-code",
                CLAUDE_SESSION,
            ),
        ):
            store.participant_add(
                participant_id=participant_id,
                common_name=common_name,
                display_name=display_name,
                role="CAM test participant",
                vendor=vendor,
                now=observed,
            )
            store.participant_bind(
                common_name,
                session_id=session_id,
                session_label=display_name,
                session_kind="interactive",
                operator_reference="test operator correlation",
                bound_at=timestamp,
                now=observed,
            )


if __name__ == "__main__":
    unittest.main()
