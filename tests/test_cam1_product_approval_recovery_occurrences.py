# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import datetime as dt
import os
import unittest
from pathlib import Path
from unittest import mock

from tools.cam1lib import product_approval_recovery, product_approvals

if __package__:
    from ._product_approval_recovery_test_case import (
        ProductApprovalRecoveryTestCase,
    )
else:
    from _product_approval_recovery_test_case import ProductApprovalRecoveryTestCase


class ProductApprovalRecoveryOccurrenceTests(ProductApprovalRecoveryTestCase):
    def restore_identical_damage(
        self,
        damaged: bytes,
        *,
        previous_status: dict[str, object],
    ) -> dict[str, object]:
        previous_identity = previous_status["recovery"]["registry_identity"]
        self.registry.write_bytes(damaged)
        self.registry.chmod(0o600)
        metadata = self.registry.stat()
        os.utime(
            self.registry,
            ns=(
                metadata.st_atime_ns,
                max(metadata.st_mtime_ns, previous_identity["mtime_ns"])
                + 1_000_000_000,
            ),
        )
        status = product_approvals.approval_recovery_status()
        self.assertEqual(
            status["recovery"]["registry_sha256"],
            previous_status["recovery"]["registry_sha256"],
        )
        self.assertNotEqual(
            status["recovery"]["registry_identity"],
            previous_identity,
        )
        return status

    def assert_artifact_counts(self, *, archives: int, manifests: int) -> None:
        self.assertEqual(
            len(
                list(
                    self.registry.parent.glob("product-executables-v1.damaged-*.jsonl")
                )
            ),
            archives,
        )
        self.assertEqual(
            len(
                list(
                    self.registry.parent.glob("product-executables-v1.recovery-*.json")
                )
            ),
            manifests,
        )

    def assert_manifest_identity(
        self,
        manifest: dict[str, object],
        status: dict[str, object],
    ) -> None:
        registry_identity = status["recovery"]["registry_identity"]
        for field in ("device", "inode", "ctime_ns", "mtime_ns"):
            self.assertEqual(
                manifest[f"registry_{field}"],
                registry_identity[field],
            )

    def recover_two_occurrences(
        self,
    ) -> tuple[
        bytes,
        bytes,
        dict[str, object],
        dict[str, object],
        dict[str, object],
        dict[str, object],
    ]:
        prefix, damaged, first_status = self.damage_registry()
        first = product_approvals.recover_partial_tail(
            **self.recovery_kwargs(first_status)
        )
        second_status = self.restore_identical_damage(
            damaged,
            previous_status=first_status,
        )
        second = product_approvals.recover_partial_tail(
            **self.recovery_kwargs(second_status)
        )
        return prefix, damaged, first_status, second_status, first, second

    def assert_distinct_occurrences(
        self,
        *,
        damaged: bytes,
        first_status: dict[str, object],
        second_status: dict[str, object],
        first: dict[str, object],
        second: dict[str, object],
    ) -> None:
        first_manifest = first["recovery_manifest"]
        second_manifest = second["recovery_manifest"]
        self.assertEqual(first["archive_path"], second["archive_path"])
        self.assertEqual(Path(first["archive_path"]).read_bytes(), damaged)
        self.assertNotEqual(first["manifest_path"], second["manifest_path"])
        self.assertNotEqual(
            first_manifest["recovery_id"],
            second_manifest["recovery_id"],
        )
        self.assert_manifest_identity(first_manifest, first_status)
        self.assert_manifest_identity(second_manifest, second_status)
        self.assert_artifact_counts(archives=1, manifests=2)

    def test_recurring_identical_damage_creates_distinct_manifest(self) -> None:
        prefix, damaged, first_status, second_status, first, second = (
            self.recover_two_occurrences()
        )
        self.assertEqual(self.registry.read_bytes(), prefix)
        self.assert_distinct_occurrences(
            damaged=damaged,
            first_status=first_status,
            second_status=second_status,
            first=first,
            second=second,
        )
        self.assertEqual(
            product_approvals.approval_recovery_status()[
                "reconciled_recovery_evidence"
            ]["count"],
            2,
        )

    def test_recurring_identical_damage_preserves_changed_context(self) -> None:
        prefix, damaged, first_status = self.damage_registry()
        first = product_approvals.recover_partial_tail(
            **self.recovery_kwargs(first_status)
        )
        second_status = self.restore_identical_damage(
            damaged,
            previous_status=first_status,
        )
        arguments = self.recovery_kwargs(second_status)
        arguments["reason"] = "separately reviewed recurring interrupted append"
        arguments["operator_reference"] = (
            "separate direct test operator recovery confirmation"
        )
        second = product_approvals.recover_partial_tail(**arguments)

        self.assertEqual(self.registry.read_bytes(), prefix)
        self.assert_distinct_occurrences(
            damaged=damaged,
            first_status=first_status,
            second_status=second_status,
            first=first,
            second=second,
        )
        self.assertNotEqual(
            first["recovery_manifest"]["reason"],
            second["recovery_manifest"]["reason"],
        )
        self.assertNotEqual(
            first["recovery_manifest"]["operator_reference"],
            second["recovery_manifest"]["operator_reference"],
        )

    def test_recurring_damage_refuses_changed_shared_archive(self) -> None:
        _prefix, damaged, first_status = self.damage_registry()
        first = product_approvals.recover_partial_tail(
            **self.recovery_kwargs(first_status)
        )
        second_status = self.restore_identical_damage(
            damaged,
            previous_status=first_status,
        )
        self.corrupt_archive(Path(first["archive_path"]), damaged)

        with self.assertRaises(product_approvals.ProductApprovalError) as changed:
            product_approvals.recover_partial_tail(
                **self.recovery_kwargs(second_status)
            )

        self.assertEqual(
            changed.exception.code,
            "product_approval.recovery_archive_changed",
        )
        self.assertEqual(self.registry.read_bytes(), damaged)
        self.assert_artifact_counts(archives=1, manifests=1)

    def test_reconciliation_refuses_changed_shared_archive(self) -> None:
        _prefix, damaged, _first_status, _second_status, first, _second = (
            self.recover_two_occurrences()
        )
        self.corrupt_archive(Path(first["archive_path"]), damaged)

        with self.assertRaises(product_approvals.ProductApprovalError) as changed:
            product_approvals.approval_recovery_status()

        self.assertEqual(
            changed.exception.code,
            "product_approval.recovery_archive_changed",
        )
        self.assert_artifact_counts(archives=1, manifests=2)

    def test_recurring_damage_capacity_counts_only_new_manifest(self) -> None:
        _prefix, damaged, first_status = self.damage_registry()
        product_approvals.recover_partial_tail(**self.recovery_kwargs(first_status))
        second_status = self.restore_identical_damage(
            damaged,
            previous_status=first_status,
        )
        evidence = product_approval_recovery._evidence
        cases = (
            (
                "MAX_RECOVERY_MANIFESTS",
                1,
                "product_approval.recovery_manifest_limit",
            ),
            (
                "MAX_RECOVERY_DIRECTORY_ENTRIES",
                3,
                "product_approval.recovery_evidence_scan_limit",
            ),
        )
        for limit_name, limit, expected_code in cases:
            with self.subTest(limit=limit_name):
                with (
                    mock.patch.object(evidence, limit_name, limit),
                    self.assertRaises(
                        product_approvals.ProductApprovalError
                    ) as refused,
                ):
                    product_approvals.recover_partial_tail(
                        **self.recovery_kwargs(second_status)
                    )
                self.assertEqual(refused.exception.code, expected_code)
                self.assertEqual(self.registry.read_bytes(), damaged)
        self.assert_artifact_counts(archives=1, manifests=1)

    def test_archive_only_failure_reuses_archive_on_retry(self) -> None:
        prefix, damaged, status = self.damage_registry()
        arguments = self.recovery_kwargs(status)
        evidence = product_approval_recovery._evidence
        with (
            mock.patch.object(
                evidence,
                "create_recovery_manifest",
                side_effect=product_approvals.ProductApprovalError(
                    "synthetic.manifest", "synthetic manifest failure"
                ),
            ),
            self.assertRaises(
                product_approval_recovery.RecoveryMutationError
            ) as failed,
        ):
            product_approvals.recover_partial_tail(**arguments)

        self.assertEqual(failed.exception.audit["mutation_state"], "not_attempted")
        self.assertEqual(self.registry.read_bytes(), damaged)
        inspected = product_approvals.approval_recovery_status()
        self.assertEqual(
            inspected["existing_recovery_evidence"]["status"],
            "archive_only",
        )
        archive_path = inspected["existing_recovery_evidence"]["archive_path"]

        result = product_approvals.recover_partial_tail(**arguments)

        self.assertEqual(result["archive_path"], archive_path)
        self.assertEqual(self.registry.read_bytes(), prefix)
        self.assert_artifact_counts(archives=1, manifests=1)

    def test_recurring_damage_retry_reuses_new_manifest(self) -> None:
        prefix, damaged, first_status = self.damage_registry()
        product_approvals.recover_partial_tail(**self.recovery_kwargs(first_status))
        second_status = self.restore_identical_damage(
            damaged,
            previous_status=first_status,
        )
        arguments = self.recovery_kwargs(second_status)
        arguments["now"] = dt.datetime(2026, 9, 3, 12, 0, tzinfo=dt.UTC)
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

        failed_manifest = failed.exception.audit["recovery_manifest"]
        self.assertEqual(self.registry.read_bytes(), damaged)
        self.assert_artifact_counts(archives=1, manifests=2)
        retry_arguments = dict(arguments)
        retry_arguments["now"] = dt.datetime(2026, 9, 3, 12, 5, tzinfo=dt.UTC)
        result = product_approvals.recover_partial_tail(**retry_arguments)

        self.assertEqual(self.registry.read_bytes(), prefix)
        self.assertEqual(
            result["recovery_manifest"]["recovery_id"],
            failed_manifest["recovery_id"],
        )
        self.assertEqual(
            result["recovery_manifest"]["prepared_at"],
            failed_manifest["prepared_at"],
        )
        self.assert_artifact_counts(archives=1, manifests=2)

    @staticmethod
    def corrupt_archive(archive: Path, damaged: bytes) -> None:
        corrupted = bytearray(damaged)
        corrupted[-1] ^= 1
        archive.write_bytes(corrupted)
        archive.chmod(0o600)


if __name__ == "__main__":
    unittest.main()
