# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Descriptor-safe inspection and mutation for approval-ledger recovery.

Immutable evidence publication is isolated in
:mod:`cam1lib.product_approval_recovery_evidence`. The approval record codec,
chain verification, and active-state projection remain in
:mod:`cam1lib.product_approvals`.
"""

from __future__ import annotations

import datetime as dt
import errno
import fcntl
import hashlib
import io
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from . import product_approval_recovery_evidence as _evidence
from .errors import ProjectError
from .product_executables import (
    ProductApprovalError,
    _bounded_text,
    _sha256,
)
from .secure_fs import (
    _open_private_directory,
    _validate_private_file,
    _validate_private_file_metadata,
)

_COPY_CHUNK_BYTES = 1_048_576
MAX_RECOVERY_REASON_LENGTH = _evidence.MAX_RECOVERY_REASON_LENGTH
parse_recovery_manifest = _evidence.parse_recovery_manifest

VerifyPrefix = Callable[[BinaryIO], tuple[list[dict[str, Any]], int]]


class RecoveryMutationError(ProductApprovalError):
    """A recovery error with an explicit primary-ledger mutation state."""

    def __init__(self, code: str, detail: str, *, audit: dict[str, Any]):
        super().__init__(code, detail)
        self.audit = audit


def _file_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_ctime_ns,
        metadata.st_mtime_ns,
    )


def call_with_stable_errors(
    operation: Callable[..., dict[str, Any]], **kwargs: Any
) -> dict[str, Any]:
    """Map low-level recovery I/O failures onto the public error contract."""

    try:
        return operation(**kwargs)
    except ProductApprovalError:
        raise
    except (OSError, ProjectError) as error:
        raise ProductApprovalError(
            "product_approval.recovery_io",
            "approval registry recovery operation failed safely",
        ) from error


@dataclass(frozen=True, slots=True)
class RecoveryLedgerApi:
    """Ledger-policy callbacks supplied without introducing an import cycle."""

    max_registry_bytes: int
    max_record_bytes: int
    open_registry: Callable[..., tuple[Path, int, BinaryIO]]
    verify: VerifyPrefix
    active_records: Callable[
        [list[dict[str, Any]]], dict[tuple[str, str], dict[str, Any]]
    ]
    operator_reference: Callable[[Any], str]
    begin_operation: Callable[[], None]


@dataclass(frozen=True, slots=True)
class PartialApprovalTailReport:
    """Exact guards for one incomplete EOF fragment after a verified prefix."""

    registry_sha256: str
    registry_bytes: int
    registry_device: int
    registry_inode: int
    registry_ctime_ns: int
    registry_mtime_ns: int
    verified_prefix_sha256: str
    verified_prefix_bytes: int
    verified_prefix_record_count: int
    verified_prefix_last_record_sha256: str | None
    partial_tail_sha256: str
    partial_tail_bytes: int

    def summary(self) -> dict[str, Any]:
        return {
            "registry_sha256": self.registry_sha256,
            "registry_bytes": self.registry_bytes,
            "registry_identity": {
                "device": self.registry_device,
                "inode": self.registry_inode,
                "ctime_ns": self.registry_ctime_ns,
                "mtime_ns": self.registry_mtime_ns,
            },
            "verified_prefix_sha256": self.verified_prefix_sha256,
            "verified_prefix_bytes": self.verified_prefix_bytes,
            "verified_prefix_record_count": self.verified_prefix_record_count,
            "verified_prefix_last_record_sha256": (
                self.verified_prefix_last_record_sha256
            ),
            "partial_tail_sha256": self.partial_tail_sha256,
            "partial_tail_bytes": self.partial_tail_bytes,
            "partial_tail_fragment_count": 1,
        }

    def guard_tuple(self) -> tuple[Any, ...]:
        """Return every field that must remain stable before mutation."""

        return (
            self.registry_sha256,
            self.registry_bytes,
            self.registry_device,
            self.registry_inode,
            self.registry_ctime_ns,
            self.registry_mtime_ns,
            self.verified_prefix_sha256,
            self.verified_prefix_bytes,
            self.verified_prefix_record_count,
            self.verified_prefix_last_record_sha256,
            self.partial_tail_sha256,
            self.partial_tail_bytes,
        )


def inspect_recovery_evidence(
    approvals_directory: Path,
    *,
    report: PartialApprovalTailReport,
) -> dict[str, Any]:
    return _evidence.inspect_recovery_evidence(
        approvals_directory,
        report=report,
    )


def acquire_registry_lock(
    descriptor: int,
    *,
    exclusive: bool,
    timeout_seconds: float,
    poll_seconds: float,
) -> None:
    """Acquire a shared or exclusive registry lock within a monotonic bound."""

    operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            fcntl.flock(descriptor, operation | fcntl.LOCK_NB)
            return
        except OSError as error:
            if error.errno not in {errno.EACCES, errno.EAGAIN}:
                raise ProductApprovalError(
                    "product_approval.lock",
                    "approval registry lock could not be acquired",
                ) from None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProductApprovalError(
                    "product_approval.lock_timeout",
                    "approval registry is busy; retry this operation later",
                ) from None
            time.sleep(min(poll_seconds, remaining))


def _range_bytes(descriptor: int, *, offset: int, length: int) -> bytes:
    chunks: list[bytes] = []
    position = offset
    remaining = length
    while remaining:
        try:
            chunk = os.pread(descriptor, min(_COPY_CHUNK_BYTES, remaining), position)
        except OSError:
            raise ProductApprovalError(
                "product_approval.recovery_read",
                "approval registry could not be read for recovery",
            ) from None
        if not chunk:
            raise ProductApprovalError(
                "product_approval.recovery_changed",
                "approval registry changed during recovery inspection",
            )
        chunks.append(chunk)
        position += len(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def range_digest(descriptor: int, *, offset: int, length: int) -> str:
    digest = hashlib.sha256()
    position = offset
    remaining = length
    while remaining:
        try:
            chunk = os.pread(descriptor, min(_COPY_CHUNK_BYTES, remaining), position)
        except OSError:
            raise ProductApprovalError(
                "product_approval.recovery_read",
                "approval registry could not be read for recovery",
            ) from None
        if not chunk:
            raise ProductApprovalError(
                "product_approval.recovery_changed",
                "approval registry changed during recovery inspection",
            )
        digest.update(chunk)
        position += len(chunk)
        remaining -= len(chunk)
    return digest.hexdigest()


def _last_complete_record_offset(descriptor: int, *, size: int) -> int:
    position = size
    while position:
        start = max(0, position - 65_536)
        try:
            chunk = os.pread(descriptor, position - start, start)
        except OSError:
            raise ProductApprovalError(
                "product_approval.recovery_read",
                "approval registry tail could not be inspected",
            ) from None
        if len(chunk) != position - start:
            raise ProductApprovalError(
                "product_approval.recovery_changed",
                "approval registry changed during recovery inspection",
            )
        newline = chunk.rfind(b"\n")
        if newline >= 0:
            return start + newline + 1
        position = start
    return 0


def inspect_partial_tail_locked(
    descriptor: int,
    *,
    max_registry_bytes: int,
    max_record_bytes: int,
    verify_prefix: VerifyPrefix,
) -> tuple[PartialApprovalTailReport, list[dict[str, Any]]]:
    """Inspect exactly one incomplete EOF fragment under an existing lock."""

    metadata = os.fstat(descriptor)
    size = metadata.st_size
    if size == 0:
        raise ProductApprovalError(
            "product_approval.recovery_not_needed",
            "approval registry is empty and has no partial tail",
        )
    if size > max_registry_bytes:
        raise ProductApprovalError(
            "product_approval.registry_limit",
            "approval registry exceeds its bounded limits",
        )
    try:
        final_byte = os.pread(descriptor, 1, size - 1)
    except OSError:
        raise ProductApprovalError(
            "product_approval.recovery_read",
            "approval registry tail could not be inspected",
        ) from None
    if final_byte == b"\n":
        raise ProductApprovalError(
            "product_approval.recovery_not_needed",
            "approval registry has no incomplete EOF fragment",
        )

    prefix_bytes = _last_complete_record_offset(descriptor, size=size)
    tail_bytes = size - prefix_bytes
    if tail_bytes > max_record_bytes:
        raise ProductApprovalError(
            "product_approval.recovery_tail_limit",
            "incomplete approval-registry tail exceeds the record limit",
        )
    prefix_raw = _range_bytes(descriptor, offset=0, length=prefix_bytes)
    records, verified_bytes = verify_prefix(io.BytesIO(prefix_raw))
    if verified_bytes != prefix_bytes:
        raise ProductApprovalError(
            "product_approval.recovery_prefix",
            "approval-registry prefix verification did not consume the exact prefix",
        )
    report = PartialApprovalTailReport(
        registry_sha256=range_digest(descriptor, offset=0, length=size),
        registry_bytes=size,
        registry_device=metadata.st_dev,
        registry_inode=metadata.st_ino,
        registry_ctime_ns=metadata.st_ctime_ns,
        registry_mtime_ns=metadata.st_mtime_ns,
        verified_prefix_sha256=hashlib.sha256(prefix_raw).hexdigest(),
        verified_prefix_bytes=prefix_bytes,
        verified_prefix_record_count=len(records),
        verified_prefix_last_record_sha256=(
            records[-1]["record_sha256"] if records else None
        ),
        partial_tail_sha256=range_digest(
            descriptor, offset=prefix_bytes, length=tail_bytes
        ),
        partial_tail_bytes=tail_bytes,
    )
    if _file_identity(os.fstat(descriptor)) != _file_identity(metadata):
        raise ProductApprovalError(
            "product_approval.recovery_changed",
            "approval registry changed during recovery inspection",
        )
    return report, records


def _bounded_nonnegative_integer(value: Any, *, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ProductApprovalError(
            "product_approval.recovery_guard",
            f"{label} must be a nonnegative integer",
        )
    return value


def _active_signature(
    active: dict[tuple[str, str], dict[str, Any]],
) -> tuple[tuple[str, str, str, str, str], ...]:
    return tuple(
        sorted(
            (
                vendor,
                path,
                record["record_id"],
                record["record_sha256"],
                record["attributes"]["fingerprint_sha256"],
            )
            for (vendor, path), record in active.items()
        )
    )


def _mutation_audit(
    *,
    mutation_state: str,
    registry: Path,
    report: PartialApprovalTailReport,
    archive_path: str,
    manifest_path: str,
    manifest: dict[str, Any] | None,
) -> dict[str, Any]:
    result = {
        "mutation_state": mutation_state,
        "registry": str(registry),
        "registry_identity": {
            "device": report.registry_device,
            "inode": report.registry_inode,
        },
        "expected_repaired_registry": {
            "sha256": report.verified_prefix_sha256,
            "bytes": report.verified_prefix_bytes,
            "record_count": report.verified_prefix_record_count,
            "last_record_sha256": report.verified_prefix_last_record_sha256,
        },
        "archive_path": archive_path,
        "manifest_path": manifest_path,
        "reconciliation_arguments": ["product-recovery-status"],
    }
    if manifest is not None:
        result["recovery_manifest"] = manifest
    return result


def _recovery_report_locked(
    descriptor: int,
    *,
    api: RecoveryLedgerApi,
) -> tuple[PartialApprovalTailReport, list[dict[str, Any]]]:
    report, records = inspect_partial_tail_locked(
        descriptor,
        max_registry_bytes=api.max_registry_bytes,
        max_record_bytes=api.max_record_bytes,
        verify_prefix=api.verify,
    )
    api.active_records(records)
    return report, records


def _recovery_arguments(report: PartialApprovalTailReport) -> list[str]:
    return [
        "product-recover-partial-tail",
        "--expected-registry-sha256",
        report.registry_sha256,
        "--expected-registry-bytes",
        str(report.registry_bytes),
        "--expected-registry-device",
        str(report.registry_device),
        "--expected-registry-inode",
        str(report.registry_inode),
        "--expected-registry-ctime-ns",
        str(report.registry_ctime_ns),
        "--expected-registry-mtime-ns",
        str(report.registry_mtime_ns),
        "--expected-prefix-sha256",
        report.verified_prefix_sha256,
        "--expected-prefix-bytes",
        str(report.verified_prefix_bytes),
        "--expected-prefix-record-count",
        str(report.verified_prefix_record_count),
        "--expected-tail-sha256",
        report.partial_tail_sha256,
        "--expected-tail-bytes",
        str(report.partial_tail_bytes),
        "--reason",
        "Describe the observed interrupted approval-ledger append",
        "--operator-reference",
        "DIRECT_OPERATOR_REFERENCE",
    ]


def approval_recovery_status(*, api: RecoveryLedgerApi) -> dict[str, Any]:
    """Inspect the ledger for one recoverable EOF fragment without mutation."""

    try:
        registry, descriptor, handle = api.open_registry(exclusive=False, create=False)
    except ProductApprovalError as error:
        if error.code.endswith("missing"):
            return {
                "ok": True,
                "status": "registry_missing",
                "registry": None,
                "recovery_required": False,
            }
        raise
    try:
        metadata = os.fstat(descriptor)
        if metadata.st_size > api.max_registry_bytes:
            raise ProductApprovalError(
                "product_approval.registry_limit",
                "approval registry exceeds its bounded limits",
            )
        if metadata.st_size == 0:
            evidence = _evidence.inspect_reconciled_recovery_evidence(
                registry.parent,
                current_registry=b"",
                current_record_sha256s=[],
            )
            return {
                "ok": True,
                "status": "recovery_not_needed",
                "registry": str(registry),
                "recovery_required": False,
                "record_count": 0,
                "registry_bytes": 0,
                "registry_sha256": range_digest(descriptor, offset=0, length=0),
                "registry_identity": {
                    "device": metadata.st_dev,
                    "inode": metadata.st_ino,
                    "ctime_ns": metadata.st_ctime_ns,
                    "mtime_ns": metadata.st_mtime_ns,
                },
                "reconciled_recovery_evidence": evidence,
            }
        try:
            final_byte = os.pread(descriptor, 1, metadata.st_size - 1)
        except OSError:
            raise ProductApprovalError(
                "product_approval.recovery_read",
                "approval registry tail could not be inspected",
            ) from None
        if final_byte == b"\n":
            handle.seek(0)
            records, total = api.verify(handle)
            active = api.active_records(records)
            current_registry = _range_bytes(descriptor, offset=0, length=total)
            evidence = _evidence.inspect_reconciled_recovery_evidence(
                registry.parent,
                current_registry=current_registry,
                current_record_sha256s=[record["record_sha256"] for record in records],
            )
            return {
                "ok": True,
                "status": "recovery_not_needed",
                "registry": str(registry),
                "recovery_required": False,
                "record_count": len(records),
                "active_approval_count": len(active),
                "registry_bytes": total,
                "registry_sha256": hashlib.sha256(current_registry).hexdigest(),
                "registry_identity": {
                    "device": metadata.st_dev,
                    "inode": metadata.st_ino,
                    "ctime_ns": metadata.st_ctime_ns,
                    "mtime_ns": metadata.st_mtime_ns,
                },
                "reconciled_recovery_evidence": evidence,
            }
        report, _records = _recovery_report_locked(descriptor, api=api)
        evidence = inspect_recovery_evidence(registry.parent, report=report)
        evidence["stale_pending_artifacts"] = _evidence.inspect_stale_pending_artifacts(
            registry.parent
        )
        return {
            "ok": True,
            "status": "recoverable_partial_tail",
            "registry": str(registry),
            "recovery_required": True,
            "recovery": report.summary(),
            "existing_recovery_evidence": evidence,
            "recovery_arguments": _recovery_arguments(report),
            "next_step": (
                "Review the exact guards and obtain direct operator confirmation. "
                "Then run product-recover-partial-tail with these unchanged guards "
                "after replacing DIRECT_OPERATOR_REFERENCE and the reason text."
            ),
        }
    finally:
        handle.close()
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _revalidate_registry_path(
    registry: Path,
    descriptor: int,
    report: PartialApprovalTailReport,
) -> None:
    directory_descriptor = _open_private_directory(
        registry.parent, label="approval.directory"
    )
    try:
        try:
            path_metadata = os.stat(
                registry.name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
        except OSError:
            raise ProductApprovalError(
                "product_approval.recovery_changed",
                "approval registry path changed before recovery",
            ) from None
        _validate_private_file_metadata(path_metadata, label="approval.registry")
        opened_metadata = _validate_private_file(descriptor, label="approval.registry")
        expected = (
            report.registry_device,
            report.registry_inode,
            report.registry_bytes,
            report.registry_ctime_ns,
            report.registry_mtime_ns,
        )
        path_identity = _file_identity(path_metadata)
        opened_identity = _file_identity(opened_metadata)
        if path_identity != expected or opened_identity != expected:
            raise ProductApprovalError(
                "product_approval.recovery_changed",
                "approval registry identity changed before recovery",
            )
    finally:
        os.close(directory_descriptor)


def _revalidate_repaired_registry_path(
    registry: Path,
    descriptor: int,
    report: PartialApprovalTailReport,
) -> None:
    """Require the repaired locked inode to remain the live registry path."""

    directory_descriptor = _open_private_directory(
        registry.parent, label="approval.directory"
    )
    try:
        try:
            path_metadata = os.stat(
                registry.name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
        except OSError:
            raise ProductApprovalError(
                "product_approval.recovery_changed",
                "approval registry path changed after recovery",
            ) from None
        _validate_private_file_metadata(path_metadata, label="approval.registry")
        opened_metadata = _validate_private_file(descriptor, label="approval.registry")
        expected_identity = (report.registry_device, report.registry_inode)
        if (
            (path_metadata.st_dev, path_metadata.st_ino) != expected_identity
            or (opened_metadata.st_dev, opened_metadata.st_ino) != expected_identity
            or path_metadata.st_size != report.verified_prefix_bytes
            or opened_metadata.st_size != report.verified_prefix_bytes
        ):
            raise ProductApprovalError(
                "product_approval.recovery_changed",
                "approval registry path no longer names the repaired locked inode",
            )
    finally:
        os.close(directory_descriptor)


def _cleanup_registry_handles(
    handle: BinaryIO,
    descriptor: int,
) -> list[dict[str, str]]:
    """Attempt every cleanup action without hiding an earlier operation result."""

    failures: list[dict[str, str]] = []
    for operation, cleanup in (
        ("handle_close", handle.close),
        ("registry_unlock", lambda: fcntl.flock(descriptor, fcntl.LOCK_UN)),
        ("descriptor_close", lambda: os.close(descriptor)),
    ):
        try:
            cleanup()
        except OSError as error:
            failures.append(
                {
                    "operation": operation,
                    "error_type": type(error).__name__,
                }
            )
    return failures


def recover_partial_tail(
    *,
    api: RecoveryLedgerApi,
    expected_registry_sha256: str,
    expected_registry_bytes: int,
    expected_registry_device: int,
    expected_registry_inode: int,
    expected_registry_ctime_ns: int,
    expected_registry_mtime_ns: int,
    expected_prefix_sha256: str,
    expected_prefix_bytes: int,
    expected_prefix_record_count: int,
    expected_tail_sha256: str,
    expected_tail_bytes: int,
    reason: str,
    operator_reference: str,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Archive and remove one operator-confirmed incomplete EOF fragment."""

    expected_full_digest = _sha256(
        expected_registry_sha256, label="expected_registry_sha256"
    )
    expected_prefix_digest = _sha256(
        expected_prefix_sha256, label="expected_prefix_sha256"
    )
    expected_tail_digest = _sha256(expected_tail_sha256, label="expected_tail_sha256")
    expected_full_bytes = _bounded_nonnegative_integer(
        expected_registry_bytes, label="expected_registry_bytes"
    )
    expected_device = _bounded_nonnegative_integer(
        expected_registry_device, label="expected_registry_device"
    )
    expected_inode = _bounded_nonnegative_integer(
        expected_registry_inode, label="expected_registry_inode"
    )
    expected_ctime_ns = _bounded_nonnegative_integer(
        expected_registry_ctime_ns, label="expected_registry_ctime_ns"
    )
    expected_mtime_ns = _bounded_nonnegative_integer(
        expected_registry_mtime_ns, label="expected_registry_mtime_ns"
    )
    expected_prefix_length = _bounded_nonnegative_integer(
        expected_prefix_bytes, label="expected_prefix_bytes"
    )
    expected_record_count = _bounded_nonnegative_integer(
        expected_prefix_record_count, label="expected_prefix_record_count"
    )
    expected_tail_length = _bounded_nonnegative_integer(
        expected_tail_bytes, label="expected_tail_bytes"
    )
    normalized_reason = _bounded_text(
        reason,
        label="reason",
        maximum=MAX_RECOVERY_REASON_LENGTH,
    )
    normalized_reference = api.operator_reference(operator_reference)

    registry, descriptor, handle = api.open_registry(exclusive=True, create=False)
    mutation_state = "not_attempted"
    report: PartialApprovalTailReport | None = None
    archive_path: str | None = None
    manifest_path: str | None = None
    recovery_manifest: dict[str, Any] | None = None
    operation_result: dict[str, Any] | None = None
    try:
        report, records = _recovery_report_locked(descriptor, api=api)
        supplied_guards = (
            expected_full_digest,
            expected_full_bytes,
            expected_device,
            expected_inode,
            expected_ctime_ns,
            expected_mtime_ns,
            expected_prefix_digest,
            expected_prefix_length,
            expected_record_count,
            expected_tail_digest,
            expected_tail_length,
        )
        observed_guards = (
            report.registry_sha256,
            report.registry_bytes,
            report.registry_device,
            report.registry_inode,
            report.registry_ctime_ns,
            report.registry_mtime_ns,
            report.verified_prefix_sha256,
            report.verified_prefix_bytes,
            report.verified_prefix_record_count,
            report.partial_tail_sha256,
            report.partial_tail_bytes,
        )
        if supplied_guards != observed_guards:
            raise ProductApprovalError(
                "product_approval.recovery_guard_mismatch",
                "approval registry changed or does not match the inspected recovery guards",
            )
        active_before = api.active_records(records)
        _evidence.require_recovery_capacity(registry.parent, report=report)
        archive_file, archive_path = _evidence.create_recovery_archive(
            registry.parent,
            source_descriptor=descriptor,
            report=report,
        )
        expected_manifest_path = str(
            registry.parent
            / (
                f"{_evidence.MANIFEST_PREFIX}{_evidence.recovery_id(report)}"
                f"{_evidence.MANIFEST_SUFFIX}"
            )
        )
        try:
            recovery_manifest, manifest_path = _evidence.create_recovery_manifest(
                registry.parent,
                archive_file=archive_file,
                report=report,
                reason=normalized_reason,
                operator_reference=normalized_reference,
                now=now,
            )
        except ProductApprovalError as error:
            raise RecoveryMutationError(
                error.code,
                error.detail,
                audit=_mutation_audit(
                    mutation_state="not_attempted",
                    registry=registry,
                    report=report,
                    archive_path=archive_path,
                    manifest_path=expected_manifest_path,
                    manifest=None,
                ),
            ) from error

        try:
            current_report, current_records = _recovery_report_locked(
                descriptor, api=api
            )
            _revalidate_registry_path(registry, descriptor, current_report)
            if (
                current_report.guard_tuple() != report.guard_tuple()
                or _active_signature(api.active_records(current_records))
                != _active_signature(active_before)
            ):
                raise ProductApprovalError(
                    "product_approval.recovery_changed",
                    "approval registry changed after recovery inspection",
                )
            current_evidence = _evidence.inspect_recovery_evidence(
                registry.parent,
                report=current_report,
            )
            if (
                current_evidence["status"] != "prepared"
                or current_evidence["archive_path"] != archive_path
                or current_evidence["manifest_path"] != manifest_path
                or current_evidence["manifest"] != recovery_manifest
            ):
                raise ProductApprovalError(
                    "product_approval.recovery_evidence_changed",
                    "approval recovery evidence changed before ledger mutation",
                )
            repaired_registry = _range_bytes(
                descriptor,
                offset=0,
                length=current_report.verified_prefix_bytes,
            )
            _evidence.inspect_reconciled_recovery_evidence(
                registry.parent,
                current_registry=repaired_registry,
                current_record_sha256s=[
                    record["record_sha256"] for record in current_records
                ],
            )
            _evidence.sync_recovery_directory(registry.parent)
        except ProductApprovalError as error:
            raise RecoveryMutationError(
                error.code,
                error.detail,
                audit=_mutation_audit(
                    mutation_state="not_attempted",
                    registry=registry,
                    report=report,
                    archive_path=archive_path,
                    manifest_path=manifest_path,
                    manifest=recovery_manifest,
                ),
            ) from error

        mutation_state = "unknown"
        try:
            os.ftruncate(descriptor, report.verified_prefix_bytes)
        except OSError as error:
            raise RecoveryMutationError(
                "product_approval.recovery_truncate",
                "approval registry partial-tail removal was not completed",
                audit=_mutation_audit(
                    mutation_state="unknown",
                    registry=registry,
                    report=report,
                    archive_path=archive_path,
                    manifest_path=manifest_path,
                    manifest=recovery_manifest,
                ),
            ) from error
        try:
            os.fsync(descriptor)
        except OSError as error:
            raise RecoveryMutationError(
                "product_approval.recovery_commit_uncertain",
                "approval registry reached the verified prefix but fsync failed; "
                "durability is uncertain and the old guards must not be reused",
                audit=_mutation_audit(
                    mutation_state="unknown",
                    registry=registry,
                    report=report,
                    archive_path=archive_path,
                    manifest_path=manifest_path,
                    manifest=recovery_manifest,
                ),
            ) from error
        mutation_state = "committed"

        try:
            handle.seek(0)
            final_records, final_bytes = api.verify(handle)
            active_after = api.active_records(final_records)
            if (
                final_bytes != report.verified_prefix_bytes
                or range_digest(
                    descriptor,
                    offset=0,
                    length=report.verified_prefix_bytes,
                )
                != report.verified_prefix_sha256
                or _active_signature(active_after) != _active_signature(active_before)
            ):
                raise ProductApprovalError(
                    "product_approval.recovery_verify",
                    "recovered approval registry did not preserve active approvals",
                )
            _revalidate_repaired_registry_path(registry, descriptor, report)
        except (OSError, ProjectError, ProductApprovalError) as error:
            api.begin_operation()
            operation_result = {
                "ok": False,
                "status": "recovery_committed_verification_uncertain",
                **_mutation_audit(
                    mutation_state="committed",
                    registry=registry,
                    report=report,
                    archive_path=archive_path,
                    manifest_path=manifest_path,
                    manifest=recovery_manifest,
                ),
                "verification_error": {
                    "code": getattr(
                        error,
                        "code",
                        "product_approval.recovery_verify_io",
                    ),
                    "detail": getattr(
                        error,
                        "detail",
                        "post-commit approval-ledger verification failed",
                    ),
                },
                "next_step": (
                    "Do not reuse the old recovery guards. Run the returned "
                    "read-only product-recovery-status command and compare the "
                    "primary ledger to expected_repaired_registry and the immutable "
                    "recovery manifest."
                ),
            }
        else:
            api.begin_operation()
            operation_result = {
                "ok": True,
                "status": "recovered_partial_tail",
                **_mutation_audit(
                    mutation_state="committed",
                    registry=registry,
                    report=report,
                    archive_path=archive_path,
                    manifest_path=manifest_path,
                    manifest=recovery_manifest,
                ),
                "original": report.summary(),
                "record_count": len(final_records),
                "active_approval_count": len(active_after),
                "active_approvals_unchanged": True,
            }
    except BaseException as active_error:
        cleanup_failures = _cleanup_registry_handles(handle, descriptor)
        if cleanup_failures:
            if isinstance(active_error, RecoveryMutationError):
                active_error.audit["cleanup_errors"] = cleanup_failures
            else:
                active_error.add_note(
                    "approval recovery cleanup also failed; run "
                    "product-recovery-status before retrying"
                )
        raise

    cleanup_failures = _cleanup_registry_handles(handle, descriptor)
    if cleanup_failures:
        if (
            mutation_state in {"committed", "unknown"}
            and report is not None
            and archive_path is not None
            and manifest_path is not None
        ):
            api.begin_operation()
            audit = _mutation_audit(
                mutation_state=mutation_state,
                registry=registry,
                report=report,
                archive_path=archive_path,
                manifest_path=manifest_path,
                manifest=recovery_manifest,
            )
            audit["cleanup_errors"] = cleanup_failures
            if mutation_state == "committed":
                return {
                    "ok": False,
                    "status": "recovery_committed_cleanup_uncertain",
                    **audit,
                    "prior_status": (
                        operation_result.get("status")
                        if operation_result is not None
                        else None
                    ),
                    "next_step": (
                        "The ledger repair committed, but process cleanup did not "
                        "complete normally. Do not reuse the old guards; run the "
                        "returned read-only product-recovery-status command."
                    ),
                }
            raise RecoveryMutationError(
                "product_approval.recovery_cleanup_uncertain",
                "approval recovery mutation or cleanup may be incomplete",
                audit=audit,
            )
        raise ProductApprovalError(
            "product_approval.recovery_cleanup",
            "approval recovery handle cleanup failed before mutation",
        )
    if operation_result is None:
        raise ProductApprovalError(
            "product_approval.recovery_internal",
            "approval recovery produced no operation result",
        )
    return operation_result
