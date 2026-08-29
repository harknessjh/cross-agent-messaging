# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Descriptor-safe inspection and replacement for incomplete journal tails.

This module owns only the filesystem mechanics of recovery. The canonical
record codec and chain verifier remain in :mod:`cam1lib.journal` and are passed
in where recovery must verify an existing prefix.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import uuid
from collections.abc import Callable
from typing import Any

from .journal_types import (
    MAX_JOURNAL_BYTES,
    JournalError,
    JournalVerification,
    PartialTailReport,
)
from .native_fs import rename_noreplace
from .project import (
    ProjectBinding,
)
from .secure_fs import (
    PRIVATE_FILE_MODE,
    _ensure_private_child,
    _open_private_directory,
    _prepare_created_private_file,
    _unlink_matching_entry,
    _validate_private_file,
    _validate_private_file_metadata,
    _write_all,
)

_COPY_CHUNK_BYTES = 1_048_576
_JOURNAL_READ_FLAGS = (
    os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
)
_RECOVERY_CREATE_FLAGS = (
    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
)

VerifyRecords = Callable[
    ...,
    tuple[list[dict[str, Any]], JournalVerification],
]


def _range_digest(descriptor: int, *, offset: int, length: int) -> str:
    digest = hashlib.sha256()
    position = offset
    remaining = length
    while remaining:
        try:
            chunk = os.pread(descriptor, min(_COPY_CHUNK_BYTES, remaining), position)
        except OSError:
            raise JournalError(
                "journal.recovery_read", "journal could not be read for recovery"
            ) from None
        if not chunk:
            raise JournalError(
                "journal.recovery_changed", "journal changed during recovery inspection"
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
            raise JournalError(
                "journal.recovery_read", "journal could not be copied for recovery"
            ) from None
        if not chunk:
            raise JournalError(
                "journal.recovery_changed", "journal changed during recovery copying"
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
            raise JournalError(
                "journal.recovery_read", "journal tail could not be inspected"
            ) from None
        if len(chunk) != position - start:
            raise JournalError(
                "journal.recovery_changed", "journal changed during recovery inspection"
            )
        newline = chunk.rfind(b"\n")
        if newline >= 0:
            return start + newline + 1
        position = start
    return 0


def _inspect_partial_tail_locked(
    descriptor: int,
    handle: Any,
    *,
    project_id: str,
    verify_records: VerifyRecords,
) -> PartialTailReport:
    metadata = os.fstat(descriptor)
    size = metadata.st_size
    if size == 0:
        raise JournalError(
            "journal.recovery_not_needed", "journal is empty and has no partial tail"
        )
    if size > MAX_JOURNAL_BYTES:
        raise JournalError(
            "journal.size_limit", f"journal exceeds {MAX_JOURNAL_BYTES} bytes"
        )
    try:
        final_byte = os.pread(descriptor, 1, size - 1)
    except OSError:
        raise JournalError(
            "journal.recovery_read", "journal tail could not be inspected"
        ) from None
    if final_byte == b"\n":
        # A newline-terminated schema, digest, sequence, or chain failure is not
        # an incomplete write and must remain an investigation-only failure.
        handle.seek(0)
        verify_records(handle, project_id=project_id, collect=False)
        raise JournalError(
            "journal.recovery_not_needed", "journal has no incomplete EOF record"
        )
    prefix_bytes = _last_complete_record_offset(descriptor, size=size)
    handle.seek(0)
    _, prefix_verification = verify_records(
        handle,
        project_id=project_id,
        collect=False,
        byte_limit=prefix_bytes,
    )
    tail_bytes = size - prefix_bytes
    return PartialTailReport(
        journal_sha256=_range_digest(descriptor, offset=0, length=size),
        journal_bytes=size,
        verified_prefix_bytes=prefix_bytes,
        verified_prefix_sha256=_range_digest(descriptor, offset=0, length=prefix_bytes),
        partial_tail_bytes=tail_bytes,
        partial_tail_sha256=_range_digest(
            descriptor, offset=prefix_bytes, length=tail_bytes
        ),
        prefix_verification=prefix_verification,
    )


def _create_recovery_archive(
    project: ProjectBinding,
    *,
    source_descriptor: int,
    report: PartialTailReport,
) -> tuple[str, str]:
    recovery_dir = _ensure_private_child(
        project.project_dir, "recovery", label="journal.recovery_directory"
    )
    directory_descriptor = _open_private_directory(
        recovery_dir, label="journal.recovery_directory"
    )
    token = str(uuid.uuid4())
    pending_name = f".pending-{token}.jsonl"
    archive_name = f"damaged-{token}.jsonl"
    archive_descriptor: int | None = None
    pending_exists = False
    pending_identity: tuple[int, int] | None = None
    sealed_exists = False
    succeeded = False
    try:
        try:
            archive_descriptor = os.open(
                pending_name,
                _RECOVERY_CREATE_FLAGS,
                PRIVATE_FILE_MODE,
                dir_fd=directory_descriptor,
            )
            pending_exists = True
            metadata = os.fstat(archive_descriptor)
            pending_identity = (metadata.st_dev, metadata.st_ino)
        except OSError:
            raise JournalError(
                "journal.recovery_archive_create",
                "recovery archive could not be created securely",
            ) from None
        _prepare_created_private_file(
            archive_descriptor, label="journal.recovery_archive"
        )
        copied_digest = _copy_range(
            source_descriptor,
            archive_descriptor,
            offset=0,
            length=report.journal_bytes,
        )
        if not secrets.compare_digest(copied_digest, report.journal_sha256):
            raise JournalError(
                "journal.recovery_changed",
                "journal changed while the recovery archive was written",
            )
        os.fsync(archive_descriptor)
        os.close(archive_descriptor)
        archive_descriptor = None
        try:
            rename_noreplace(directory_descriptor, pending_name, archive_name)
        except FileExistsError:
            raise JournalError(
                "journal.recovery_archive_exists",
                "recovery archive destination already exists",
            ) from None
        except OSError:
            raise JournalError(
                "journal.recovery_archive_seal",
                "recovery archive could not be sealed",
            ) from None
        pending_exists = False
        sealed_exists = True
        try:
            verified_descriptor = os.open(
                archive_name, _JOURNAL_READ_FLAGS, dir_fd=directory_descriptor
            )
        except OSError:
            raise JournalError(
                "journal.recovery_archive_seal",
                "sealed recovery archive could not be verified",
            ) from None
        try:
            sealed_metadata = _validate_private_file(
                verified_descriptor, label="journal.recovery_archive"
            )
            if (
                pending_identity != (sealed_metadata.st_dev, sealed_metadata.st_ino)
                or sealed_metadata.st_size != report.journal_bytes
            ):
                raise JournalError(
                    "journal.recovery_archive_changed",
                    "sealed recovery archive identity changed",
                )
            sealed_digest = _range_digest(
                verified_descriptor, offset=0, length=report.journal_bytes
            )
            if not secrets.compare_digest(sealed_digest, report.journal_sha256):
                raise JournalError(
                    "journal.recovery_archive_changed",
                    "sealed recovery archive contents changed",
                )
        finally:
            os.close(verified_descriptor)
        os.fsync(directory_descriptor)
        succeeded = True
        return archive_name, f"recovery/{archive_name}"
    finally:
        if archive_descriptor is not None:
            os.close(archive_descriptor)
        if pending_exists and pending_identity is not None:
            # A failed or identity-mismatched cleanup leaves an incomplete
            # artifact; it is never interpreted as a sealed recovery archive.
            _unlink_matching_entry(directory_descriptor, pending_name, pending_identity)
        if not succeeded and sealed_exists and pending_identity is not None:
            _unlink_matching_entry(directory_descriptor, archive_name, pending_identity)
        os.close(directory_descriptor)


def _replace_partial_journal(
    project: ProjectBinding,
    *,
    source_descriptor: int,
    source_metadata: os.stat_result,
    report: PartialTailReport,
    recovery_record_raw: bytes,
) -> None:
    replacement_bytes = report.verified_prefix_bytes + len(recovery_record_raw)
    if replacement_bytes > MAX_JOURNAL_BYTES:
        raise JournalError(
            "journal.size_limit",
            "recovered journal plus recovery record exceeds the journal limit",
        )
    directory_descriptor = _open_private_directory(
        project.project_dir, label="project.directory"
    )
    temporary_name = f".journal-recovery-{uuid.uuid4()}.tmp"
    temporary_descriptor: int | None = None
    temporary_exists = False
    temporary_identity: tuple[int, int] | None = None
    try:
        try:
            temporary_descriptor = os.open(
                temporary_name,
                _RECOVERY_CREATE_FLAGS,
                PRIVATE_FILE_MODE,
                dir_fd=directory_descriptor,
            )
            temporary_exists = True
            metadata = os.fstat(temporary_descriptor)
            temporary_identity = (metadata.st_dev, metadata.st_ino)
        except OSError:
            raise JournalError(
                "journal.recovery_temp_create",
                "recovery replacement could not be created securely",
            ) from None
        _prepare_created_private_file(
            temporary_descriptor, label="journal.recovery_replacement"
        )
        copied_prefix = _copy_range(
            source_descriptor,
            temporary_descriptor,
            offset=0,
            length=report.verified_prefix_bytes,
        )
        if not secrets.compare_digest(copied_prefix, report.verified_prefix_sha256):
            raise JournalError(
                "journal.recovery_changed",
                "verified journal prefix changed during recovery",
            )
        _write_all(temporary_descriptor, recovery_record_raw)
        os.fsync(temporary_descriptor)
        os.close(temporary_descriptor)
        temporary_descriptor = None

        try:
            path_metadata = os.stat(
                project.journal_path.name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
        except OSError:
            raise JournalError(
                "journal.recovery_changed", "journal path changed during recovery"
            ) from None
        _validate_private_file_metadata(path_metadata, label="journal.file")
        current_metadata = os.fstat(source_descriptor)
        identity_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if any(
            getattr(path_metadata, field) != getattr(source_metadata, field)
            or getattr(current_metadata, field) != getattr(source_metadata, field)
            for field in identity_fields
        ):
            raise JournalError(
                "journal.recovery_changed", "journal changed during recovery"
            )
        current_digest = _range_digest(
            source_descriptor, offset=0, length=report.journal_bytes
        )
        if not secrets.compare_digest(current_digest, report.journal_sha256):
            raise JournalError(
                "journal.recovery_changed", "journal contents changed during recovery"
            )
        try:
            os.replace(
                temporary_name,
                project.journal_path.name,
                src_dir_fd=directory_descriptor,
                dst_dir_fd=directory_descriptor,
            )
        except OSError:
            raise JournalError(
                "journal.recovery_replace",
                "recovery replacement could not be installed atomically",
            ) from None
        temporary_exists = False
        os.fsync(directory_descriptor)
    finally:
        if temporary_descriptor is not None:
            os.close(temporary_descriptor)
        if temporary_exists and temporary_identity is not None:
            _unlink_matching_entry(
                directory_descriptor, temporary_name, temporary_identity
            )
        os.close(directory_descriptor)
