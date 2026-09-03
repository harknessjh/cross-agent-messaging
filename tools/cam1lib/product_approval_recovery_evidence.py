# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Immutable evidence artifacts for approval-ledger partial-tail recovery."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import secrets
import uuid
from pathlib import Path
from typing import Any, Protocol

from jsonschema import Draft202012Validator, FormatChecker

from .native_fs import rename_noreplace
from .product_executables import (
    ProductApprovalError,
    _bounded_text,
    _canonical_json,
    _digest,
)
from .secure_fs import (
    PRIVATE_FILE_MODE,
    _object_without_duplicates,
    _open_private_directory,
    _prepare_created_private_file,
    _reject_constant,
    _unlink_matching_entry,
    _validate_private_file,
    _write_all,
)

COPY_CHUNK_BYTES = 1_048_576
ARCHIVE_PREFIX = "product-executables-v1.damaged-"
ARCHIVE_SUFFIX = ".jsonl"
MANIFEST_PREFIX = "product-executables-v1.recovery-"
MANIFEST_SUFFIX = ".json"
RECOVERY_FORMAT = "CAM-PRODUCT-EXECUTABLE-RECOVERY/1"
MAX_RECOVERY_REASON_LENGTH = 500
RECOVERY_NAMESPACE = uuid.UUID("31a657d3-a589-44a9-9f97-d698ef769342")
READ_FLAGS = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "schemas"
    / "cam-product-executable-recovery-1.schema.json"
)


class PartialTailReport(Protocol):
    registry_sha256: str
    registry_bytes: int
    verified_prefix_sha256: str
    verified_prefix_bytes: int
    verified_prefix_record_count: int
    verified_prefix_last_record_sha256: str | None
    partial_tail_sha256: str
    partial_tail_bytes: int


def _load_validator() -> Draft202012Validator:
    with SCHEMA_PATH.open("r", encoding="utf-8") as handle:
        schema = json.load(handle)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


VALIDATOR = _load_validator()


