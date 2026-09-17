# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import concurrent.futures
import json
import os
import subprocess
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from tests._native_executable_fixture import write_native
from tools import cam1_transport
from tools.cam1lib import (
    product_approvals,
    product_executables,
    project,
)

APPROVAL_PROCESS_HELPER = (
    Path(__file__).resolve().with_name("_product_approval_process.py")
)


if __package__:
    from ._product_approval_test_case import ProductApprovalTestCase
else:
    from _product_approval_test_case import ProductApprovalTestCase


class ProductApprovalTests(ProductApprovalTestCase):
    def test_strict_device_only_drift_names_field_without_reapproval(self) -> None:
        self.approve()
        registry = product_approvals.registry_path()
        before = registry.read_bytes()
        observed = self.discover()
        fingerprint = replace(observed.fingerprint, dev=observed.fingerprint.dev + 2)
        changed = replace(
            observed,
            fingerprint=fingerprint,
            fingerprint_sha256=product_executables._candidate_digest(
                observed.vendor, observed.canonical_path, fingerprint
            ),
        )
        product_approvals.begin_operation()
        with mock.patch.object(
            product_approvals, "discover_candidate", return_value=changed
        ):
            with self.assertRaises(product_approvals.ProductApprovalError) as error:
                product_approvals.require_approved_executable(
                    vendor="claude-code", product_bin=str(self.executable)
                )
        self.assertEqual(error.exception.code, "product_approval.drift")
        self.assertIn("changed fields: dev)", error.exception.detail)
        self.assertEqual(registry.read_bytes(), before)

    def test_cached_strict_device_drift_names_field_before_launch(self) -> None:
        self.approve()
        product_approvals.require_approved_executable(
            vendor="claude-code", product_bin=str(self.executable)
        )
        observed = product_approvals._metadata_opened(self.executable.resolve())
        observed["dev"] += 2
        with mock.patch.object(
            product_approvals, "_metadata_opened", return_value=observed
        ):
            for check in (
                product_approvals.require_approved_executable,
                product_approvals.require_approved_metadata,
            ):
                with self.subTest(check=check.__name__):
                    with self.assertRaises(
                        product_approvals.ProductApprovalError
                    ) as error:
                        check(vendor="claude-code", product_bin=str(self.executable))
                    self.assertEqual(error.exception.code, "product_approval.drift")
                    self.assertIn("changed fields: dev)", error.exception.detail)

    def test_discovery_approval_status_and_require_never_execute_candidate(
        self,
    ) -> None:
        with mock.patch("subprocess.run") as spawn:
            candidate = self.discover()
        spawn.assert_not_called()
        self.assertFalse(self.marker.exists())
        result = self.approve()
        self.assertEqual(result["status"], "approved")
        status = product_approvals.approval_status(vendor="claude-code")
        self.assertEqual(len(status["active"]), 1)
        resolved, approval = product_approvals.require_approved_executable(
            vendor="claude-code",
            product_bin=str(self.executable),
        )
        self.assertEqual(resolved, candidate.canonical_path)
        self.assertEqual(approval["fingerprint_sha256"], candidate.fingerprint_sha256)
        self.assertFalse(self.marker.exists())

    def test_record_limit_rejects_append_without_poisoning_registry(self) -> None:
        second_executable = self.bin_dir / "codex"
        write_native(second_executable)
        second_executable.chmod(0o700)
        second_candidate = product_executables.discover_candidate(
            "codex",
            str(second_executable),
            allow_path_lookup=False,
        )

        with mock.patch.object(product_approvals, "MAX_REGISTRY_RECORDS", 1):
            self.approve()
            registry = product_approvals.registry_path()
            original = registry.read_bytes()
            with self.assertRaises(product_approvals.ProductApprovalError) as context:
                product_approvals.approve_candidate(
                    vendor="codex",
                    product_bin=str(second_executable),
                    expected_fingerprint_sha256=(second_candidate.fingerprint_sha256),
                    operator_reference="direct test operator confirmation",
                )
            self.assertEqual(
                context.exception.code,
                "product_approval.registry_limit",
            )
            self.assertEqual(registry.read_bytes(), original)
            status = product_approvals.approval_status()

        self.assertEqual(status["record_count"], 1)
        self.assertEqual(len(status["active"]), 1)

    def test_path_is_candidate_only_and_approval_requires_absolute_path(self) -> None:
        with mock.patch.dict(os.environ, {"PATH": str(self.bin_dir)}, clear=False):
            candidate = product_executables.discover_candidate("claude-code")
            self.assertEqual(candidate.source, "path_candidate")
            with self.assertRaises(product_approvals.ProductApprovalError) as context:
                product_approvals.approve_candidate(
                    vendor="claude-code",
                    product_bin="claude",
                    expected_fingerprint_sha256=candidate.fingerprint_sha256,
                    operator_reference="direct test operator confirmation",
                )
        self.assertEqual(
            context.exception.code, "product_approval.absolute_path_required"
        )
        self.assertFalse(self.marker.exists())

    def test_supplied_tilde_relative_and_terminal_control_paths_are_rejected(
        self,
    ) -> None:
        candidate = self.discover()
        with mock.patch.dict(
            os.environ,
            {"HOME": str(self.home), "PATH": str(self.bin_dir)},
            clear=False,
        ):
            for supplied in (
                "~/bin/claude",
                "bin/claude",
                f"{self.executable}\x1b[2J",
                f"{self.executable}\u202erorrim",
            ):
                with (
                    self.subTest(supplied=repr(supplied)),
                    self.assertRaises(product_approvals.ProductApprovalError),
                ):
                    product_executables.discover_candidate(
                        "claude-code",
                        supplied,
                        allow_path_lookup=True,
                    )
                with (
                    self.subTest(approval=repr(supplied)),
                    self.assertRaises(product_approvals.ProductApprovalError),
                ):
                    product_approvals.approve_candidate(
                        vendor="claude-code",
                        product_bin=supplied,
                        expected_fingerprint_sha256=candidate.fingerprint_sha256,
                        operator_reference="direct test operator confirmation",
                    )
        self.assertFalse(self.marker.exists())

    def test_fifo_is_rejected_before_open_for_hash_and_metadata_checks(self) -> None:
        fifo = self.bin_dir / "claude-fifo"
        os.mkfifo(fifo, mode=0o700)
        real_open = os.open

        def guarded_open(path: object, *args: object, **kwargs: object) -> int:
            if path == fifo.name and kwargs.get("dir_fd") is not None:
                raise AssertionError("FIFO must be rejected before open")
            return real_open(path, *args, **kwargs)

        with mock.patch.object(
            product_executables.os,
            "open",
            side_effect=guarded_open,
        ):
            operations = (
                lambda: product_executables.discover_candidate(
                    "claude-code", str(fifo), allow_path_lookup=False
                ),
                lambda: product_executables._metadata_opened(fifo),
            )
            for operation in operations:
                with (
                    self.subTest(operation=operation),
                    self.assertRaises(product_approvals.ProductApprovalError) as error,
                ):
                    operation()
                self.assertEqual(error.exception.code, "product_approval.file_type")

    def test_candidate_change_requires_a_fresh_card(self) -> None:
        candidate = self.discover()
        write_native(self.executable, "replacement 7")
        self.executable.chmod(0o700)
        with self.assertRaises(product_approvals.ProductApprovalError) as context:
            product_approvals.approve_candidate(
                vendor="claude-code",
                product_bin=str(self.executable),
                expected_fingerprint_sha256=candidate.fingerprint_sha256,
                operator_reference="direct test operator confirmation",
            )
        self.assertEqual(context.exception.code, "product_approval.candidate_changed")
        self.assertEqual(product_approvals.approval_status()["active"], [])

    def test_fingerprint_drift_and_symlink_retarget_fail_closed(self) -> None:
        self.approve()
        write_native(self.executable, "replacement 9")
        self.executable.chmod(0o700)
        with self.assertRaises(product_approvals.ProductApprovalError) as drift:
            product_approvals.require_approved_executable(
                vendor="claude-code", product_bin=str(self.executable)
            )
        self.assertEqual(drift.exception.code, "product_approval.drift")

        approved_target = self.bin_dir / "approved-target"
        write_native(approved_target)
        approved_target.chmod(0o700)
        replacement = self.bin_dir / "replacement"
        write_native(replacement, "replacement 1")
        replacement.chmod(0o700)
        link = self.bin_dir / "current-claude"
        link.symlink_to(approved_target)
        fresh = product_executables.discover_candidate(
            "claude-code", str(link), allow_path_lookup=False
        )
        product_approvals.approve_candidate(
            vendor="claude-code",
            product_bin=str(link),
            expected_fingerprint_sha256=fresh.fingerprint_sha256,
            operator_reference="direct approval after changed-file review",
        )
        link.unlink()
        link.symlink_to(replacement)
        with self.assertRaises(product_approvals.ProductApprovalError) as retargeted:
            product_approvals.require_approved_executable(
                vendor="claude-code", product_bin=str(link)
            )
        self.assertEqual(retargeted.exception.code, "product_approval.required")

        first_directory = self.bin_dir / "first"
        second_directory = self.bin_dir / "second"
        first_directory.mkdir()
        second_directory.mkdir()
        for directory, exit_code in (
            (first_directory, 0),
            (second_directory, 1),
        ):
            executable = directory / "claude"
            write_native(executable, str(exit_code))
            executable.chmod(0o700)
        directory_link = self.bin_dir / "selected"
        directory_link.symlink_to(first_directory, target_is_directory=True)
        ancestor_candidate = product_executables.discover_candidate(
            "claude-code",
            str(directory_link / "claude"),
            allow_path_lookup=False,
        )
        product_approvals.approve_candidate(
            vendor="claude-code",
            product_bin=ancestor_candidate.canonical_path,
            expected_fingerprint_sha256=ancestor_candidate.fingerprint_sha256,
            operator_reference="direct approval of resolved ancestor target",
        )
        directory_link.unlink()
        directory_link.symlink_to(second_directory, target_is_directory=True)
        with self.assertRaises(product_approvals.ProductApprovalError) as ancestor_swap:
            product_approvals.require_approved_executable(
                vendor="claude-code",
                product_bin=str(directory_link / "claude"),
            )
        self.assertEqual(ancestor_swap.exception.code, "product_approval.required")

    def test_revoke_is_guarded_and_append_only(self) -> None:
        result = self.approve()
        approval = result["approval"]
        revoked = product_approvals.revoke_approval(
            vendor="claude-code",
            product_bin=str(self.executable),
            approval_record_id=approval["record_id"],
            expected_fingerprint_sha256=approval["attributes"]["fingerprint_sha256"],
            operator_reference="direct test revocation",
        )
        self.assertEqual(revoked["status"], "revoked")
        status = product_approvals.approval_status()
        self.assertEqual(status["record_count"], 2)
        self.assertEqual(status["active"], [])
        with self.assertRaises(product_approvals.ProductApprovalError) as context:
            product_approvals.require_approved_executable(
                vendor="claude-code", product_bin=str(self.executable)
            )
        self.assertEqual(context.exception.code, "product_approval.required")

    def test_cached_metadata_recheck_replays_only_after_registry_change(self) -> None:
        candidate = product_executables.discover_candidate(
            "claude-code", str(self.executable), allow_path_lookup=False
        )
        approved = product_approvals.approve_candidate(
            vendor="claude-code",
            product_bin=str(self.executable),
            expected_fingerprint_sha256=candidate.fingerprint_sha256,
            operator_reference="direct test operator confirmation",
        )
        product_approvals.require_approved_executable(
            vendor="claude-code",
            product_bin=str(self.executable),
        )
        with mock.patch.object(
            product_approvals,
            "_verify",
            wraps=product_approvals._verify,
        ) as verify:
            product_approvals.require_approved_metadata(
                vendor="claude-code",
                product_bin=str(self.executable),
            )
            self.assertEqual(verify.call_count, 0)
            approval = approved["approval"]
            product_approvals.revoke_approval(
                vendor="claude-code",
                product_bin=str(self.executable),
                approval_record_id=approval["record_id"],
                expected_fingerprint_sha256=approval["attributes"][
                    "fingerprint_sha256"
                ],
                operator_reference="direct test revocation after cache",
            )
            with self.assertRaises(product_approvals.ProductApprovalError) as error:
                product_approvals.require_approved_metadata(
                    vendor="claude-code",
                    product_bin=str(self.executable),
                )
        self.assertEqual(error.exception.code, "product_approval.required")
        self.assertEqual(verify.call_count, 2)

    def test_metadata_recheck_requires_an_operation_local_full_attestation(
        self,
    ) -> None:
        self.approve()
        with product_approvals._VERIFIED_APPROVALS_LOCK:
            product_approvals._VERIFIED_APPROVALS.clear()
        with self.assertRaises(product_approvals.ProductApprovalError) as missing:
            product_approvals.require_approved_metadata(
                vendor="claude-code",
                product_bin=str(self.executable),
            )
        self.assertEqual(
            missing.exception.code,
            "product_approval.attestation_missing",
        )
        product_approvals.require_approved_executable(
            vendor="claude-code",
            product_bin=str(self.executable),
        )
        product_approvals.require_approved_metadata(
            vendor="claude-code",
            product_bin=str(self.executable),
        )

    def test_concurrent_identical_approval_appends_once(self) -> None:
        candidate = self.discover()

        def approve() -> str:
            return product_approvals.approve_candidate(
                vendor="claude-code",
                product_bin=str(self.executable),
                expected_fingerprint_sha256=candidate.fingerprint_sha256,
                operator_reference="same direct concurrent confirmation",
            )["status"]

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            statuses = list(executor.map(lambda _index: approve(), range(16)))
        self.assertEqual(statuses.count("approved"), 1)
        self.assertEqual(statuses.count("already_approved"), 15)
        self.assertEqual(product_approvals.approval_status()["record_count"], 1)

    def test_cross_process_identical_approval_appends_once(self) -> None:
        candidate = self.discover()
        gate = self.home / "start-approval-processes"
        command = [
            sys.executable,
            str(APPROVAL_PROCESS_HELPER),
            str(self.home),
            str(gate),
            "claude-code",
            str(self.executable),
            candidate.fingerprint_sha256,
        ]
        processes = [
            subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(8)
        ]
        gate.touch(mode=0o600)
        outputs: list[dict[str, str]] = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=20)
            self.assertEqual(process.returncode, 0, stderr)
            outputs.append(json.loads(stdout))

        statuses = [output["status"] for output in outputs]
        self.assertEqual(statuses.count("approved"), 1)
        self.assertEqual(statuses.count("already_approved"), 7)
        self.assertEqual(product_approvals.approval_status()["record_count"], 1)

    def test_registry_rejects_insecure_mode_and_tampered_chain(self) -> None:
        self.approve()
        path = self.home / "CAM" / "Approvals" / product_approvals.REGISTRY_NAME
        path.chmod(0o644)
        with self.assertRaises(product_approvals.ProductApprovalError) as insecure:
            product_approvals.approval_status()
        self.assertEqual(insecure.exception.code, "approval.registry.mode")

        path.chmod(0o600)
        raw = bytearray(path.read_bytes())
        offset = raw.index(b"direct test")
        raw[offset] = ord("D")
        path.write_bytes(raw)
        path.chmod(0o600)
        with self.assertRaises(product_approvals.ProductApprovalError):
            product_approvals.approval_status()

    def test_registry_replay_enforces_grandfather_migration_pairing(self) -> None:
        self.approve()
        path = self.home / "CAM" / "Approvals" / product_approvals.REGISTRY_NAME
        record = json.loads(path.read_text(encoding="utf-8"))
        record["attributes"]["basis"] = "grandfathered_roster"
        unsigned = dict(record)
        unsigned.pop("record_sha256")
        record["record_sha256"] = product_approvals._digest(unsigned)
        path.write_bytes(product_approvals._canonical_json(record) + b"\n")
        path.chmod(0o600)
        with self.assertRaises(product_approvals.ProductApprovalError) as error:
            product_approvals.approval_status()
        self.assertEqual(error.exception.code, "product_approval.migration")

    def test_registry_replay_rejects_unsafe_paths_and_noncanonical_metadata(
        self,
    ) -> None:
        self.approve()
        path = self.home / "CAM" / "Approvals" / product_approvals.REGISTRY_NAME
        original = json.loads(path.read_text(encoding="utf-8"))

        def write_record(record: dict[str, object]) -> None:
            unsigned = dict(record)
            unsigned.pop("record_sha256")
            record["record_sha256"] = product_approvals._digest(unsigned)
            path.write_bytes(product_approvals._canonical_json(record) + b"\n")
            path.chmod(0o600)

        for unsafe_path in ("/tmp/claude\x1b[2J", "/tmp/claude\u202eevil"):
            record = json.loads(json.dumps(original))
            attributes = record["attributes"]
            attributes["canonical_path"] = unsafe_path
            fingerprint = product_approvals.ExecutableFingerprint(
                **attributes["fingerprint"]
            )
            attributes["fingerprint_sha256"] = product_approvals._candidate_digest(
                attributes["vendor"],
                unsafe_path,
                fingerprint,
            )
            write_record(record)
            with (
                self.subTest(unsafe_path=repr(unsafe_path)),
                self.assertRaises(product_approvals.ProductApprovalError),
            ):
                product_approvals.approval_status()

        unsafe_reference = json.loads(json.dumps(original))
        unsafe_reference["attributes"]["operator_reference"] = (
            "operator approved\u202ereversed"
        )
        write_record(unsafe_reference)
        with self.assertRaises(product_approvals.ProductApprovalError):
            product_approvals.approval_status()

        noncanonical_cases = (
            ("record_id", "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"),
            ("recorded_at", original["recorded_at"].replace("Z", "+00:00")),
        )
        for field, value in noncanonical_cases:
            record = json.loads(json.dumps(original))
            record[field] = value
            write_record(record)
            with (
                self.subTest(field=field),
                self.assertRaises(product_approvals.ProductApprovalError),
            ):
                product_approvals.approval_status()

    def test_home_environment_does_not_choose_registry_location(self) -> None:
        hostile = self.home / "hostile-home"
        with mock.patch.dict(os.environ, {"HOME": str(hostile)}, clear=False):
            self.approve()
        expected = self.home / "CAM" / "Approvals" / product_approvals.REGISTRY_NAME
        self.assertTrue(expected.is_file())
        self.assertFalse(hostile.exists())

    def test_status_never_hashes_or_executes_the_product(self) -> None:
        self.approve()
        with mock.patch.object(
            product_executables,
            "_fingerprint_opened",
            side_effect=AssertionError("status must not hash a product"),
        ):
            status = product_approvals.approval_status(
                vendor="claude-code",
                product_bin=str(self.executable),
            )
        self.assertEqual(len(status["active"]), 1)
        self.assertFalse(self.marker.exists())

    def test_status_and_revoke_report_symlink_loops_as_bounded_errors(self) -> None:
        loop = self.bin_dir / "loop"
        loop.symlink_to(loop)
        operations = (
            lambda: product_approvals.approval_status(
                vendor="claude-code",
                product_bin=str(loop),
            ),
            lambda: product_approvals.revoke_approval(
                vendor="claude-code",
                product_bin=str(loop),
                approval_record_id="00000000-0000-4000-8000-000000000001",
                expected_fingerprint_sha256="a" * 64,
                operator_reference="direct test revocation",
            ),
        )
        for operation in operations:
            with (
                self.subTest(operation=operation),
                self.assertRaises(product_approvals.ProductApprovalError) as error,
            ):
                operation()
            self.assertEqual(error.exception.code, "product_approval.path")

    def test_failed_append_retains_partial_record(self) -> None:
        candidate = self.discover()
        original_write = product_approvals._write_all

        def partial_write(descriptor: int, raw: bytes) -> None:
            original_write(descriptor, raw[:17])
            raise project.ProjectError("state.write", "injected partial write")

        with (
            mock.patch.object(product_approvals, "_write_all", partial_write),
            self.assertRaises(product_approvals.ProductApprovalError) as context,
        ):
            product_approvals.approve_candidate(
                vendor="claude-code",
                product_bin=str(self.executable),
                expected_fingerprint_sha256=candidate.fingerprint_sha256,
                operator_reference="direct test operator confirmation",
            )
        self.assertEqual(context.exception.code, "product_approval.write")
        self.assertEqual(context.exception.audit["mutation_state"], "unknown")
        self.assertEqual(product_approvals.registry_path().stat().st_size, 17)
        with self.assertRaises(product_approvals.ProductApprovalError):
            product_approvals.approval_status()

    def test_approve_and_revoke_faults_retain_bytes_and_invalidate_cache(self) -> None:
        for operation in ("approve", "revoke"):
            for fault in (
                "zero",
                "partial",
                "fsync",
                "verification",
                "cache",
                "unlock",
                "close",
                "partial_cleanup",
            ):
                if operation == "revoke" and fault == "cache":
                    continue
                with self.subTest(operation=operation, fault=fault):
                    self._assert_approval_append_fault(operation, fault)

    def _assert_approval_append_fault(self, operation: str, fault: str) -> None:
        original_write = product_approvals._write_all
        original_fsync = os.fsync
        original_fstat = os.fstat
        original_identity = product_approvals._registry_identity
        original_flock = product_approvals.fcntl.flock
        original_close = os.close
        isolated = self.home / f"{operation}-{fault}"
        isolated.mkdir(mode=0o700)
        with mock.patch.object(
            product_approvals, "account_home", return_value=isolated
        ):
            approval = self.approve()["approval"] if operation == "revoke" else None
            registry = isolated / "CAM" / "Approvals" / product_approvals.REGISTRY_NAME
            before = registry.read_bytes() if registry.exists() else b""
            candidate = self.discover()
            written = b""
            append_fd = None
            synced = False
            partial = fault in {"partial", "partial_cleanup"}

            def write_fault(descriptor, raw):
                nonlocal written, append_fd
                append_fd = descriptor
                written = raw
                if fault == "zero":
                    raise OSError("synthetic zero-progress fault")
                original_write(descriptor, raw[:17] if partial else raw)
                if partial:
                    raise project.ProjectError("state.write", "synthetic partial write")

            def sync_fault(descriptor):
                nonlocal synced
                if descriptor == append_fd:
                    if fault == "fsync":
                        raise OSError("synthetic sync fault")
                    synced = True
                return original_fsync(descriptor)

            def stat_fault(descriptor):
                if descriptor == append_fd and synced and fault == "verification":
                    raise OSError("synthetic verification fault")
                return original_fstat(descriptor)

            def identity_fault(descriptor):
                if descriptor == append_fd and fault == "cache":
                    raise OSError("synthetic post-append cache verification fault")
                return original_identity(descriptor)

            def unlock_fault(descriptor, operation):
                original_flock(descriptor, operation)
                if (
                    descriptor == append_fd
                    and operation == product_approvals.fcntl.LOCK_UN
                    and fault in {"unlock", "partial_cleanup"}
                ):
                    raise OSError("synthetic post-append unlock fault")

            def close_fault(descriptor):
                original_close(descriptor)
                if descriptor == append_fd and fault == "close":
                    raise OSError("synthetic post-append close fault")

            with (
                mock.patch.object(
                    product_approvals, "_write_all", side_effect=write_fault
                ),
                mock.patch.object(
                    product_approvals.os, "fsync", side_effect=sync_fault
                ),
                mock.patch.object(
                    product_approvals.os, "fstat", side_effect=stat_fault
                ),
                mock.patch.object(
                    product_approvals, "_registry_identity", side_effect=identity_fault
                ),
                mock.patch.object(
                    product_approvals.fcntl, "flock", side_effect=unlock_fault
                ),
                mock.patch.object(
                    product_approvals.os, "close", side_effect=close_fault
                ),
                mock.patch.object(product_approvals.os, "ftruncate") as truncate,
                self.assertRaises(cam1_transport.TransportError) as context,
            ):
                if approval is None:
                    cam1_transport.approve_product_executable(
                        vendor="claude-code",
                        product_bin=str(self.executable),
                        expected_fingerprint_sha256=candidate.fingerprint_sha256,
                        operator_reference="direct synthetic approval",
                    )
                else:
                    cam1_transport.revoke_product_executable(
                        vendor="claude-code",
                        product_bin=str(self.executable),
                        approval_record_id=approval["record_id"],
                        expected_fingerprint_sha256=candidate.fingerprint_sha256,
                        operator_reference="direct synthetic revocation",
                    )
            truncate.assert_not_called()
            committed = fault in {"cache", "unlock", "close"}
            self.assertEqual(
                context.exception.code,
                "product_approval.committed_uncertain"
                if committed
                else "product_approval.write",
            )
            self.assertEqual(
                context.exception.audit["mutation_state"],
                "committed" if committed else "unknown",
            )
            self.assertEqual(
                context.exception.audit["durability_confirmed"],
                fault == "verification" or committed,
            )
            appended = b"" if fault == "zero" else written[:17] if partial else written
            self.assertEqual(registry.read_bytes(), before + appended)
            self.assertEqual(product_approvals._VERIFIED_APPROVALS, {})
            if partial:
                with self.assertRaises(product_approvals.ProductApprovalError):
                    product_approvals.approval_status()
            else:
                status = product_approvals.approval_status()
                self.assertEqual(
                    status["record_count"],
                    int(approval is not None) + int(fault != "zero"),
                )


if __name__ == "__main__":
    unittest.main()
