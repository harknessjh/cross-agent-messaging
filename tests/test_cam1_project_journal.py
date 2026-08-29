# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import unittest
import uuid
from pathlib import Path
from unittest import mock

from tools.cam1lib import journal, journal_recovery, project, state

if __package__:
    from .test_cam1_project import NOW, ProjectTestCase, mode
else:
    from test_cam1_project import NOW, ProjectTestCase, mode


class ProjectJournalTests(ProjectTestCase):
    def test_journal_preserves_exact_bytes_and_builds_hash_chain(self) -> None:
        binding = self.initialize()
        first_bytes = b'\x00not utf-8: \xff\n{"body":"exact"}\n'
        first = journal.append_record(
            binding,
            event_type="transport.sent",
            exact_message=first_bytes,
            attributes={"route": "claude", "private_note": "do not print"},
            now=NOW,
        )
        second = journal.append_record(
            binding,
            event_type="application.received",
            exact_message=b"reply",
            attributes={"correlated": True},
            now=NOW + dt.timedelta(seconds=1),
        )

        verification = journal.verify_journal(binding)
        records = journal.replay_records(binding)
        self.assertEqual(verification.record_count, 2)
        self.assertEqual(verification.last_sequence, 2)
        self.assertEqual(verification.last_record_sha256, second["record_sha256"])
        self.assertEqual(first["previous_record_sha256"], None)
        self.assertEqual(second["previous_record_sha256"], first["record_sha256"])
        self.assertEqual(first["worktree_id"], binding.worktree_id)
        self.assertEqual(first["provenance"]["git_top_level"], str(self.repo))
        self.assertIsNone(first["provenance"]["head_sha"])
        self.assertIsNone(first["provenance"]["head_tree_sha"])
        self.assertIn(first["provenance"]["branch"], {"main", "master"})
        self.assertFalse(first["provenance"]["dirty"])
        self.assertEqual(journal.decode_exact_message(records[0]), first_bytes)
        self.assertEqual(journal.decode_exact_message(records[1]), b"reply")
        self.assertEqual(mode(binding.journal_path), 0o600)
        self.assertEqual(
            journal.replay_records(binding, event_types={"transport.sent"}),
            (records[0],),
        )

    def test_project_transaction_token_scopes_an_append(self) -> None:
        binding = self.initialize()
        with (
            project.project_transaction(binding) as transaction,
            project.project_transaction(binding) as nested,
        ):
            self.assertIs(nested, transaction)
            journal.append_record(
                binding,
                event_type="message.sent",
                now=NOW,
            )
        with self.assertRaises(project.ProjectError) as context:
            journal.append_record(
                binding,
                event_type="message.received",
                now=NOW,
                transaction=transaction,
            )
        self.assertEqual(context.exception.code, "transaction.inactive")

    def test_transaction_verifies_journal_once_and_advances_cached_chain(self) -> None:
        binding = self.initialize()
        original_verify = journal._verify_records

        with mock.patch.object(
            journal, "_verify_records", wraps=original_verify
        ) as verify_records:
            with project.project_transaction(binding) as transaction:
                self.assertEqual(journal.replay_records(binding), ())
                first = journal.append_record(
                    binding,
                    event_type="message.sent",
                    now=NOW,
                    transaction=transaction,
                )
                second = journal.append_record(
                    binding,
                    event_type="message.received",
                    now=NOW + dt.timedelta(seconds=1),
                    transaction=transaction,
                )
                self.assertEqual(
                    journal.verify_journal(binding).last_record_sha256,
                    second["record_sha256"],
                )
                self.assertEqual(
                    [record["record_id"] for record in journal.replay_records(binding)],
                    [first["record_id"], second["record_id"]],
                )
                first["event_type"] = "message.changed-by-caller"
                self.assertEqual(
                    journal.replay_records(binding)[0]["event_type"],
                    "message.sent",
                )
                self.assertEqual(verify_records.call_count, 1)

            with project.project_transaction(binding):
                journal.verify_journal(binding)
                journal.replay_records(binding)
                self.assertEqual(verify_records.call_count, 2)

    def test_append_detaches_nested_attributes_before_digest_and_cache(self) -> None:
        binding = self.initialize()
        attributes = {"nested": {"values": ["preserved"]}}
        original_digest = journal._record_digest

        def digest_then_mutate(record: dict[str, object]) -> str:
            digest = original_digest(record)
            attributes["nested"]["values"].append("caller mutation")
            return digest

        with (
            mock.patch.object(
                journal, "_record_digest", side_effect=digest_then_mutate
            ),
            project.project_transaction(binding) as transaction,
        ):
            appended = journal.append_record(
                binding,
                event_type="note.nested-attributes",
                attributes=attributes,
                now=NOW,
                transaction=transaction,
            )
            replayed = journal.replay_records(binding)[0]

        expected = {"nested": {"values": ["preserved"]}}
        self.assertEqual(appended["attributes"], expected)
        self.assertEqual(replayed["attributes"], expected)
        self.assertGreater(len(attributes["nested"]["values"]), 1)
        self.assertEqual(journal.verify_journal(binding).record_count, 1)

    def test_append_rejects_generated_record_with_invalid_self_digest(self) -> None:
        binding = self.initialize()
        original_digest = journal._record_digest
        calls = 0

        def wrong_then_actual(record: dict[str, object]) -> str:
            nonlocal calls
            calls += 1
            if calls == 1:
                return "0" * 64
            return original_digest(record)

        with (
            mock.patch.object(journal, "_record_digest", side_effect=wrong_then_actual),
            self.assertRaises(journal.JournalError) as context,
        ):
            journal.append_record(binding, event_type="note.invalid-generated-digest")

        self.assertEqual(context.exception.code, "journal.record_digest")
        self.assertEqual(binding.journal_path.read_bytes(), b"")

    def test_full_replay_checks_each_record_self_digest_once(self) -> None:
        binding = self.initialize()
        journal.append_record(binding, event_type="note.digest-count", now=NOW)
        original_digest = journal._record_digest

        with mock.patch.object(
            journal, "_record_digest", wraps=original_digest
        ) as record_digest:
            self.assertEqual(journal.verify_journal(binding).record_count, 1)

        self.assertEqual(record_digest.call_count, 1)

    def test_transaction_cache_rejects_journal_identity_substitution(self) -> None:
        binding = self.initialize()
        journal.append_record(binding, event_type="message.sent", now=NOW)

        with project.project_transaction(binding):
            journal.replay_records(binding)
            original = binding.journal_path.read_bytes()
            binding.journal_path.rename(binding.project_dir / "journal-replaced.jsonl")
            project.create_private_bytes(binding.journal_path, original)

            with self.assertRaises(journal.JournalError) as context:
                journal.replay_records(binding)

        self.assertEqual(context.exception.code, "journal.changed")

    def test_project_transaction_lock_contention_is_bounded(self) -> None:
        binding = self.initialize()

        def contended_flock(_descriptor: int, operation: int) -> None:
            if operation & project.fcntl.LOCK_NB:
                raise BlockingIOError(project.errno.EAGAIN, "injected contention")

        with (
            mock.patch.object(project, "PROJECT_LOCK_TIMEOUT_SECONDS", 0.0),
            mock.patch.object(project.fcntl, "flock", side_effect=contended_flock),
            self.assertRaises(project.ProjectError) as context,
            project.project_transaction(binding),
        ):
            self.fail("contended transaction unexpectedly acquired the lock")

        self.assertEqual(context.exception.code, "transaction.busy")
        self.assertEqual(journal.verify_journal(binding).record_count, 0)

    def test_project_transaction_rejects_lock_path_substitution_after_acquire(
        self,
    ) -> None:
        binding = self.initialize()
        original_flock = project.fcntl.flock
        substituted = False

        def substitute_after_acquire(descriptor: int, operation: int) -> None:
            nonlocal substituted
            original_flock(descriptor, operation)
            if (
                not substituted
                and operation & project.fcntl.LOCK_EX
                and operation & project.fcntl.LOCK_NB
            ):
                substituted = True
                orphaned = binding.project_dir / "orphaned-transaction.lock"
                binding.transaction_lock_path.rename(orphaned)
                project.create_private_bytes(binding.transaction_lock_path, b"")

        with (
            mock.patch.object(
                project.fcntl,
                "flock",
                side_effect=substitute_after_acquire,
            ),
            self.assertRaises(project.ProjectError) as context,
            project.project_transaction(binding),
        ):
            self.fail("substituted transaction lock unexpectedly authorized mutation")

        self.assertEqual(context.exception.code, "transaction.identity")
        self.assertEqual(journal.verify_journal(binding).record_count, 0)

    def test_caller_constructed_transaction_token_is_rejected(self) -> None:
        binding = self.initialize()
        descriptor = os.open(binding.transaction_lock_path, os.O_RDWR)
        try:
            metadata = os.fstat(descriptor)
            forged = project.ProjectTransaction(
                project_id=binding.project_id,
                project_dir=binding.project_dir,
                lock_path=binding.transaction_lock_path,
                descriptor=descriptor,
                device=metadata.st_dev,
                inode=metadata.st_ino,
            )
            with self.assertRaises(project.ProjectError) as context:
                journal.append_record(
                    binding,
                    event_type="message.observed",
                    transaction=forged,
                )
        finally:
            os.close(descriptor)

        self.assertEqual(context.exception.code, "transaction.inactive")
        self.assertEqual(journal.verify_journal(binding).record_count, 0)

    def test_tail_is_bounded_and_redacts_message_and_attributes(self) -> None:
        binding = self.initialize()
        journal.append_record(
            binding,
            event_type="message.received",
            exact_message=b"MESSAGE-SECRET",
            attributes={"token": "ATTRIBUTE-SECRET"},
            now=NOW,
        )

        tail = journal.tail_records(binding, limit=1)
        rendered = json.dumps(tail)

        self.assertNotIn("MESSAGE-SECRET", rendered)
        self.assertNotIn("ATTRIBUTE-SECRET", rendered)
        self.assertEqual(tail[0]["message"]["content"], "<redacted>")
        self.assertEqual(tail[0]["attributes"]["redacted"], True)
        full_tail = journal.tail_records(binding, limit=1, redact=False)
        self.assertEqual(journal.decode_exact_message(full_tail[0]), b"MESSAGE-SECRET")
        self.assertEqual(full_tail[0]["attributes"]["token"], "ATTRIBUTE-SECRET")
        with self.assertRaises(journal.JournalError) as context:
            journal.tail_records(binding, limit=journal.MAX_TAIL_RECORDS + 1)
        self.assertEqual(context.exception.code, "journal.tail_limit")

    def test_tamper_and_partial_record_fail_closed_without_repair(self) -> None:
        binding = self.initialize()
        journal.append_record(
            binding,
            event_type="message.sent",
            attributes={"value": 1},
            now=NOW,
        )
        valid = binding.journal_path.read_bytes()
        record = json.loads(binding.journal_path.read_text(encoding="utf-8"))
        record["attributes"]["value"] = 2
        tampered = (
            json.dumps(record, separators=(",", ":"), sort_keys=True).encode("utf-8")
            + b"\n"
        )
        binding.journal_path.write_bytes(tampered)
        binding.journal_path.chmod(0o600)

        with self.assertRaises(journal.JournalError) as verify_context:
            journal.verify_journal(binding)
        self.assertEqual(verify_context.exception.code, "journal.record_digest")
        with self.assertRaises(journal.JournalError):
            journal.append_record(binding, event_type="message.received")
        self.assertEqual(binding.journal_path.read_bytes(), tampered)

        binding.journal_path.write_bytes(valid + b"{")
        binding.journal_path.chmod(0o600)
        partial = binding.journal_path.read_bytes()
        with self.assertRaises(journal.JournalError) as partial_context:
            journal.verify_journal(binding)
        self.assertEqual(partial_context.exception.code, "journal.partial_record")
        self.assertEqual(binding.journal_path.read_bytes(), partial)

    def test_partial_tail_recovery_archives_exact_bytes_and_preserves_prefix(
        self,
    ) -> None:
        binding = self.initialize()
        original_record = journal.append_record(
            binding,
            event_type="note.before-recovery",
            attributes={"value": 1},
            now=NOW,
        )
        verified_prefix = binding.journal_path.read_bytes()
        damaged = verified_prefix + b'{"incomplete":"record"'
        binding.journal_path.write_bytes(damaged)
        binding.journal_path.chmod(0o600)
        expected_digest = hashlib.sha256(damaged).hexdigest()

        report = journal.inspect_partial_tail(binding)
        self.assertEqual(report.journal_sha256, expected_digest)
        self.assertEqual(report.verified_prefix_bytes, len(verified_prefix))
        self.assertEqual(report.partial_tail_bytes, len(damaged) - len(verified_prefix))
        self.assertEqual(report.prefix_verification.record_count, 1)

        recovery = journal.recover_partial_tail(
            binding,
            expected_journal_sha256=expected_digest,
            confirm_project_id=binding.project_id,
            reason="Injected incomplete write",
            operator_reference="Local operator approved this exact digest",
            now=NOW + dt.timedelta(seconds=1),
        )

        archive_path = Path(recovery.archive_path)
        self.assertEqual(archive_path.read_bytes(), damaged)
        self.assertEqual(mode(archive_path), 0o600)
        self.assertEqual(archive_path.parent.name, "recovery")
        self.assertEqual(mode(archive_path.parent), 0o700)
        recovered_bytes = binding.journal_path.read_bytes()
        self.assertTrue(recovered_bytes.startswith(verified_prefix))
        records = journal.replay_records(binding)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["record_sha256"], original_record["record_sha256"])
        self.assertEqual(records[1]["event_type"], "journal.recovered_partial_tail")
        self.assertEqual(
            records[1]["previous_record_sha256"], original_record["record_sha256"]
        )
        attributes = records[1]["attributes"]
        self.assertEqual(attributes["archive_sha256"], expected_digest)
        self.assertEqual(attributes["partial_tail_sha256"], report.partial_tail_sha256)
        self.assertEqual(journal.verify_journal(binding).record_count, 2)
        self.assertEqual(state.StateStore(binding).snapshot().journal_sequence, 2)
        with self.assertRaises(journal.JournalError) as repeated_context:
            journal.inspect_partial_tail(binding)
        self.assertEqual(repeated_context.exception.code, "journal.recovery_not_needed")

    def test_partial_tail_recovery_reseeds_active_transaction_caches(self) -> None:
        binding = self.initialize()
        journal.append_record(binding, event_type="note.before-recovery", now=NOW)
        store = state.StateStore(binding)

        with project.project_transaction(binding) as transaction:
            self.assertEqual(journal.replay_records(binding)[0]["sequence"], 1)
            self.assertEqual(
                store.snapshot(transaction=transaction).journal_sequence,
                1,
            )
            damaged = binding.journal_path.read_bytes() + b'{"partial"'
            binding.journal_path.write_bytes(damaged)
            binding.journal_path.chmod(0o600)
            recovery = journal.recover_partial_tail(
                binding,
                expected_journal_sha256=hashlib.sha256(damaged).hexdigest(),
                confirm_project_id=binding.project_id,
                reason="Injected in-transaction incomplete write",
                operator_reference="Local operator approved this exact digest",
                now=NOW + dt.timedelta(seconds=1),
                transaction=transaction,
            )

            self.assertEqual(recovery.verification.record_count, 2)
            self.assertEqual(journal.verify_journal(binding).record_count, 2)
            self.assertEqual(
                store.snapshot(transaction=transaction).journal_sequence,
                2,
            )

    def test_partial_tail_recovery_refuses_wrong_digest_and_complete_corruption(
        self,
    ) -> None:
        binding = self.initialize()
        journal.append_record(binding, event_type="note.valid", now=NOW)
        valid = binding.journal_path.read_bytes()

        with self.assertRaises(journal.JournalError) as clean_context:
            journal.inspect_partial_tail(binding)
        self.assertEqual(clean_context.exception.code, "journal.recovery_not_needed")

        partial = valid + b"{"
        binding.journal_path.write_bytes(partial)
        binding.journal_path.chmod(0o600)
        with self.assertRaises(journal.JournalError) as digest_context:
            journal.recover_partial_tail(
                binding,
                expected_journal_sha256="0" * 64,
                confirm_project_id=binding.project_id,
                reason="Wrong digest test",
                operator_reference="Test operator",
                now=NOW,
            )
        self.assertEqual(
            digest_context.exception.code, "journal.recovery_digest_mismatch"
        )
        self.assertEqual(binding.journal_path.read_bytes(), partial)
        self.assertFalse((binding.project_dir / "recovery").exists())

        record = json.loads(valid.decode("utf-8"))
        record["event_type"] = "note.tampered"
        complete_corruption = (
            json.dumps(record, separators=(",", ":"), sort_keys=True).encode("utf-8")
            + b"\n"
        )
        binding.journal_path.write_bytes(complete_corruption)
        binding.journal_path.chmod(0o600)
        with self.assertRaises(journal.JournalError) as complete_context:
            journal.inspect_partial_tail(binding)
        self.assertEqual(complete_context.exception.code, "journal.record_digest")

    def test_partial_tail_recovery_replace_failure_keeps_original_and_archive(
        self,
    ) -> None:
        binding = self.initialize()
        damaged = b'{"incomplete"'
        binding.journal_path.write_bytes(damaged)
        binding.journal_path.chmod(0o600)
        digest = hashlib.sha256(damaged).hexdigest()

        with (
            mock.patch.object(
                journal_recovery.os,
                "replace",
                side_effect=OSError("injected"),
            ),
            self.assertRaises(journal.JournalError) as context,
        ):
            journal.recover_partial_tail(
                binding,
                expected_journal_sha256=digest,
                confirm_project_id=binding.project_id,
                reason="Replacement failure test",
                operator_reference="Test operator",
                now=NOW,
            )

        self.assertEqual(context.exception.code, "journal.recovery_replace")
        self.assertEqual(binding.journal_path.read_bytes(), damaged)
        recovery_files = list(
            (binding.project_dir / "recovery").glob("damaged-*.jsonl")
        )
        self.assertEqual(len(recovery_files), 1)
        self.assertEqual(recovery_files[0].read_bytes(), damaged)
        self.assertFalse(list(binding.project_dir.glob(".journal-recovery-*.tmp")))

    def test_recovery_cleanup_preserves_substituted_temporary_entries(self) -> None:
        binding = self.initialize()
        damaged = b'{"incomplete"'
        binding.journal_path.write_bytes(damaged)
        binding.journal_path.chmod(0o600)
        report = journal.inspect_partial_tail(binding)
        source_descriptor = os.open(binding.journal_path, os.O_RDONLY)
        source_metadata = os.fstat(source_descriptor)
        token = uuid.UUID("00000000-0000-4000-8000-000000000901")
        foreign = b"foreign-substitution"
        try:
            recovery_directory = binding.project_dir / "recovery"
            pending = recovery_directory / f".pending-{token}.jsonl"
            moved_archive = recovery_directory / "owned-pending"

            def substitute_archive(_descriptor: int, *, label: str) -> None:
                self.assertEqual(label, "journal.recovery_archive")
                pending.rename(moved_archive)
                pending.write_bytes(foreign)
                pending.chmod(0o600)
                raise journal.JournalError("proof.failure", "forced failure")

            with (
                mock.patch.object(journal_recovery.uuid, "uuid4", return_value=token),
                mock.patch.object(
                    journal_recovery,
                    "_prepare_created_private_file",
                    side_effect=substitute_archive,
                ),
                self.assertRaises(journal.JournalError) as archive_context,
            ):
                journal_recovery._create_recovery_archive(
                    binding,
                    source_descriptor=source_descriptor,
                    report=report,
                )
            self.assertEqual(archive_context.exception.code, "proof.failure")
            self.assertEqual(pending.read_bytes(), foreign)
            self.assertTrue(moved_archive.exists())

            replacement = binding.project_dir / f".journal-recovery-{token}.tmp"
            moved_replacement = binding.project_dir / "owned-replacement"

            def substitute_replacement(_descriptor: int, *, label: str) -> None:
                self.assertEqual(label, "journal.recovery_replacement")
                replacement.rename(moved_replacement)
                replacement.write_bytes(foreign)
                replacement.chmod(0o600)
                raise journal.JournalError("proof.failure", "forced failure")

            with (
                mock.patch.object(journal_recovery.uuid, "uuid4", return_value=token),
                mock.patch.object(
                    journal_recovery,
                    "_prepare_created_private_file",
                    side_effect=substitute_replacement,
                ),
                self.assertRaises(journal.JournalError) as replacement_context,
            ):
                journal_recovery._replace_partial_journal(
                    binding,
                    source_descriptor=source_descriptor,
                    source_metadata=source_metadata,
                    report=report,
                    recovery_record_raw=b"{}\n",
                )
            self.assertEqual(replacement_context.exception.code, "proof.failure")
            self.assertEqual(replacement.read_bytes(), foreign)
            self.assertTrue(moved_replacement.exists())
        finally:
            os.close(source_descriptor)

    def test_partial_tail_recovery_refuses_a_full_verified_prefix(self) -> None:
        binding = self.initialize()
        journal.append_record(binding, event_type="note.only-slot", now=NOW)
        damaged = binding.journal_path.read_bytes() + b"{"
        binding.journal_path.write_bytes(damaged)
        binding.journal_path.chmod(0o600)

        with (
            mock.patch.object(journal, "MAX_JOURNAL_RECORDS", 1),
            self.assertRaises(journal.JournalError) as context,
        ):
            journal.recover_partial_tail(
                binding,
                expected_journal_sha256=hashlib.sha256(damaged).hexdigest(),
                confirm_project_id=binding.project_id,
                reason="Record limit regression",
                operator_reference="Test operator",
                now=NOW,
            )

        self.assertEqual(context.exception.code, "journal.record_limit")
        self.assertEqual(binding.journal_path.read_bytes(), damaged)
        self.assertFalse((binding.project_dir / "recovery").exists())

    def test_cli_partial_tail_recovery_round_trip(self) -> None:
        initialized = self.run_tool("project", "init")
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        binding = project.resolve_project(self.repo, state_root=self.state_root)
        journal.append_record(binding, event_type="note.before-cli-recovery", now=NOW)
        damaged = binding.journal_path.read_bytes() + b'{"partial"'
        binding.journal_path.write_bytes(damaged)
        binding.journal_path.chmod(0o600)

        status = self.run_tool("journal", "recovery-status")
        self.assertEqual(status.returncode, 0, status.stderr)
        report = json.loads(status.stdout)
        expected_digest = hashlib.sha256(damaged).hexdigest()
        self.assertEqual(report["recovery"]["journal_sha256"], expected_digest)

        recovered = self.run_tool(
            "journal",
            "recover-partial-tail",
            "--expected-journal-sha256",
            expected_digest,
            "--confirm-project-id",
            binding.project_id,
            "--reason",
            "CLI recovery regression",
            "--operator-reference",
            "Local test operator confirmed the digest",
        )
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        payload = json.loads(recovered.stdout)
        self.assertEqual(payload["status"], "recovered_partial_tail")
        self.assertEqual(payload["recovery"]["original_sha256"], expected_digest)
        self.assertEqual(journal.verify_journal(binding).record_count, 2)

    def test_invalid_attributes_and_full_journal_are_rejected(self) -> None:
        binding = self.initialize()
        with self.assertRaises(journal.JournalError) as key_context:
            journal.append_record(
                binding,
                event_type="message.sent",
                attributes={1: "coerced"},  # type: ignore[dict-item]
            )
        self.assertEqual(key_context.exception.code, "journal.json_invalid")
        with self.assertRaises(journal.JournalError) as value_context:
            journal.append_record(
                binding,
                event_type="message.sent",
                attributes={"tuple": (1, 2)},  # type: ignore[dict-item]
            )
        self.assertEqual(value_context.exception.code, "journal.json_invalid")
        with self.assertRaises(journal.JournalError) as unicode_context:
            journal.append_record(
                binding,
                event_type="message.sent",
                attributes={"surrogate": "\ud800"},
            )
        self.assertEqual(unicode_context.exception.code, "journal.json_invalid")
        nested: object = "leaf"
        for _ in range(journal.MAX_ATTRIBUTE_NESTING + 1):
            nested = [nested]
        with self.assertRaises(journal.JournalError) as depth_context:
            journal.append_record(
                binding,
                event_type="message.sent",
                attributes={"nested": nested},  # type: ignore[dict-item]
            )
        self.assertEqual(depth_context.exception.code, "journal.attributes_nesting")

        journal.append_record(binding, event_type="message.sent", now=NOW)
        with (
            mock.patch.object(journal, "MAX_JOURNAL_RECORDS", 1),
            self.assertRaises(journal.JournalError) as full_context,
        ):
            journal.append_record(binding, event_type="message.sent", now=NOW)
        self.assertEqual(full_context.exception.code, "journal.record_limit")
        self.assertEqual(journal.verify_journal(binding).record_count, 1)


if __name__ == "__main__":
    unittest.main()