def _range_bytes(descriptor: int, *, offset: int, length: int) -> bytes:
    chunks: list[bytes] = []
    position = offset
    remaining = length
    while remaining:
        try:
            chunk = os.pread(descriptor, min(COPY_CHUNK_BYTES, remaining), position)
        except OSError:
            raise ProductApprovalError(
                "product_approval.recovery_read",
                "approval recovery evidence could not be read",
            ) from None
        if not chunk:
            raise ProductApprovalError(
                "product_approval.recovery_changed",
                "approval recovery evidence changed during inspection",
            )
        chunks.append(chunk)
        position += len(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _range_digest(descriptor: int, *, offset: int, length: int) -> str:
    digest = hashlib.sha256()
    position = offset
    remaining = length
    while remaining:
        try:
            chunk = os.pread(descriptor, min(COPY_CHUNK_BYTES, remaining), position)
        except OSError:
            raise ProductApprovalError(
                "product_approval.recovery_read",
                "approval recovery evidence could not be read",
            ) from None
        if not chunk:
            raise ProductApprovalError(
                "product_approval.recovery_changed",
                "approval recovery evidence changed during inspection",
            )
        digest.update(chunk)
        position += len(chunk)
        remaining -= len(chunk)
    return digest.hexdigest()


def _copy_range(
    source_descriptor: int,
    target_descriptor: int,
    *,
    length: int,
) -> str:
    digest = hashlib.sha256()
    position = 0
    remaining = length
    while remaining:
        try:
            chunk = os.pread(
                source_descriptor,
                min(COPY_CHUNK_BYTES, remaining),
                position,
            )
        except OSError:
            raise ProductApprovalError(
                "product_approval.recovery_read",
                "approval registry could not be copied for recovery",
            ) from None
        if not chunk:
            raise ProductApprovalError(
                "product_approval.recovery_changed",
                "approval registry changed while recovery evidence was written",
            )
        _write_all(target_descriptor, chunk)
        digest.update(chunk)
        position += len(chunk)
        remaining -= len(chunk)
    return digest.hexdigest()


def validate_archive_name(value: Any) -> str:
    filename = _bounded_text(value, label="archive_file", maximum=255)
    if (
        Path(filename).name != filename
        or not filename.startswith(ARCHIVE_PREFIX)
        or not filename.endswith(ARCHIVE_SUFFIX)
    ):
        raise ProductApprovalError(
            "product_approval.recovery_archive",
            "approval recovery archive filename is invalid",
        )
    token = filename[len(ARCHIVE_PREFIX) : -len(ARCHIVE_SUFFIX)]
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


def _utc_text(value: dt.datetime | None = None) -> str:
    observed = value or dt.datetime.now(dt.UTC)
    if observed.tzinfo is None or observed.utcoffset() is None:
        raise ProductApprovalError(
            "product_approval.timestamp",
            "approval recovery timestamp must be timezone-aware",
        )
    return (
        observed.astimezone(dt.UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def recovery_id(report: PartialTailReport) -> str:
    return str(uuid.uuid5(RECOVERY_NAMESPACE, report.registry_sha256))


def _manifest_name(recovery_id: str) -> str:
    return f"{MANIFEST_PREFIX}{recovery_id}{MANIFEST_SUFFIX}"


def parse_recovery_manifest(raw: bytes) -> dict[str, Any]:
    """Verify one immutable recovery-evidence artifact."""

    if not raw or raw.endswith(b"\n") or len(raw) > 32_768:
        raise ProductApprovalError(
            "product_approval.recovery_manifest",
            "approval recovery manifest framing is invalid",
        )
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise ProductApprovalError(
            "product_approval.recovery_manifest",
            "approval recovery manifest is invalid JSON",
        ) from None
    if not isinstance(value, dict) or list(VALIDATOR.iter_errors(value)):
        raise ProductApprovalError(
            "product_approval.recovery_manifest",
            "approval recovery manifest failed schema validation",
        )
    if _canonical_json(value) != raw:
        raise ProductApprovalError(
            "product_approval.recovery_manifest",
            "approval recovery manifest is not canonical JSON",
        )
    unsigned = dict(value)
    claimed = unsigned.pop("record_sha256")
    if not secrets.compare_digest(claimed, _digest(unsigned)):
        raise ProductApprovalError(
            "product_approval.recovery_manifest",
            "approval recovery manifest digest is invalid",
        )
    validate_archive_name(value["archive_file"])
    _bounded_text(
        value["reason"],
        label="reason",
        maximum=MAX_RECOVERY_REASON_LENGTH,
    )
    if (
        value["archive_sha256"] != value["damaged_registry_sha256"]
        or value["archive_byte_length"] != value["damaged_registry_byte_length"]
        or value["verified_prefix_byte_length"] + value["partial_tail_byte_length"]
        != value["damaged_registry_byte_length"]
        or value["partial_tail_fragment_count"] != 1
    ):
        raise ProductApprovalError(
            "product_approval.recovery_manifest",
            "approval recovery manifest guards are internally inconsistent",
        )
    return value


def inspect_recovery_evidence(
    approvals_directory: Path,
    *,
    report: PartialTailReport,
) -> dict[str, Any]:
    """Inspect deterministic immutable evidence for this exact damaged ledger."""

    identifier = recovery_id(report)
    archive_name = f"{ARCHIVE_PREFIX}{identifier}{ARCHIVE_SUFFIX}"
    manifest_name = _manifest_name(identifier)
    directory_descriptor = _open_private_directory(
        approvals_directory, label="approval.directory"
    )
    try:
        archive_present = False
        try:
            archive_descriptor = os.open(
                archive_name, READ_FLAGS, dir_fd=directory_descriptor
            )
        except FileNotFoundError:
            archive_descriptor = None
        except OSError:
            raise ProductApprovalError(
                "product_approval.recovery_archive_changed",
                "existing approval recovery archive is unsafe",
            ) from None
        if archive_descriptor is not None:
            try:
                metadata = _validate_private_file(
                    archive_descriptor, label="approval.recovery_archive"
                )
                digest = _range_digest(
                    archive_descriptor,
                    offset=0,
                    length=report.registry_bytes,
                )
                if (
                    metadata.st_size != report.registry_bytes
                    or not secrets.compare_digest(digest, report.registry_sha256)
                ):
                    raise ProductApprovalError(
                        "product_approval.recovery_archive_changed",
                        "existing recovery archive does not match the damaged ledger",
                    )
                archive_present = True
            finally:
                os.close(archive_descriptor)

        manifest: dict[str, Any] | None = None
        try:
            manifest_descriptor = os.open(
                manifest_name, READ_FLAGS, dir_fd=directory_descriptor
            )
        except FileNotFoundError:
            manifest_descriptor = None
        except OSError:
            raise ProductApprovalError(
                "product_approval.recovery_manifest_changed",
                "existing approval recovery manifest is unsafe",
            ) from None
        if manifest_descriptor is not None:
            try:
                metadata = _validate_private_file(
                    manifest_descriptor, label="approval.recovery_manifest"
                )
                if metadata.st_size > 32_768:
                    raise ProductApprovalError(
                        "product_approval.recovery_manifest_changed",
                        "existing approval recovery manifest is oversized",
                    )
                manifest = parse_recovery_manifest(
                    _range_bytes(
                        manifest_descriptor,
                        offset=0,
                        length=metadata.st_size,
                    )
                )
            finally:
                os.close(manifest_descriptor)
            expected = {
                "recovery_id": identifier,
                "registry_file": "product-executables-v1.jsonl",
                "archive_file": archive_name,
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
            }
            if any(
                manifest.get(key) != expected_value
                for key, expected_value in expected.items()
            ):
                raise ProductApprovalError(
                    "product_approval.recovery_manifest_changed",
                    "existing recovery manifest does not match the damaged ledger",
                )
        if manifest is not None and not archive_present:
            raise ProductApprovalError(
                "product_approval.recovery_evidence_incomplete",
                "approval recovery manifest exists without its damaged archive",
            )
        return {
            "status": (
                "prepared"
                if manifest is not None
                else "archive_only"
                if archive_present
                else "absent"
            ),
            "recovery_id": identifier,
            "archive_path": str(approvals_directory / archive_name),
            "manifest_path": str(approvals_directory / manifest_name),
            "manifest": manifest,
        }
    finally:
        os.close(directory_descriptor)


def create_recovery_archive(
    approvals_directory: Path,
    *,
    source_descriptor: int,
    report: PartialTailReport,
) -> tuple[str, str]:
    """Publish and verify an exact owner-private copy before source mutation."""

    existing = inspect_recovery_evidence(approvals_directory, report=report)
    if existing["status"] in {"archive_only", "prepared"}:
        return Path(existing["archive_path"]).name, existing["archive_path"]
    directory_descriptor = _open_private_directory(
        approvals_directory, label="approval.directory"
    )
    identifier = recovery_id(report)
    pending_name = f".product-approval-recovery-{uuid.uuid4().hex}.pending"
    archive_name = f"{ARCHIVE_PREFIX}{identifier}{ARCHIVE_SUFFIX}"
    descriptor: int | None = None
    pending_identity: tuple[int, int] | None = None
    pending_exists = False
    try:
        try:
            descriptor = os.open(
                pending_name,
                CREATE_FLAGS,
                PRIVATE_FILE_MODE,
                dir_fd=directory_descriptor,
            )
            pending_exists = True
            metadata = os.fstat(descriptor)
            pending_identity = (metadata.st_dev, metadata.st_ino)
        except OSError:
            raise ProductApprovalError(
                "product_approval.recovery_archive_create",
                "approval recovery archive could not be created securely",
            ) from None
        _prepare_created_private_file(descriptor, label="approval.recovery_archive")
        copied_digest = _copy_range(
            source_descriptor,
            descriptor,
            length=report.registry_bytes,
        )
        if not secrets.compare_digest(copied_digest, report.registry_sha256):
            raise ProductApprovalError(
                "product_approval.recovery_changed",
                "approval registry changed while the recovery archive was written",
            )
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
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
        verified_descriptor = os.open(
            archive_name, READ_FLAGS, dir_fd=directory_descriptor
        )
        try:
            sealed = _validate_private_file(
                verified_descriptor, label="approval.recovery_archive"
            )
            digest = _range_digest(
                verified_descriptor,
                offset=0,
                length=report.registry_bytes,
            )
            if (
                pending_identity != (sealed.st_dev, sealed.st_ino)
                or sealed.st_size != report.registry_bytes
                or not secrets.compare_digest(digest, report.registry_sha256)
            ):
                raise ProductApprovalError(
                    "product_approval.recovery_archive_changed",
                    "sealed approval recovery archive changed",
                )
        finally:
            os.close(verified_descriptor)
        os.fsync(directory_descriptor)
        return archive_name, str(approvals_directory / archive_name)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if pending_exists and pending_identity is not None:
            _unlink_matching_entry(directory_descriptor, pending_name, pending_identity)
        os.close(directory_descriptor)


def create_recovery_manifest(
    approvals_directory: Path,
    *,
    archive_file: str,
    report: PartialTailReport,
    reason: str,
    operator_reference: str,
    now: dt.datetime | None,
) -> tuple[dict[str, Any], str]:
    """Publish immutable prepared-recovery evidence before ledger mutation."""

    archive_name = validate_archive_name(archive_file)
    identifier = archive_name[len(ARCHIVE_PREFIX) : -len(ARCHIVE_SUFFIX)]
    existing = inspect_recovery_evidence(approvals_directory, report=report)
    if existing["status"] == "prepared":
        manifest = existing["manifest"]
        if (
            not isinstance(manifest, dict)
            or manifest["reason"] != reason
            or manifest["operator_reference"] != operator_reference
        ):
            raise ProductApprovalError(
                "product_approval.recovery_evidence_exists",
                "immutable recovery evidence already exists for these bytes with "
                f"different operator context: {existing['manifest_path']}",
            )
        return manifest, existing["manifest_path"]
    unsigned = {
        "format": RECOVERY_FORMAT,
        "recovery_id": identifier,
        "prepared_at": _utc_text(now),
        "status": "prepared",
        "registry_file": "product-executables-v1.jsonl",
        "archive_file": archive_name,
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
        "reason": reason,
        "operator_reference": operator_reference,
    }
    manifest = {**unsigned, "record_sha256": _digest(unsigned)}
    raw = _canonical_json(manifest)
    parse_recovery_manifest(raw)

    directory_descriptor = _open_private_directory(
        approvals_directory, label="approval.directory"
    )
    pending_name = f".product-approval-recovery-{uuid.uuid4().hex}.manifest.pending"
    manifest_name = _manifest_name(identifier)
    descriptor: int | None = None
    pending_identity: tuple[int, int] | None = None
    published = False
    succeeded = False
    try:
        descriptor = os.open(
            pending_name,
            CREATE_FLAGS,
            PRIVATE_FILE_MODE,
            dir_fd=directory_descriptor,
        )
        metadata = os.fstat(descriptor)
        pending_identity = (metadata.st_dev, metadata.st_ino)
        _prepare_created_private_file(descriptor, label="approval.recovery_manifest")
        _write_all(descriptor, raw)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        rename_noreplace(directory_descriptor, pending_name, manifest_name)
        published = True
        verified_descriptor = os.open(
            manifest_name, READ_FLAGS, dir_fd=directory_descriptor
        )
        try:
            sealed = _validate_private_file(
                verified_descriptor, label="approval.recovery_manifest"
            )
            observed = _range_bytes(
                verified_descriptor,
                offset=0,
                length=len(raw),
            )
            if pending_identity != (sealed.st_dev, sealed.st_ino) or observed != raw:
                raise ProductApprovalError(
                    "product_approval.recovery_manifest_changed",
                    "sealed approval recovery manifest changed",
                )
            parse_recovery_manifest(observed)
        finally:
            os.close(verified_descriptor)
        os.fsync(directory_descriptor)
        succeeded = True
        return manifest, str(approvals_directory / manifest_name)
    except FileExistsError:
        raise ProductApprovalError(
            "product_approval.recovery_manifest_exists",
            "approval recovery manifest destination already exists",
        ) from None
    except OSError:
        raise ProductApprovalError(
            "product_approval.recovery_manifest_create",
            "approval recovery manifest could not be published securely",
        ) from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if not succeeded and not published and pending_identity is not None:
            _unlink_matching_entry(directory_descriptor, pending_name, pending_identity)
        os.close(directory_descriptor)
