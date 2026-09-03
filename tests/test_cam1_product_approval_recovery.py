# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import concurrent.futures
import errno
import hashlib
import json
import os
import stat
import tempfile
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
        self.assertFalse(self.marker.exists())

        verified = product_approvals.approval_status()
        self.assertEqual(verified["record_count"], 2)
        self.assertEqual(verified["active"][0]["record_id"], approval_id)
        self.assertTrue(self.registry.read_bytes().startswith(prefix))
        records = [json.loads(line) for line in self.registry.read_bytes().splitlines()]
        recovery_record = records[-1]
        self.assertEqual(
            recovery_record["event_type"], product_approvals.RECOVERY_EVENT
        )
        self.assertEqual(recovery_record["sequence"], 2)
        self.assertEqual(
            recovery_record["previous_record_sha256"], records[0]["record_sha256"]
        )
        self.assertEqual(
            recovery_record["attributes"]["partial_tail_sha256"],
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
        token = uuid.UUID("00000000-0000-4000-8000-000000000099")
        archive = self.registry.parent / f"product-executables-v1.damaged-{token}.jsonl"
        outside = self.home / "outside"
        outside.write_bytes(b"outside")
        archive.symlink_to(outside)

        with (
            mock.patch.object(
                product_approval_recovery.uuid, "uuid4", return_value=token
            ),
            self.assertRaises(product_approvals.ProductApprovalError) as symlink,
        ):
            product_approvals.recover_partial_tail(**self.recovery_kwargs(status))
        self.assertEqual(
            symlink.exception.code, "product_approval.recovery_archive_exists"
        )
        self.assertTrue(archive.is_symlink())
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
        self.assertEqual(product_approvals.approval_status()["record_count"], 2)
        archives = list(
            self.registry.parent.glob("product-executables-v1.damaged-*.jsonl")
        )
        self.assertEqual(len(archives), 1)
        self.assertEqual(archives[0].read_bytes(), damaged)

    def test_abrupt_interruption_leaves_a_recoverable_partial_tail(self) -> None:
        prefix, damaged, status = self.damage_registry()
        original_write = product_approval_recovery._write_all
        calls = 0

        def interrupted_write(descriptor: int, raw: bytes) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                original_write(descriptor, raw)
                return
            original_write(descriptor, raw[:17])
            raise KeyboardInterrupt

        with (
            mock.patch.object(
                product_approval_recovery, "_write_all", side_effect=interrupted_write
            ),
            self.assertRaises(KeyboardInterrupt),
        ):
            product_approvals.recover_partial_tail(**self.recovery_kwargs(status))

        interrupted = self.registry.read_bytes()
        self.assertTrue(interrupted.startswith(prefix))
        self.assertGreater(len(interrupted), len(prefix))
        follow_up = product_approvals.approval_recovery_status()
        self.assertEqual(follow_up["status"], "recoverable_partial_tail")
        self.assertEqual(follow_up["recovery"]["verified_prefix_bytes"], len(prefix))
        archives = list(
            self.registry.parent.glob("product-executables-v1.damaged-*.jsonl")
        )
        self.assertEqual(len(archives), 1)
        self.assertEqual(archives[0].read_bytes(), damaged)
        self.assertEqual(
            follow_up["recovery"]["partial_tail_sha256"],
            hashlib.sha256(interrupted[len(prefix) :]).hexdigest(),
        )

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
            product_approval_recovery,
            "_write_all",
            side_effect=OSError(errno.ENOSPC, "synthetic archive failure"),
        ):
            returncode, failed = self.invoke_product_cli(*arguments)
        self.assertEqual(returncode, 2)
        self.assertEqual(failed["error"]["code"], "product_approval.recovery_io")
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
        self.assertTrue(recovered["active_approvals_unchanged"])
        self.assertEqual(product_approvals.approval_status()["record_count"], 2)
        self.assertFalse(self.marker.exists())


if __name__ == "__main__":
    unittest.main()
