# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import errno
import json
import unittest
import uuid
from pathlib import Path
from unittest import mock

from tools.cam1lib import product_approval_recovery, product_approvals

if __package__:
    from ._product_approval_recovery_test_case import (
        ProductApprovalRecoveryTestCase,
    )
else:
    from _product_approval_recovery_test_case import ProductApprovalRecoveryTestCase


class ProductApprovalRecoveryEvidenceTests(ProductApprovalRecoveryTestCase):
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

        recovery = status["recovery"]
        registry_identity = recovery["registry_identity"]
        for field in ("device", "inode", "ctime_ns", "mtime_ns"):
            self.assertEqual(
                original[f"registry_{field}"],
                registry_identity[field],
            )

        archive_identifier = original["archive_file"][
            len(product_approval_recovery._evidence.ARCHIVE_PREFIX) : -len(
                product_approval_recovery._evidence.ARCHIVE_SUFFIX
            )
        ]
        self.assertEqual(
            archive_identifier,
            str(
                uuid.uuid5(
                    product_approval_recovery._evidence.ARCHIVE_NAMESPACE,
                    original["damaged_registry_sha256"],
                )
            ),
        )
        self.assertNotEqual(original["recovery_id"], archive_identifier)

        different = str(uuid.uuid4())
        with self.assertRaises(product_approvals.ProductApprovalError) as identity:
            product_approval_recovery.parse_recovery_manifest(
                encoded(recovery_id=different)
            )
        self.assertEqual(identity.exception.code, "product_approval.recovery_manifest")

        with self.assertRaises(product_approvals.ProductApprovalError) as archive:
            product_approval_recovery.parse_recovery_manifest(
                encoded(
                    archive_file=(f"product-executables-v1.damaged-{different}.jsonl")
                )
            )
        self.assertEqual(archive.exception.code, "product_approval.recovery_manifest")

        for field in (
            "registry_device",
            "registry_inode",
            "registry_ctime_ns",
            "registry_mtime_ns",
        ):
            with self.subTest(field=field):
                with self.assertRaises(product_approvals.ProductApprovalError) as guard:
                    product_approval_recovery.parse_recovery_manifest(
                        encoded(**{field: original[field] + 1})
                    )
                self.assertEqual(
                    guard.exception.code,
                    "product_approval.recovery_manifest",
                )

    def test_manifest_parser_rejects_integral_json_floats(self) -> None:
        _prefix, _damaged, status = self.damage_registry()
        result = product_approvals.recover_partial_tail(**self.recovery_kwargs(status))
        original = dict(result["recovery_manifest"])

        def encoded(field: str) -> bytes:
            manifest = dict(original)
            manifest[field] = float(manifest[field])
            unsigned = dict(manifest)
            unsigned.pop("record_sha256")
            manifest["record_sha256"] = product_approvals._digest(unsigned)
            return product_approvals._canonical_json(manifest)

        for field in product_approval_recovery._evidence.MANIFEST_INTEGER_FIELDS:
            with self.subTest(field=field):
                with self.assertRaises(
                    product_approvals.ProductApprovalError
                ) as invalid:
                    product_approval_recovery.parse_recovery_manifest(encoded(field))
                self.assertEqual(
                    invalid.exception.code,
                    "product_approval.recovery_manifest",
                )

        manifest_path = Path(result["manifest_path"])
        manifest_path.write_bytes(encoded("verified_prefix_byte_length"))
        manifest_path.chmod(0o600)
        with self.assertRaises(product_approvals.ProductApprovalError) as status_error:
            product_approvals.approval_recovery_status()
        self.assertEqual(
            status_error.exception.code,
            "product_approval.recovery_manifest",
        )

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

    def test_recovery_identity_binds_prefix_bytes_to_record_count(self) -> None:
        _prefix, _damaged, status = self.damage_registry()
        result = product_approvals.recover_partial_tail(**self.recovery_kwargs(status))
        manifest_path = Path(result["manifest_path"])
        manifest = dict(result["recovery_manifest"])
        manifest["verified_prefix_record_count"] = 0
        manifest["verified_prefix_last_record_sha256"] = None
        unsigned = dict(manifest)
        unsigned.pop("record_sha256")
        manifest["record_sha256"] = product_approvals._digest(unsigned)
        manifest_path.write_bytes(product_approvals._canonical_json(manifest))
        manifest_path.chmod(0o600)

        with self.assertRaises(product_approvals.ProductApprovalError) as mismatch:
            product_approvals.approval_recovery_status()

        self.assertEqual(
            mismatch.exception.code,
            "product_approval.recovery_manifest",
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

    def test_recovery_refuses_to_exceed_evidence_reader_capacity(self) -> None:
        _prefix, damaged, status = self.damage_registry()
        arguments = self.recovery_kwargs(status)
        evidence = product_approval_recovery._evidence
        cases = (
            (
                "MAX_RECOVERY_MANIFESTS",
                0,
                "product_approval.recovery_manifest_limit",
            ),
            (
                "MAX_RECOVERY_DIRECTORY_ENTRIES",
                1,
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
                    product_approvals.recover_partial_tail(**arguments)
                self.assertEqual(refused.exception.code, expected_code)
                self.assertEqual(self.registry.read_bytes(), damaged)

        evidence_names = [path.name for path in self.registry.parent.iterdir()]
        self.assertEqual(evidence_names, [product_approvals.REGISTRY_NAME])

    def test_recovery_refuses_preexisting_malformed_manifest(self) -> None:
        _prefix, damaged, status = self.damage_registry()
        malformed = self.registry.parent / (
            f"product-executables-v1.recovery-{uuid.uuid4()}.json"
        )
        malformed.write_bytes(b"{}")
        malformed.chmod(0o600)

        with self.assertRaises(
            product_approval_recovery.RecoveryMutationError
        ) as refused:
            product_approvals.recover_partial_tail(**self.recovery_kwargs(status))

        self.assertEqual(refused.exception.code, "product_approval.recovery_manifest")
        self.assertEqual(refused.exception.audit["mutation_state"], "not_attempted")
        self.assertEqual(self.registry.read_bytes(), damaged)


if __name__ == "__main__":
    unittest.main()
