# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Descriptor-safe inspection and archiving for approval-ledger recovery.

This module owns bounded byte inspection and archive publication. The approval
record codec, chain verification, active-state projection, and guarded in-place
mutation remain in :mod:`cam1lib.product_approvals`.
"""

from __future__ import annotations

import datetime as dt
import errno
import fcntl
import hashlib
import io
import os
import secrets
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from .errors import ProjectError
from .native_fs import rename_noreplace
from .product_executables import ProductApprovalError, _bounded_text, _sha256
from .secure_fs import (
    PRIVATE_FILE_MODE,
    _open_private_directory,
    _prepare_created_private_file,
    _unlink_matching_entry,
    _validate_private_file,
    _validate_private_file_metadata,
    _write_all,
)

_COPY_CHUNK_BYTES = 1_048_576
_ARCHIVE_PREFIX = "product-executables-v1.damaged-"
_ARCHIVE_SUFFIX = ".jsonl"
_READ_FLAGS = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
_ARCHIVE_CREATE_FLAGS = (
    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
)

VerifyPrefix = Callable[[BinaryIO], tuple[list[dict[str, Any]], int]]


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

    recovery_event: str
    max_registry_bytes: int
    max_record_bytes: int
    max_registry_records: int
    open_registry: Callable[..., tuple[Path, int, BinaryIO]]
    verify: VerifyPrefix
    active_records: Callable[
        [list[dict[str, Any]]], dict[tuple[str, str], dict[str, Any]]
    ]
    build_record: Callable[..., tuple[dict[str, Any], bytes]]
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


def validate_archive_name(value: Any) -> str:
    """Validate the canonical owner-private recovery archive filename."""

    filename = _bounded_text(value, label="archive_file", maximum=255)
    if (
        Path(filename).name != filename
        or not filename.startswith(_ARCHIVE_PREFIX)
        or not filename.endswith(_ARCHIVE_SUFFIX)
    ):
        raise ProductApprovalError(
            "product_approval.recovery_archive",
            "approval recovery archive filename is invalid",
        )
    token = filename[len(_ARCHIVE_PREFIX) : -len(_ARCHIVE_SUFFIX)]
    try:
        canonical_token = str(uuid.UUID(token))
    except (AttributeError, ValueError):
        canonical_token = ""
    if token != canonical_token:
        raise ProductApprovalError(
            "product_approval.recovery_archive",
            "approval recovery archive filename is invalid",
        )
    return filename


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


def _copy_range(
    source_descriptor: int,
    target_descriptor: int,
    *,
    offset: int,
    length: int,
) -> str:
    digest = hashlib.sha256()
    position = offset
    remaining = length
    while remaining:
        try:
            chunk = os.pread(
                source_descriptor, min(_COPY_CHUNK_BYTES, remaining), position
            )
        except OSError:
            raise ProductApprovalError(
                "product_approval.recovery_read",
                "approval registry could not be copied for recovery",
            ) from None
        if not chunk:
            raise ProductApprovalError(
                "product_approval.recovery_changed",
                "approval registry changed while the recovery archive was written",
            )
        _write_all(target_descriptor, chunk)
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
    max_registry_records: int,
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
    if len(records) >= max_registry_records:
        raise ProductApprovalError(
            "product_approval.registry_limit",
            "verified prefix cannot accommodate a recovery record",
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


def create_recovery_archive(
    approvals_directory: Path,
    *,
    source_descriptor: int,
    report: PartialApprovalTailReport,
) -> tuple[str, str]:
    """Publish and verify an exact owner-private copy before source mutation."""

    directory_descriptor = _open_private_directory(
        approvals_directory, label="approval.directory"
    )
    token = str(uuid.uuid4())
    pending_name = f".product-approval-recovery-{token}.pending"
    archive_name = f"{_ARCHIVE_PREFIX}{token}{_ARCHIVE_SUFFIX}"
    archive_descriptor: int | None = None
    pending_exists = False
    pending_identity: tuple[int, int] | None = None
    sealed_exists = False
    succeeded = False
    try:
        try:
            archive_descriptor = os.open(
                pending_name,
                _ARCHIVE_CREATE_FLAGS,
                PRIVATE_FILE_MODE,
                dir_fd=directory_descriptor,
            )
            pending_exists = True
            metadata = os.fstat(archive_descriptor)
            pending_identity = (metadata.st_dev, metadata.st_ino)
        except OSError:
            raise ProductApprovalError(
                "product_approval.recovery_archive_create",
                "approval recovery archive could not be created securely",
            ) from None
        _prepare_created_private_file(
            archive_descriptor, label="approval.recovery_archive"
        )
        copied_digest = _copy_range(
            source_descriptor,
            archive_descriptor,
            offset=0,
            length=report.registry_bytes,
        )
        if not secrets.compare_digest(copied_digest, report.registry_sha256):
            raise ProductApprovalError(
                "product_approval.recovery_changed",
                "approval registry changed while the recovery archive was written",
            )
        os.fsync(archive_descriptor)
        os.close(archive_descriptor)
        archive_descriptor = None
        try:
            rename_noreplace(directory_descriptor, pending_name, archive_name)
        except FileExistsError:
            raise ProductApprovalError(
                "product_approval.recovery_archive_exists",
                "approval recovery archive destination already exists",
            ) from None
        except OSError:
            raise ProductApprovalError(
                "product_approval.recovery_archive_seal",
                "approval recovery archive could not be sealed",
            ) from None
        pending_exists = False
        sealed_exists = True
        try:
            verified_descriptor = os.open(
                archive_name, _READ_FLAGS, dir_fd=directory_descriptor
            )
        except OSError:
            raise ProductApprovalError(
                "product_approval.recovery_archive_seal",
                "sealed approval recovery archive could not be verified",
            ) from None
        try:
            sealed_metadata = _validate_private_file(
                verified_descriptor, label="approval.recovery_archive"
            )
            if (
                pending_identity != (sealed_metadata.st_dev, sealed_metadata.st_ino)
                or sealed_metadata.st_size != report.registry_bytes
            ):
                raise ProductApprovalError(
                    "product_approval.recovery_archive_changed",
                    "sealed approval recovery archive identity changed",
                )
            sealed_digest = range_digest(
                verified_descriptor, offset=0, length=report.registry_bytes
            )
            if not secrets.compare_digest(sealed_digest, report.registry_sha256):
                raise ProductApprovalError(
                    "product_approval.recovery_archive_changed",
                    "sealed approval recovery archive contents changed",
                )
        finally:
            os.close(verified_descriptor)
        os.fsync(directory_descriptor)
        succeeded = True
        return archive_name, str(approvals_directory / archive_name)
    finally:
        if archive_descriptor is not None:
            os.close(archive_descriptor)
        if pending_exists and pending_identity is not None:
            _unlink_matching_entry(directory_descriptor, pending_name, pending_identity)
        if not succeeded and sealed_exists and pending_identity is not None:
            _unlink_matching_entry(directory_descriptor, archive_name, pending_identity)
        os.close(directory_descriptor)


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


def _recovery_report_locked(
    descriptor: int,
    *,
    api: RecoveryLedgerApi,
) -> tuple[PartialApprovalTailReport, list[dict[str, Any]]]:
    report, records = inspect_partial_tail_locked(
        descriptor,
        max_registry_bytes=api.max_registry_bytes,
        max_record_bytes=api.max_record_bytes,
        max_registry_records=api.max_registry_records,
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
            return {
                "ok": True,
                "status": "recovery_not_needed",
                "registry": str(registry),
                "recovery_required": False,
                "record_count": 0,
                "registry_bytes": 0,
                "registry_sha256": range_digest(descriptor, offset=0, length=0),
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
            return {
                "ok": True,
                "status": "recovery_not_needed",
                "registry": str(registry),
                "recovery_required": False,
                "record_count": len(records),
                "active_approval_count": len(active),
                "registry_bytes": total,
                "registry_sha256": range_digest(descriptor, offset=0, length=total),
            }
        report, _records = _recovery_report_locked(descriptor, api=api)
        return {
            "ok": True,
            "status": "recoverable_partial_tail",
            "registry": str(registry),
            "recovery_required": True,
            "recovery": report.summary(),
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
    normalized_reason = _bounded_text(reason, label="reason", maximum=500)
    normalized_reference = api.operator_reference(operator_reference)

    registry, descriptor, handle = api.open_registry(exclusive=True, create=False)
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
        archive_file, archive_path = create_recovery_archive(
            registry.parent,
            source_descriptor=descriptor,
            report=report,
        )
        attributes = {
            "archive_file": archive_file,
            "archive_sha256": report.registry_sha256,
            "archive_byte_length": report.registry_bytes,
            "damaged_registry_sha256": report.registry_sha256,
            "damaged_registry_byte_length": report.registry_bytes,
            "verified_prefix_sha256": report.verified_prefix_sha256,
            "verified_prefix_byte_length": report.verified_prefix_bytes,
            "verified_prefix_record_count": report.verified_prefix_record_count,
            "verified_prefix_last_record_sha256": (
                report.verified_prefix_last_record_sha256
            ),
            "partial_tail_sha256": report.partial_tail_sha256,
            "partial_tail_byte_length": report.partial_tail_bytes,
            "partial_tail_fragment_count": 1,
            "reason": normalized_reason,
            "operator_reference": normalized_reference,
        }
        recovery_record, recovery_raw = api.build_record(
            records,
            event_type=api.recovery_event,
            attributes=attributes,
            now=now,
        )
        if report.verified_prefix_bytes + len(recovery_raw) > api.max_registry_bytes:
            raise ProductApprovalError(
                "product_approval.registry_limit",
                "verified prefix cannot accommodate the recovery record",
            )

        current_report, current_records = _recovery_report_locked(descriptor, api=api)
        _revalidate_registry_path(registry, descriptor, current_report)
        if current_report.guard_tuple() != report.guard_tuple() or _active_signature(
            api.active_records(current_records)
        ) != _active_signature(active_before):
            raise ProductApprovalError(
                "product_approval.recovery_changed",
                "approval registry changed after recovery inspection",
            )

        try:
            os.ftruncate(descriptor, report.verified_prefix_bytes)
            os.fsync(descriptor)
        except OSError:
            raise ProductApprovalError(
                "product_approval.recovery_truncate",
                "approval registry partial tail could not be removed",
            ) from None

        try:
            os.lseek(descriptor, 0, os.SEEK_END)
            _write_all(descriptor, recovery_raw)
            os.fsync(descriptor)
        except (OSError, ProjectError) as error:
            try:
                os.ftruncate(descriptor, report.verified_prefix_bytes)
                os.fsync(descriptor)
            except OSError:
                pass
            raise ProductApprovalError(
                "product_approval.recovery_write",
                "approval recovery record append did not complete",
            ) from error

        try:
            handle.seek(0)
            final_records, final_bytes = api.verify(handle)
            active_after = api.active_records(final_records)
            if (
                final_bytes != report.verified_prefix_bytes + len(recovery_raw)
                or final_records[-1]["record_id"] != recovery_record["record_id"]
                or _active_signature(active_after) != _active_signature(active_before)
            ):
                raise ProductApprovalError(
                    "product_approval.recovery_verify",
                    "recovered approval registry did not preserve active approvals",
                )
        except ProductApprovalError:
            try:
                os.ftruncate(descriptor, report.verified_prefix_bytes)
                os.fsync(descriptor)
            except OSError:
                pass
            raise
        api.begin_operation()
        return {
            "ok": True,
            "status": "recovered_partial_tail",
            "registry": str(registry),
            "archive_path": archive_path,
            "original": report.summary(),
            "recovery_record": recovery_record,
            "record_count": len(final_records),
            "active_approval_count": len(active_after),
            "active_approvals_unchanged": True,
        }
    finally:
        handle.close()
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
