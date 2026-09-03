# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import concurrent.futures
import errno
import fcntl
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

from tools import cam1_transport
from tools.cam1lib import (
    product_approval_recovery,
    product_approvals,
    product_executables,
)

APPROVAL_PROCESS_HELPER = (
    Path(__file__).resolve().with_name("_product_approval_process.py")
)


class ProductApprovalRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name) / "account"
        self.home.mkdir(mode=0o700)
        self.home.chmod(0o700)
        self.bin_dir = self.home / "bin"
        self.bin_dir.mkdir(mode=0o700)
        self.marker = self.home / "executed"
        self.executable = self.bin_dir / "claude"
        self.executable.write_text(
            "#!/bin/sh\nprintf executed > " + str(self.marker) + "\n",
            encoding="utf-8",
        )
        self.executable.chmod(0o700)
        account_home_patch = mock.patch.object(
            product_approvals, "account_home", return_value=self.home
        )
        account_home_patch.start()
        self.addCleanup(account_home_patch.stop)

    @property
    def registry(self) -> Path:
        return self.home / "CAM" / "Approvals" / product_approvals.REGISTRY_NAME

    def approve(self) -> dict[str, object]:
        candidate = product_executables.discover_candidate(
            "claude-code", str(self.executable), allow_path_lookup=False
        )
        return product_approvals.approve_candidate(
            vendor="claude-code",
            product_bin=str(self.executable),
            expected_fingerprint_sha256=candidate.fingerprint_sha256,
            operator_reference="direct test operator confirmation",
        )

    def damage_registry(
        self, tail: bytes = b'{"interrupted":'
    ) -> tuple[bytes, bytes, dict[str, object]]:
        self.approve()
        prefix = self.registry.read_bytes()
        damaged = prefix + tail
        self.registry.write_bytes(damaged)
        self.registry.chmod(0o600)
        status = product_approvals.approval_recovery_status()
        return prefix, damaged, status

    @staticmethod
    def recovery_kwargs(status: dict[str, object]) -> dict[str, object]:
        recovery = status["recovery"]
        identity = recovery["registry_identity"]
        return {
            "expected_registry_sha256": recovery["registry_sha256"],
            "expected_registry_bytes": recovery["registry_bytes"],
            "expected_registry_device": identity["device"],
            "expected_registry_inode": identity["inode"],
            "expected_registry_ctime_ns": identity["ctime_ns"],
            "expected_registry_mtime_ns": identity["mtime_ns"],
            "expected_prefix_sha256": recovery["verified_prefix_sha256"],
            "expected_prefix_bytes": recovery["verified_prefix_bytes"],
            "expected_prefix_record_count": recovery["verified_prefix_record_count"],
            "expected_tail_sha256": recovery["partial_tail_sha256"],
            "expected_tail_bytes": recovery["partial_tail_bytes"],
            "reason": "directly reviewed interrupted append",
            "operator_reference": "direct test operator recovery confirmation",
        }

    def invoke_product_cli(self, *arguments: str) -> tuple[int, dict[str, object]]:
        emitted: list[dict[str, object]] = []

        def capture(payload: dict[str, object], **_kwargs: object) -> None:
            emitted.append(payload)

        with (
            mock.patch.object(
                cam1_transport,
                "_require_live_validation_profile",
                return_value=({}, False),
            ),
            mock.patch.object(
                cam1_transport,
                "_with_validation_profile",
                side_effect=lambda payload: payload,
            ),
            mock.patch.object(cam1_transport, "_emit", side_effect=capture),
        ):
            returncode = cam1_transport.main(list(arguments))
        self.assertTrue(emitted)
        return returncode, emitted[-1]

    def test_status_is_read_only_for_missing_empty_and_complete_ledgers(self) -> None:
        missing = product_approvals.approval_recovery_status()
        self.assertEqual(missing["status"], "registry_missing")
        self.assertFalse((self.home / "CAM").exists())

        cam_directory = self.home / "CAM"
        cam_directory.mkdir(mode=0o700)
        self.registry.parent.mkdir(mode=0o700)
        self.registry.write_bytes(b"")
        self.registry.chmod(0o600)
        empty = product_approvals.approval_recovery_status()
        self.assertEqual(empty["status"], "recovery_not_needed")
        self.assertEqual(empty["record_count"], 0)
        self.assertEqual(self.registry.read_bytes(), b"")

        self.approve()
        complete = self.registry.read_bytes()
        status = product_approvals.approval_recovery_status()
        self.assertEqual(status["status"], "recovery_not_needed")
        self.assertFalse(status["recovery_required"])
        self.assertEqual(
            status["reconciled_recovery_evidence"],
            {
                "status": "absent",
                "count": 0,
                "items": [],
                "stale_pending_artifacts": [],
            },
        )
        self.assertEqual(self.registry.read_bytes(), complete)
        self.assertFalse(self.marker.exists())

    def test_partial_tail_recovery_archives_exact_bytes_and_preserves_approval(
        self,
    ) -> None:
        prefix, damaged, status = self.damage_registry()
        approval_id = json.loads(prefix)["record_id"]

        self.assertEqual(status["status"], "recoverable_partial_tail")
        self.assertEqual(self.registry.read_bytes(), damaged)
        result = product_approvals.recover_partial_tail(**self.recovery_kwargs(status))

        archive = Path(result["archive_path"])
        archive_metadata = archive.lstat()
        self.assertTrue(stat.S_ISREG(archive_metadata.st_mode))
        self.assertFalse(archive.is_symlink())
        self.assertEqual(stat.S_IMODE(archive_metadata.st_mode), 0o600)
        self.assertEqual(archive.parent, self.registry.parent)
        self.assertEqual(archive.read_bytes(), damaged)
        self.assertEqual(
            hashlib.sha256(archive.read_bytes()).hexdigest(),
            status["recovery"]["registry_sha256"],
        )
        self.assertTrue(result["active_approvals_unchanged"])
        self.assertEqual(result["mutation_state"], "committed")
        self.assertFalse(self.marker.exists())

        verified = product_approvals.approval_status()
        self.assertEqual(verified["record_count"], 1)
        self.assertEqual(verified["active"][0]["record_id"], approval_id)
        self.assertEqual(self.registry.read_bytes(), prefix)
        # A reader that knows only the original approve/revoke ledger vocabulary
        # can replay the recovered primary file without encountering a new event.
        with self.registry.open("rb") as legacy_handle:
            legacy_records, legacy_bytes = product_approvals._verify(legacy_handle)
        self.assertEqual(legacy_bytes, len(prefix))
        self.assertEqual(
            {record["event_type"] for record in legacy_records},
            {product_approvals.APPROVAL_EVENT},
        )

        manifest_path = Path(result["manifest_path"])
        self.assertEqual(stat.S_IMODE(manifest_path.stat().st_mode), 0o600)
        manifest = product_approval_recovery.parse_recovery_manifest(
            manifest_path.read_bytes()
        )
        self.assertEqual(manifest["status"], "prepared")
        self.assertEqual(
            manifest["partial_tail_sha256"],
            hashlib.sha256(damaged[len(prefix) :]).hexdigest(),
        )

    def test_status_refuses_complete_malformed_and_interior_corruption(self) -> None:
        self.approve()
        valid = self.registry.read_bytes()

        self.registry.write_bytes(valid + b"not-json\n")
        self.registry.chmod(0o600)
        with self.assertRaises(product_approvals.ProductApprovalError) as complete:
            product_approvals.approval_recovery_status()
        self.assertEqual(complete.exception.code, "product_approval.record")

        corrupt = bytearray(valid)
        corrupt[corrupt.index(b"direct test")] = ord("D")
        self.registry.write_bytes(bytes(corrupt) + b'{"interrupted":')
        self.registry.chmod(0o600)
        with self.assertRaises(product_approvals.ProductApprovalError) as interior:
            product_approvals.approval_recovery_status()
        self.assertEqual(interior.exception.code, "product_approval.record_digest")

    def test_status_refuses_noncanonical_digest_and_chain_prefixes(self) -> None:
        approved = self.approve()
        first = json.loads(self.registry.read_text(encoding="utf-8"))
        tail = b'{"interrupted":'

        self.registry.write_bytes(
            product_approvals._canonical_json(first) + b" \n" + tail
        )
        self.registry.chmod(0o600)
        with self.assertRaises(product_approvals.ProductApprovalError) as noncanonical:
            product_approvals.approval_recovery_status()
        self.assertEqual(noncanonical.exception.code, "product_approval.noncanonical")

        altered = json.loads(json.dumps(first))
        altered["attributes"]["operator_reference"] = "altered without digest"
        self.registry.write_bytes(
            product_approvals._canonical_json(altered) + b"\n" + tail
        )
        self.registry.chmod(0o600)
        with self.assertRaises(product_approvals.ProductApprovalError) as digest:
            product_approvals.approval_recovery_status()
        self.assertEqual(digest.exception.code, "product_approval.record_digest")

        self.registry.write_bytes(product_approvals._canonical_json(first) + b"\n")
        self.registry.chmod(0o600)
        product_approvals.revoke_approval(
            vendor="claude-code",
            product_bin=str(self.executable),
            approval_record_id=approved["approval"]["record_id"],
            expected_fingerprint_sha256=approved["approval"]["attributes"][
                "fingerprint_sha256"
            ],
            operator_reference="direct test chain setup",
        )
        lines = self.registry.read_bytes().splitlines()
        second = json.loads(lines[1])
        second["previous_record_sha256"] = "a" * 64
        unsigned = dict(second)
        unsigned.pop("record_sha256")
        second["record_sha256"] = product_approvals._digest(unsigned)
        self.registry.write_bytes(
            lines[0] + b"\n" + product_approvals._canonical_json(second) + b"\n" + tail
        )
        self.registry.chmod(0o600)
        with self.assertRaises(product_approvals.ProductApprovalError) as chain:
            product_approvals.approval_recovery_status()
        self.assertEqual(chain.exception.code, "product_approval.chain")

    def test_recovery_refuses_stale_guards_without_archive_or_mutation(self) -> None:
        _prefix, damaged, status = self.damage_registry()
        changed = damaged[:-1] + b"x"
        self.registry.write_bytes(changed)
        self.registry.chmod(0o600)

        with self.assertRaises(product_approvals.ProductApprovalError) as stale:
            product_approvals.recover_partial_tail(**self.recovery_kwargs(status))
        self.assertEqual(
            stale.exception.code, "product_approval.recovery_guard_mismatch"
        )
        self.assertEqual(self.registry.read_bytes(), changed)
        self.assertEqual(
            list(self.registry.parent.glob("product-executables-v1.damaged-*.jsonl")),
            [],
        )

    def test_recovery_refuses_metadata_drift_with_identical_bytes(self) -> None:
        _prefix, damaged, status = self.damage_registry()
        metadata = self.registry.stat()
        os.utime(
            self.registry,
            ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 1_000_000_000),
        )

        with self.assertRaises(product_approvals.ProductApprovalError) as stale:
            product_approvals.recover_partial_tail(**self.recovery_kwargs(status))
        self.assertEqual(
            stale.exception.code, "product_approval.recovery_guard_mismatch"
        )
        self.assertEqual(self.registry.read_bytes(), damaged)
        self.assertEqual(
            list(self.registry.parent.glob("product-executables-v1.damaged-*.jsonl")),
            [],
        )

    def test_recovery_refuses_oversized_tail_and_ledger(self) -> None:
        _prefix, damaged, _status = self.damage_registry()
        with (
            mock.patch.object(product_approvals, "MAX_RECORD_BYTES", 4),
            self.assertRaises(product_approvals.ProductApprovalError) as tail,
        ):
            product_approvals.approval_recovery_status()
        self.assertEqual(tail.exception.code, "product_approval.recovery_tail_limit")

        with (
            mock.patch.object(
                product_approvals, "MAX_REGISTRY_BYTES", len(damaged) - 1
            ),
            self.assertRaises(product_approvals.ProductApprovalError) as ledger,
        ):
            product_approvals.approval_recovery_status()
        self.assertEqual(ledger.exception.code, "product_approval.registry_limit")
        self.assertEqual(self.registry.read_bytes(), damaged)

    def test_recovery_refuses_reserved_reference_before_archive_creation(self) -> None:
        _prefix, damaged, status = self.damage_registry()
        arguments = self.recovery_kwargs(status)
        arguments["operator_reference"] = "DIRECT_OPERATOR_REFERENCE"
        with self.assertRaises(product_approvals.ProductApprovalError) as reserved:
            product_approvals.recover_partial_tail(**arguments)
        self.assertEqual(
            reserved.exception.code,
            "product_approval.operator_reference_reserved",
        )
        self.assertEqual(self.registry.read_bytes(), damaged)
        self.assertEqual(
            list(self.registry.parent.glob("product-executables-v1.damaged-*.jsonl")),
            [],
        )

    def test_recovery_refuses_registry_inode_substitution(self) -> None:
        _prefix, damaged, status = self.damage_registry()
        original = self.registry.with_name("displaced-registry.jsonl")
        self.registry.rename(original)
        self.registry.write_bytes(damaged)
        self.registry.chmod(0o600)

        with self.assertRaises(product_approvals.ProductApprovalError) as replaced:
            product_approvals.recover_partial_tail(**self.recovery_kwargs(status))
        self.assertEqual(
            replaced.exception.code, "product_approval.recovery_guard_mismatch"
        )
        self.assertEqual(original.read_bytes(), damaged)
        self.assertEqual(self.registry.read_bytes(), damaged)
        self.assertEqual(
            list(self.registry.parent.glob("product-executables-v1.damaged-*.jsonl")),
            [],
        )

    def test_recovery_refuses_registry_path_symlink_substitution(self) -> None:
        _prefix, damaged, status = self.damage_registry()
        original = self.registry.with_name("displaced-registry.jsonl")
        self.registry.rename(original)
        self.registry.symlink_to(original)

        with self.assertRaises(product_approvals.ProductApprovalError) as replaced:
            product_approvals.recover_partial_tail(**self.recovery_kwargs(status))
        self.assertEqual(replaced.exception.code, "product_approval.registry_open")
        self.assertTrue(self.registry.is_symlink())
        self.assertEqual(original.read_bytes(), damaged)
        self.assertEqual(
            list(self.registry.parent.glob("product-executables-v1.damaged-*.jsonl")),
            [],
        )

    def test_recovery_refuses_archive_symlink_without_registry_mutation(self) -> None:
        _prefix, damaged, status = self.damage_registry()
        token = uuid.uuid5(
            product_approval_recovery._RECOVERY_NAMESPACE,
            status["recovery"]["registry_sha256"],
        )
        archive = self.registry.parent / f"product-executables-v1.damaged-{token}.jsonl"
        outside = self.home / "outside"
        outside.write_bytes(b"outside")
        archive.symlink_to(outside)

        with self.assertRaises(product_approvals.ProductApprovalError) as symlink:
            product_approvals.recover_partial_tail(**self.recovery_kwargs(status))
        self.assertEqual(
            symlink.exception.code, "product_approval.recovery_archive_changed"
        )
        self.assertTrue(archive.is_symlink())
        self.assertEqual(outside.read_bytes(), b"outside")
        self.assertEqual(self.registry.read_bytes(), damaged)

    def test_recovery_refuses_manifest_symlink_without_registry_mutation(self) -> None:
        _prefix, damaged, status = self.damage_registry()
        token = uuid.uuid5(
            product_approval_recovery._RECOVERY_NAMESPACE,
            status["recovery"]["registry_sha256"],
        )
        manifest = (
            self.registry.parent / f"product-executables-v1.recovery-{token}.json"
        )
        outside = self.home / "outside-manifest"
        outside.write_bytes(b"outside")
        manifest.symlink_to(outside)

        with self.assertRaises(product_approvals.ProductApprovalError) as symlink:
            product_approvals.recover_partial_tail(**self.recovery_kwargs(status))
        self.assertEqual(
            symlink.exception.code, "product_approval.recovery_manifest_changed"
        )
        self.assertTrue(manifest.is_symlink())
        self.assertEqual(outside.read_bytes(), b"outside")
        self.assertEqual(self.registry.read_bytes(), damaged)

    def test_recovery_refuses_unsafe_registry_permissions(self) -> None:
        _prefix, damaged, _status = self.damage_registry()
        self.registry.chmod(0o644)
        with self.assertRaises(product_approvals.ProductApprovalError) as unsafe:
            product_approvals.approval_recovery_status()
        self.assertEqual(unsafe.exception.code, "approval.registry.mode")
        self.assertEqual(self.registry.read_bytes(), damaged)

    def test_exactly_one_concurrent_recovery_mutates_the_ledger(self) -> None:
        _prefix, damaged, status = self.damage_registry()
        arguments = self.recovery_kwargs(status)

        def recover() -> str:
            try:
                return product_approvals.recover_partial_tail(**arguments)["status"]
            except product_approvals.ProductApprovalError as error:
                return error.code

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(lambda _index: recover(), range(2)))
        self.assertEqual(outcomes.count("recovered_partial_tail"), 1)
        self.assertEqual(outcomes.count("product_approval.recovery_not_needed"), 1)
        self.assertEqual(product_approvals.approval_status()["record_count"], 1)
        archives = list(
            self.registry.parent.glob("product-executables-v1.damaged-*.jsonl")
        )
        self.assertEqual(len(archives), 1)
        self.assertEqual(archives[0].read_bytes(), damaged)

    def test_abrupt_interruption_after_truncate_leaves_valid_prefix_and_evidence(
        self,
    ) -> None:
        prefix, damaged, status = self.damage_registry()
        original_truncate = product_approval_recovery.os.ftruncate

        def interrupted_truncate(descriptor: int, length: int) -> None:
            original_truncate(descriptor, length)
            raise KeyboardInterrupt

        with (
            mock.patch.object(
                product_approval_recovery.os,
                "ftruncate",
                side_effect=interrupted_truncate,
            ),
            self.assertRaises(KeyboardInterrupt),
        ):
            product_approvals.recover_partial_tail(**self.recovery_kwargs(status))

        self.assertEqual(self.registry.read_bytes(), prefix)
        follow_up = product_approvals.approval_recovery_status()
        self.assertEqual(follow_up["status"], "recovery_not_needed")
        self.assertEqual(
            follow_up["reconciled_recovery_evidence"]["status"], "reconciled"
        )
        self.assertEqual(follow_up["reconciled_recovery_evidence"]["count"], 1)
        self.assertEqual(
            follow_up["reconciled_recovery_evidence"]["items"][0]["relationship"],
            "exact_verified_prefix",
        )
        archives = list(
            self.registry.parent.glob("product-executables-v1.damaged-*.jsonl")
        )
        self.assertEqual(len(archives), 1)
        self.assertEqual(archives[0].read_bytes(), damaged)
        manifests = list(
            self.registry.parent.glob("product-executables-v1.recovery-*.json")
        )
        self.assertEqual(len(manifests), 1)
        manifest = product_approval_recovery.parse_recovery_manifest(
            manifests[0].read_bytes()
        )
        self.assertEqual(
            manifest["verified_prefix_sha256"], hashlib.sha256(prefix).hexdigest()
        )

    def test_existing_prepared_evidence_is_reused_after_pretruncate_failure(
        self,
    ) -> None:
        prefix, damaged, status = self.damage_registry()
        arguments = self.recovery_kwargs(status)
        with (
            mock.patch.object(
                product_approval_recovery,
                "_revalidate_registry_path",
                side_effect=product_approvals.ProductApprovalError(
                    "synthetic.pretruncate", "synthetic pretruncate failure"
                ),
            ),
            self.assertRaises(
                product_approval_recovery.RecoveryMutationError
            ) as failed,
        ):
            product_approvals.recover_partial_tail(**arguments)
        self.assertEqual(failed.exception.audit["mutation_state"], "not_attempted")
        self.assertEqual(self.registry.read_bytes(), damaged)
        self.assertEqual(
            product_approvals.approval_recovery_status()["existing_recovery_evidence"][
                "status"
            ],
            "prepared",
        )
        before = sorted(path.name for path in self.registry.parent.iterdir())
        result = product_approvals.recover_partial_tail(**arguments)
        after = sorted(path.name for path in self.registry.parent.iterdir())
        self.assertEqual(before, after)
        self.assertEqual(self.registry.read_bytes(), prefix)
        self.assertEqual(result["mutation_state"], "committed")

    def test_manifest_publication_rejects_suffix_append_before_mutation(self) -> None:
        _prefix, damaged, status = self.damage_registry()
        evidence = product_approval_recovery._evidence
        real_rename = evidence.rename_noreplace

        def append_after_publish(
            directory_descriptor: int,
            source_name: str,
            destination_name: str,
        ) -> None:
            real_rename(directory_descriptor, source_name, destination_name)
            if destination_name.endswith(evidence.MANIFEST_SUFFIX):
                descriptor = os.open(
                    destination_name,
                    os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=directory_descriptor,
                )
                try:
                    os.write(descriptor, b"x")
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)

        with (
            mock.patch.object(
                evidence,
                "rename_noreplace",
                side_effect=append_after_publish,
            ),
            self.assertRaises(
                product_approval_recovery.RecoveryMutationError
            ) as changed,
        ):
            product_approvals.recover_partial_tail(**self.recovery_kwargs(status))
        self.assertEqual(
            changed.exception.code,
            "product_approval.recovery_manifest_changed",
        )
        self.assertEqual(changed.exception.audit["mutation_state"], "not_attempted")
        self.assertEqual(self.registry.read_bytes(), damaged)

    def test_evidence_is_reinspected_immediately_before_truncate(self) -> None:
        _prefix, damaged, status = self.damage_registry()
        token = uuid.uuid5(
            product_approval_recovery._RECOVERY_NAMESPACE,
            status["recovery"]["registry_sha256"],
        )
        manifest = (
            self.registry.parent / f"product-executables-v1.recovery-{token}.json"
        )
        real_revalidate = product_approval_recovery._revalidate_registry_path
        appended = False

        def alter_after_registry_revalidation(
            registry: Path,
            descriptor: int,
            report: product_approval_recovery.PartialApprovalTailReport,
        ) -> None:
            nonlocal appended
            real_revalidate(registry, descriptor, report)
            if not appended:
                appended = True
                with manifest.open("ab") as handle:
                    handle.write(b"x")
                    handle.flush()
                    os.fsync(handle.fileno())

        with (
            mock.patch.object(
                product_approval_recovery,
                "_revalidate_registry_path",
                side_effect=alter_after_registry_revalidation,
            ),
            self.assertRaises(
                product_approval_recovery.RecoveryMutationError
            ) as changed,
        ):
            product_approvals.recover_partial_tail(**self.recovery_kwargs(status))
        self.assertEqual(changed.exception.audit["mutation_state"], "not_attempted")
        self.assertEqual(
            changed.exception.code,
            "product_approval.recovery_manifest",
        )
        self.assertEqual(self.registry.read_bytes(), damaged)

    def test_manifest_parser_enforces_text_timestamp_and_recovery_identity(
        self,
    ) -> None:
        _prefix, _damaged, status = self.damage_registry()
        result = product_approvals.recover_partial_tail(**self.recovery_kwargs(status))
        original = dict(result["recovery_manifest"])

        def encoded(**changes: object) -> bytes:
            manifest = {**original, **changes}
            unsigned = dict(manifest)
            unsigned.pop("record_sha256")
            manifest["record_sha256"] = product_approvals._digest(unsigned)
            return product_approvals._canonical_json(manifest)

        with self.assertRaises(product_approvals.ProductApprovalError) as unsafe_text:
            product_approval_recovery.parse_recovery_manifest(
                encoded(operator_reference="direct\u0600operator confirmation")
            )
        self.assertEqual(unsafe_text.exception.code, "product_approval.field")

        with self.assertRaises(product_approvals.ProductApprovalError) as timestamp:
            product_approval_recovery.parse_recovery_manifest(
                encoded(prepared_at="2026-09-02T00:00:00Z")
            )
        self.assertEqual(timestamp.exception.code, "product_approval.timestamp")

        different = str(uuid.uuid4())
        with self.assertRaises(product_approvals.ProductApprovalError) as identity:
            product_approval_recovery.parse_recovery_manifest(
                encoded(
                    recovery_id=different,
                    archive_file=(f"product-executables-v1.damaged-{different}.jsonl"),
                )
            )
        self.assertEqual(identity.exception.code, "product_approval.recovery_manifest")

    def test_committed_cleanup_failure_returns_reconciliation_result(self) -> None:
        prefix, _damaged, status = self.damage_registry()
        real_flock = product_approval_recovery.fcntl.flock

        def fail_after_unlock(descriptor: int, operation: int) -> None:
            real_flock(descriptor, operation)
            if operation == product_approval_recovery.fcntl.LOCK_UN:
                raise OSError(errno.EIO, "synthetic unlock failure")

        with mock.patch.object(
            product_approval_recovery.fcntl,
            "flock",
            side_effect=fail_after_unlock,
        ):
            result = product_approvals.recover_partial_tail(
                **self.recovery_kwargs(status)
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "recovery_committed_cleanup_uncertain")
        self.assertEqual(result["mutation_state"], "committed")
        self.assertEqual(
            result["cleanup_errors"],
            [{"operation": "registry_unlock", "error_type": "OSError"}],
        )
        self.assertEqual(self.registry.read_bytes(), prefix)
        self.assertTrue(Path(result["archive_path"]).exists())
        self.assertTrue(Path(result["manifest_path"]).exists())

    def test_postcommit_registry_path_substitution_is_not_success(self) -> None:
        prefix, _damaged, status = self.damage_registry()
        real_verify = product_approvals._verify
        calls = 0
        displaced = self.registry.with_name("repaired-displaced.jsonl")

        def substitute_after_final_verify(
            handle: object,
        ) -> tuple[list[dict[str, object]], int]:
            nonlocal calls
            calls += 1
            result = real_verify(handle)
            if calls == 3:
                self.registry.rename(displaced)
                self.registry.write_bytes(prefix)
                self.registry.chmod(0o600)
            return result

        with mock.patch.object(
            product_approvals,
            "_verify",
            side_effect=substitute_after_final_verify,
        ):
            result = product_approvals.recover_partial_tail(
                **self.recovery_kwargs(status)
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "recovery_committed_verification_uncertain")
        self.assertEqual(result["mutation_state"], "committed")
        self.assertEqual(
            result["verification_error"]["code"],
            "product_approval.recovery_changed",
        )
        self.assertEqual(displaced.read_bytes(), prefix)
        self.assertEqual(self.registry.read_bytes(), prefix)

    def test_valid_primary_reconciles_evidence_after_later_append(self) -> None:
        approved = self.approve()
        prefix = self.registry.read_bytes()
        self.registry.write_bytes(prefix + b'{"interrupted":')
        self.registry.chmod(0o600)
        status = product_approvals.approval_recovery_status()
        product_approvals.recover_partial_tail(**self.recovery_kwargs(status))
        product_approvals.revoke_approval(
            vendor="claude-code",
            product_bin=str(self.executable),
            approval_record_id=approved["approval"]["record_id"],
            expected_fingerprint_sha256=approved["approval"]["attributes"][
                "fingerprint_sha256"
            ],
            operator_reference="direct test post-recovery revocation",
        )

        reconciled = product_approvals.approval_recovery_status()
        self.assertEqual(reconciled["status"], "recovery_not_needed")
        evidence = reconciled["reconciled_recovery_evidence"]
        self.assertEqual(evidence["status"], "reconciled")
        self.assertEqual(evidence["count"], 1)
        self.assertEqual(
            evidence["items"][0]["relationship"],
            "later_valid_ledger_extends_prefix",
        )

    def test_empty_repaired_prefix_is_reconciled(self) -> None:
        cam_directory = self.home / "CAM"
        cam_directory.mkdir(mode=0o700)
        self.registry.parent.mkdir(mode=0o700)
        damaged = b'{"interrupted":'
        self.registry.write_bytes(damaged)
        self.registry.chmod(0o600)
        status = product_approvals.approval_recovery_status()
        self.assertEqual(status["recovery"]["verified_prefix_bytes"], 0)
        product_approvals.recover_partial_tail(**self.recovery_kwargs(status))

        reconciled = product_approvals.approval_recovery_status()
        self.assertEqual(reconciled["registry_bytes"], 0)
        self.assertEqual(
            reconciled["reconciled_recovery_evidence"]["items"][0]["relationship"],
            "exact_verified_prefix",
        )

    def test_valid_primary_refuses_malformed_or_symlinked_manifests(self) -> None:
        self.approve()
        malformed = self.registry.parent / (
            f"product-executables-v1.recovery-{uuid.uuid4()}.json"
        )
        malformed.write_bytes(b"{}")
        malformed.chmod(0o600)
        with self.assertRaises(product_approvals.ProductApprovalError) as invalid:
            product_approvals.approval_recovery_status()
        self.assertEqual(invalid.exception.code, "product_approval.recovery_manifest")

        malformed.unlink()
        outside = self.home / "outside-recovery-manifest"
        outside.write_bytes(b"{}")
        malformed.symlink_to(outside)
        with self.assertRaises(product_approvals.ProductApprovalError) as symlink:
            product_approvals.approval_recovery_status()
        self.assertEqual(
            symlink.exception.code,
            "product_approval.recovery_manifest_changed",
        )

    def test_valid_primary_refuses_unbounded_manifest_scan(self) -> None:
        self.approve()
        evidence = product_approval_recovery._evidence
        for _index in range(evidence.MAX_RECOVERY_MANIFESTS + 1):
            path = self.registry.parent / (
                f"product-executables-v1.recovery-{uuid.uuid4()}.json"
            )
            path.write_bytes(b"{}")
            path.chmod(0o600)
        with self.assertRaises(product_approvals.ProductApprovalError) as bounded:
            product_approvals.approval_recovery_status()
        self.assertEqual(
            bounded.exception.code,
            "product_approval.recovery_manifest_limit",
        )

    def test_valid_primary_refuses_prepared_prefix_mismatch(self) -> None:
        prefix, _damaged, status = self.damage_registry()
        product_approvals.recover_partial_tail(**self.recovery_kwargs(status))
        replacement = json.loads(prefix)
        replacement["attributes"]["operator_reference"] = (
            "different direct operator confirmation"
        )
        unsigned = dict(replacement)
        unsigned.pop("record_sha256")
        replacement["record_sha256"] = product_approvals._digest(unsigned)
        self.registry.write_bytes(
            product_approvals._canonical_json(replacement) + b"\n"
        )
        self.registry.chmod(0o600)

        with self.assertRaises(product_approvals.ProductApprovalError) as mismatch:
            product_approvals.approval_recovery_status()
        self.assertEqual(
            mismatch.exception.code,
            "product_approval.recovery_evidence_mismatch",
        )

    def test_status_reports_stale_reserved_pending_artifacts(self) -> None:
        self.approve()
        pending = self.registry.parent / ".product-approval-recovery-stale.pending"
        pending.write_bytes(b"stale")
        pending.chmod(0o600)
        status = product_approvals.approval_recovery_status()
        self.assertEqual(
            status["reconciled_recovery_evidence"]["stale_pending_artifacts"],
            [str(pending)],
        )

    def test_partial_status_reports_stale_reserved_pending_artifacts(self) -> None:
        _prefix, damaged, _status = self.damage_registry()
        pending = self.registry.parent / ".product-approval-recovery-stale.pending"
        pending.write_bytes(b"stale")
        pending.chmod(0o600)
        status = product_approvals.approval_recovery_status()
        self.assertEqual(status["status"], "recoverable_partial_tail")
        self.assertEqual(
            status["existing_recovery_evidence"]["stale_pending_artifacts"],
            [str(pending)],
        )
        self.assertEqual(self.registry.read_bytes(), damaged)

    def test_public_transport_facade_exports_recovery_operations(self) -> None:
        self.assertIs(
            cam1_transport.product_recovery_status,
            cam1_transport._products.product_recovery_status,
        )
        self.assertIs(
            cam1_transport.recover_product_partial_tail,
            cam1_transport._products.recover_product_partial_tail,
        )

    def test_reason_bound_matches_manifest_schema(self) -> None:
        _prefix, damaged, status = self.damage_registry()
        arguments = self.recovery_kwargs(status)
        arguments["reason"] = "r" * 600
        with self.assertRaises(product_approvals.ProductApprovalError) as runtime:
            product_approvals.recover_partial_tail(**arguments)
        self.assertEqual(runtime.exception.code, "product_approval.field")
        self.assertEqual(runtime.exception.audit["mutation_state"], "not_attempted")
        self.assertEqual(self.registry.read_bytes(), damaged)

        arguments["reason"] = "r" * 500
        recovered = product_approvals.recover_partial_tail(**arguments)
        manifest = dict(recovered["recovery_manifest"])
        manifest["reason"] = "r" * 600
        unsigned = dict(manifest)
        unsigned.pop("record_sha256")
        manifest["record_sha256"] = product_approvals._digest(unsigned)
        with self.assertRaises(product_approvals.ProductApprovalError):
            product_approval_recovery.parse_recovery_manifest(
                product_approvals._canonical_json(manifest)
            )

    def test_fsync_failure_after_truncate_reports_unknown_mutation(self) -> None:
        prefix, damaged, status = self.damage_registry()
        real_fsync = product_approval_recovery.os.fsync
        registry_inode = self.registry.stat().st_ino

        def fail_registry_fsync(descriptor: int) -> None:
            if os.fstat(descriptor).st_ino == registry_inode and os.fstat(
                descriptor
            ).st_size == len(prefix):
                raise OSError(errno.EIO, "synthetic commit fsync failure")
            real_fsync(descriptor)

        with (
            mock.patch.object(
                product_approval_recovery.os,
                "fsync",
                side_effect=fail_registry_fsync,
            ),
            self.assertRaises(
                product_approval_recovery.RecoveryMutationError
            ) as uncertain,
        ):
            product_approvals.recover_partial_tail(**self.recovery_kwargs(status))
        self.assertEqual(uncertain.exception.audit["mutation_state"], "unknown")
        self.assertEqual(self.registry.read_bytes(), prefix)
        self.assertNotEqual(self.registry.read_bytes(), damaged)
        self.assertEqual(
            uncertain.exception.audit["reconciliation_arguments"],
            ["product-recovery-status"],
        )
        self.assertTrue(Path(uncertain.exception.audit["archive_path"]).exists())
        self.assertTrue(Path(uncertain.exception.audit["manifest_path"]).exists())

    def test_postcommit_verification_failure_returns_uncertain_result(self) -> None:
        prefix, _damaged, status = self.damage_registry()
        real_verify = product_approvals._verify
        calls = 0

        def fail_final_verification(
            handle: object,
        ) -> tuple[list[dict[str, object]], int]:
            nonlocal calls
            calls += 1
            if calls >= 3:
                raise OSError(errno.EIO, "synthetic final verification failure")
            return real_verify(handle)

        arguments = list(status["recovery_arguments"])
        arguments[arguments.index("DIRECT_OPERATOR_REFERENCE")] = (
            "direct postcommit verification test"
        )
        arguments[
            arguments.index("Describe the observed interrupted approval-ledger append")
        ] = "postcommit verification test"
        with mock.patch.object(
            product_approvals, "_verify", side_effect=fail_final_verification
        ):
            returncode, result = self.invoke_product_cli(*arguments)
        self.assertEqual(returncode, 3)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "recovery_committed_verification_uncertain")
        self.assertEqual(result["mutation_state"], "committed")
        self.assertEqual(self.registry.read_bytes(), prefix)
        self.assertTrue(Path(result["archive_path"]).exists())
        self.assertTrue(Path(result["manifest_path"]).exists())
        self.assertEqual(
            result["reconciliation_arguments"], ["product-recovery-status"]
        )
        self.assertIn("reconciliation_command", result)
        with self.assertRaises(product_approvals.ProductApprovalError) as stale:
            product_approvals.recover_partial_tail(**self.recovery_kwargs(status))
        self.assertEqual(stale.exception.code, "product_approval.recovery_not_needed")

    def test_ordinary_status_directs_only_bounded_eof_tail_to_recovery(self) -> None:
        self.approve()
        complete = self.registry.read_bytes()
        self.registry.write_bytes(complete + b'{"interrupted":')
        self.registry.chmod(0o600)
        with self.assertRaises(product_approvals.ProductApprovalError) as partial:
            product_approvals.approval_status()
        self.assertEqual(partial.exception.code, "product_approval.recovery_required")

        self.registry.write_bytes(complete + b"not-json\n")
        self.registry.chmod(0o600)
        with self.assertRaises(product_approvals.ProductApprovalError) as malformed:
            product_approvals.approval_status()
        self.assertEqual(malformed.exception.code, "product_approval.record")

    def test_created_registry_remains_linked_when_creator_loses_lock(self) -> None:
        candidate = product_executables.discover_candidate(
            "claude-code", str(self.executable), allow_path_lookup=False
        )
        published = self.home / "registry-published"
        proceed = self.home / "creator-proceed"
        process = subprocess.Popen(
            [
                sys.executable,
                str(APPROVAL_PROCESS_HELPER),
                "create-lock-timeout",
                str(self.home),
                str(published),
                str(proceed),
                "claude-code",
                str(self.executable),
                candidate.fingerprint_sha256,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + 10
        while not published.exists():
            if time.monotonic() >= deadline:
                process.kill()
                self.fail("creator did not publish the registry")
            time.sleep(0.005)
        descriptor = os.open(self.registry, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            winner_inode = os.fstat(descriptor).st_ino
            proceed.touch(mode=0o600)
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, stderr)
            self.assertEqual(
                json.loads(stdout)["code"], "product_approval.lock_timeout"
            )
            os.write(descriptor, b"winner-owned-inode\n")
            os.fsync(descriptor)
            self.assertTrue(self.registry.exists())
            self.assertEqual(self.registry.stat().st_ino, winner_inode)
            self.assertEqual(self.registry.read_bytes(), b"winner-owned-inode\n")
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def test_registry_lock_wait_is_bounded(self) -> None:
        self.approve()

        def contended(_descriptor: int, operation: int) -> None:
            if operation & product_approval_recovery.fcntl.LOCK_NB:
                raise OSError(errno.EAGAIN, "busy")

        with (
            mock.patch.object(
                product_approval_recovery.fcntl, "flock", side_effect=contended
            ),
            mock.patch.object(
                product_approvals, "REGISTRY_LOCK_TIMEOUT_SECONDS", 0.001
            ),
            mock.patch.object(product_approvals, "REGISTRY_LOCK_POLL_SECONDS", 0.0001),
            self.assertRaises(product_approvals.ProductApprovalError) as busy,
        ):
            product_approvals.approval_status()
        self.assertEqual(busy.exception.code, "product_approval.lock_timeout")

        damaged = self.registry.read_bytes() + b'{"interrupted":'
        self.registry.write_bytes(damaged)
        self.registry.chmod(0o600)
        status = product_approvals.approval_recovery_status()
        with (
            mock.patch.object(
                product_approval_recovery.fcntl, "flock", side_effect=contended
            ),
            mock.patch.object(
                product_approvals, "REGISTRY_LOCK_TIMEOUT_SECONDS", 0.001
            ),
            mock.patch.object(product_approvals, "REGISTRY_LOCK_POLL_SECONDS", 0.0001),
            self.assertRaises(product_approvals.ProductApprovalError) as exclusive,
        ):
            product_approvals.recover_partial_tail(**self.recovery_kwargs(status))
        self.assertEqual(exclusive.exception.code, "product_approval.lock_timeout")
        self.assertEqual(self.registry.read_bytes(), damaged)

    def test_competing_process_lock_blocks_recovery_without_mutation(self) -> None:
        _prefix, damaged, status = self.damage_registry()
        ready = self.home / "registry-lock-ready"
        release = self.home / "registry-lock-release"
        process = subprocess.Popen(
            [
                sys.executable,
                str(APPROVAL_PROCESS_HELPER),
                "hold-registry-lock",
                str(self.home),
                str(ready),
                str(release),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.monotonic() + 10
            while not ready.exists():
                if time.monotonic() >= deadline:
                    process.kill()
                    self.fail("competing process did not acquire the registry lock")
                time.sleep(0.005)
            with (
                mock.patch.object(
                    product_approvals,
                    "REGISTRY_LOCK_TIMEOUT_SECONDS",
                    0.05,
                ),
                mock.patch.object(
                    product_approvals,
                    "REGISTRY_LOCK_POLL_SECONDS",
                    0.005,
                ),
                self.assertRaises(product_approvals.ProductApprovalError) as busy,
            ):
                product_approvals.recover_partial_tail(**self.recovery_kwargs(status))
            self.assertEqual(busy.exception.code, "product_approval.lock_timeout")
            self.assertEqual(busy.exception.audit["mutation_state"], "not_attempted")
            self.assertEqual(self.registry.read_bytes(), damaged)
            self.assertEqual(
                list(
                    self.registry.parent.glob("product-executables-v1.damaged-*.jsonl")
                ),
                [],
            )
        finally:
            release.touch(mode=0o600)
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, stderr)
            self.assertEqual(json.loads(stdout)["status"], "released")

    def test_cli_requires_status_then_exact_guards(self) -> None:
        self.approve()
        damaged = self.registry.read_bytes() + b'{"interrupted":'
        self.registry.write_bytes(damaged)
        self.registry.chmod(0o600)

        returncode, inspected = self.invoke_product_cli("product-recovery-status")
        self.assertEqual(returncode, 0)
        self.assertEqual(inspected["status"], "recoverable_partial_tail")
        self.assertEqual(self.registry.read_bytes(), damaged)
        arguments = list(inspected["recovery_arguments"])
        self.assertEqual(inspected["recovery_command"][-len(arguments) :], arguments)
        self.assertIn(
            "product-recover-partial-tail", inspected["recovery_command_text"]
        )
        arguments[arguments.index("DIRECT_OPERATOR_REFERENCE")] = (
            "direct CLI recovery confirmation"
        )
        arguments[
            arguments.index("Describe the observed interrupted approval-ledger append")
        ] = "CLI test interrupted append"
        with mock.patch.object(
            product_approval_recovery._evidence,
            "_write_all",
            side_effect=OSError(errno.ENOSPC, "synthetic archive failure"),
        ):
            returncode, failed = self.invoke_product_cli(*arguments)
        self.assertEqual(returncode, 2)
        self.assertEqual(failed["error"]["code"], "product_approval.recovery_io")
        self.assertEqual(failed["audit"]["mutation_state"], "not_attempted")
        self.assertEqual(self.registry.read_bytes(), damaged)
        self.assertEqual(
            list(self.registry.parent.glob("product-executables-v1.damaged-*.jsonl")),
            [],
        )

        stale_arguments = list(arguments)
        stale_arguments[
            stale_arguments.index(inspected["recovery"]["registry_sha256"])
        ] = "f" * 64
        returncode, rejected = self.invoke_product_cli(*stale_arguments)
        self.assertEqual(returncode, 2)
        self.assertEqual(
            rejected["error"]["code"],
            "product_approval.recovery_guard_mismatch",
        )
        self.assertEqual(self.registry.read_bytes(), damaged)

        returncode, recovered = self.invoke_product_cli(*arguments)
        self.assertEqual(returncode, 0)
        self.assertEqual(recovered["status"], "recovered_partial_tail")
        self.assertEqual(recovered["mutation_state"], "committed")
        self.assertTrue(recovered["active_approvals_unchanged"])
        self.assertEqual(product_approvals.approval_status()["record_count"], 1)
        self.assertFalse(self.marker.exists())


if __name__ == "__main__":
    unittest.main()
