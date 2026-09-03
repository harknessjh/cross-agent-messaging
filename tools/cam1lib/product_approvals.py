# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Account-scoped approval ledger for local product executables.

Approval permits CAM to invoke that unchanged executable for product I/O.  It
does not authenticate its vendor or a session, authorize a message or workload
action, or establish that the program is trustworthy.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import secrets
import shlex
import stat
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, cast

from jsonschema import Draft202012Validator, FormatChecker

from . import product_approval_recovery as _recovery
from .errors import ProjectError
from .product_executables import (
    ExecutableCandidate,
    ExecutableFingerprint,
    ProductApprovalError,
    _bounded_text,
    _candidate_digest,
    _canonical_json,
    _digest,
    _metadata_opened,
    _resolved_candidate_path,
    _sha256,
    _vendor,
    discover_candidate,
    discovery_command,
)
from .secure_fs import (
    PRIVATE_FILE_MODE,
    _ensure_private_child,
    _object_without_duplicates,
    _open_private_directory,
    _prepare_created_private_file,
    _reject_constant,
    _validate_private_file,
    _write_all,
    account_home,
)

REGISTRY_FORMAT = "CAM-PRODUCT-EXECUTABLE-APPROVAL/1"
REGISTRY_NAME = "product-executables-v1.jsonl"
APPROVAL_EVENT = "product_executable.approved"
REVOCATION_EVENT = "product_executable.revoked"
RECOVERY_EVENT = "product_executable.recovered_partial_tail"
MAX_RECORD_BYTES = 32_768
MAX_REGISTRY_BYTES = 16 * 1_048_576
MAX_REGISTRY_RECORDS = 10_000
REGISTRY_LOCK_TIMEOUT_SECONDS = 5.0
REGISTRY_LOCK_POLL_SECONDS = 0.05
_READ_FLAGS = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
_APPEND_FLAGS = (
    os.O_RDWR
    | os.O_APPEND
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_CREATE_FLAGS = (
    os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
)
_SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "schemas"
    / "cam-product-executable-approval-1.schema.json"
)
_EXECUTABLE_METADATA_FIELDS = (
    "size",
    "uid",
    "mode",
    "dev",
    "inode",
    "ctime_ns",
)
_RESERVED_OPERATOR_REFERENCES = frozenset({"DIRECT_OPERATOR_REFERENCE"})


@dataclass(frozen=True, slots=True)
class _VerifiedApproval:
    """Process-local attestation for one fully verified approval record.

    Public CLI invocations are one-shot processes.  Retaining this bounded
    snapshot avoids replaying a potentially 16 MiB registry before every
    subprocess in the same invocation.  Every reuse still opens and validates
    the registry path, compares its complete cheap identity tuple, and checks
    the executable's approved non-content metadata.  A changed registry is
    replayed before the approval can be reused.
    """

    registry: str
    registry_identity: tuple[int, ...]
    vendor: str
    canonical_path: str
    record_id: str
    record_sha256: str
    fingerprint_sha256: str
    basis: str
    executable_metadata: tuple[int, ...]

    def summary(self) -> dict[str, Any]:
        return {
            "registry": self.registry,
            "vendor": self.vendor,
            "canonical_path": self.canonical_path,
            "record_id": self.record_id,
            "record_sha256": self.record_sha256,
            "fingerprint_sha256": self.fingerprint_sha256,
            "basis": self.basis,
        }


_VERIFIED_APPROVALS: dict[tuple[str, str, str], _VerifiedApproval] = {}
_VERIFIED_APPROVALS_LOCK = threading.RLock()


def begin_operation() -> None:
    """Discard executable attestations retained by an earlier API operation.

    Public CAM commands are one-shot processes, but tests and embedding callers
    can invoke ``main()`` repeatedly in one interpreter.  Treat each such call
    as a fresh operation so every live command performs one full content check
    per product before relying on cheap prelaunch metadata checks.
    """

    with _VERIFIED_APPROVALS_LOCK:
        _VERIFIED_APPROVALS.clear()


def _operator_reference(value: Any) -> str:
    reference = _bounded_text(
        value,
        label="operator_reference",
        maximum=1_024,
    )
    if reference in _RESERVED_OPERATOR_REFERENCES:
        raise ProductApprovalError(
            "product_approval.operator_reference_reserved",
            "replace DIRECT_OPERATOR_REFERENCE with the direct operator's actual "
            "approval reference before changing the account approval ledger",
        )
    return reference


def _load_validator() -> Draft202012Validator:
    with _SCHEMA_PATH.open("r", encoding="utf-8") as handle:
        schema = json.load(handle)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


_VALIDATOR = _load_validator()


def _utc_text(value: dt.datetime | None = None) -> str:
    observed = value or dt.datetime.now(dt.UTC)
    if observed.tzinfo is None or observed.utcoffset() is None:
        raise ProductApprovalError(
            "product_approval.timestamp", "approval timestamp must be timezone-aware"
        )
    return (
        observed.astimezone(dt.UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _canonical_uuid(value: Any, *, label: str) -> str:
    text = _bounded_text(value, label=label, maximum=36)
    try:
        canonical = str(uuid.UUID(text))
    except (AttributeError, ValueError):
        raise ProductApprovalError(
            "product_approval.record_id",
            f"{label} must be a UUID",
        ) from None
    if text != canonical:
        raise ProductApprovalError(
            "product_approval.record_id",
            f"{label} must use canonical lowercase UUID text",
        )
    return canonical


def _recorded_at(value: Any) -> str:
    text = _bounded_text(value, label="recorded_at", maximum=64)
    if not text.endswith("Z"):
        raise ProductApprovalError(
            "product_approval.timestamp",
            "approval record timestamp must use canonical UTC text",
        )
    try:
        parsed = dt.datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError:
        raise ProductApprovalError(
            "product_approval.timestamp",
            "approval record timestamp is invalid",
        ) from None
    if parsed.utcoffset() != dt.timedelta(0) or _utc_text(parsed) != text:
        raise ProductApprovalError(
            "product_approval.timestamp",
            "approval record timestamp must use canonical UTC text",
        )
    return text


def _canonical_stored_path(value: Any) -> str:
    text = _bounded_text(value, label="canonical_path", maximum=4_096)
    if not Path(text).is_absolute() or os.path.normpath(text) != text:
        raise ProductApprovalError(
            "product_approval.path",
            "approval record path must be a normalized absolute path",
        )
    return text


def _canonical_cli_path(value: Any, *, label: str) -> str:
    """Resolve one absolute CLI path without trusting shell-style expansion."""

    raw = Path(_bounded_text(value, label=label, maximum=4_096))
    if not raw.is_absolute():
        raise ProductApprovalError(
            "product_approval.absolute_path_required",
            f"{label} must be an absolute path",
        )
    unresolved: list[str] = []
    probe = raw
    while True:
        try:
            resolved = probe.resolve(strict=True)
            canonical = str(resolved.joinpath(*reversed(unresolved)))
            break
        except FileNotFoundError:
            parent = probe.parent
            if parent == probe:
                raise ProductApprovalError(
                    "product_approval.path",
                    f"{label} could not be resolved safely",
                ) from None
            unresolved.append(probe.name)
            probe = parent
        except (OSError, RuntimeError, ValueError):
            raise ProductApprovalError(
                "product_approval.path",
                f"{label} could not be resolved safely",
            ) from None
    return _canonical_stored_path(canonical)


def _registry_directory(*, create: bool) -> Path:
    home = account_home()
    cam = home / "CAM"
    approvals = cam / "Approvals"
    if create:
        cam = _ensure_private_child(home, "CAM", label="approval.cam_directory")
        approvals = _ensure_private_child(cam, "Approvals", label="approval.directory")
    else:
        try:
            cam_descriptor = _open_private_directory(
                cam, label="approval.cam_directory"
            )
            os.close(cam_descriptor)
            approval_descriptor = _open_private_directory(
                approvals, label="approval.directory"
            )
            os.close(approval_descriptor)
        except ProjectError as error:
            raise ProductApprovalError(error.code, error.detail) from error
    return approvals


def registry_path() -> Path:
    """Return the canonical account registry location without trusting HOME."""

    return _registry_directory(create=False) / REGISTRY_NAME


def _open_registry(*, exclusive: bool, create: bool) -> tuple[Path, int, BinaryIO]:
    directory = _registry_directory(create=create)
    parent_descriptor = _open_private_directory(directory, label="approval.directory")
    descriptor: int | None = None
    created_identity: tuple[int, int] | None = None
    try:
        flags = _APPEND_FLAGS if exclusive else _READ_FLAGS
        try:
            descriptor = os.open(REGISTRY_NAME, flags, dir_fd=parent_descriptor)
        except FileNotFoundError:
            if not create:
                raise ProductApprovalError(
                    "product_approval.registry_missing",
                    "no account product-executable approval registry exists",
                ) from None
            try:
                descriptor = os.open(
                    REGISTRY_NAME,
                    _CREATE_FLAGS,
                    PRIVATE_FILE_MODE,
                    dir_fd=parent_descriptor,
                )
            except FileExistsError:
                descriptor = os.open(
                    REGISTRY_NAME, _APPEND_FLAGS, dir_fd=parent_descriptor
                )
            else:
                created_metadata = _prepare_created_private_file(
                    descriptor, label="approval.registry"
                )
                created_identity = (
                    created_metadata.st_dev,
                    created_metadata.st_ino,
                )
                os.fsync(parent_descriptor)
        _validate_private_file(descriptor, label="approval.registry")
        _recovery.acquire_registry_lock(
            descriptor,
            exclusive=exclusive,
            timeout_seconds=REGISTRY_LOCK_TIMEOUT_SECONDS,
            poll_seconds=REGISTRY_LOCK_POLL_SECONDS,
        )
        # The mode/owner/link count can change while this process blocks on
        # the advisory lock.  Revalidate after acquisition, before trusting
        # any ledger bytes.
        _validate_private_file(descriptor, label="approval.registry")
        path_metadata = os.stat(
            REGISTRY_NAME, dir_fd=parent_descriptor, follow_symlinks=False
        )
        opened_metadata = os.fstat(descriptor)
        if (path_metadata.st_dev, path_metadata.st_ino) != (
            opened_metadata.st_dev,
            opened_metadata.st_ino,
        ):
            raise ProductApprovalError(
                "product_approval.registry_changed",
                "approval registry identity changed while it was opened",
            )
        os.lseek(descriptor, 0, os.SEEK_SET)
        handle = os.fdopen(os.dup(descriptor), "rb")
        return directory / REGISTRY_NAME, descriptor, handle
    except ProductApprovalError:
        if descriptor is not None:
            os.close(descriptor)
        _remove_created_registry(parent_descriptor, created_identity)
        raise
    except (OSError, ProjectError) as error:
        if descriptor is not None:
            os.close(descriptor)
        _remove_created_registry(parent_descriptor, created_identity)
        code = getattr(error, "code", "product_approval.registry_open")
        detail = getattr(
            error, "detail", "approval registry could not be opened safely"
        )
        raise ProductApprovalError(str(code), str(detail)) from error
    finally:
        os.close(parent_descriptor)


def _remove_created_registry(
    parent_descriptor: int, expected_identity: tuple[int, int] | None
) -> None:
    """Best-effort cleanup of an unchanged empty registry creation failure."""

    if expected_identity is None:
        return
    try:
        current = os.stat(
            REGISTRY_NAME,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (
            current.st_dev,
            current.st_ino,
        ) == expected_identity and current.st_size == 0:
            os.unlink(REGISTRY_NAME, dir_fd=parent_descriptor)
            os.fsync(parent_descriptor)
    except OSError:
        pass


def _parse_record(raw: bytes) -> dict[str, Any]:
    if not raw.endswith(b"\n") or len(raw) > MAX_RECORD_BYTES:
        raise ProductApprovalError(
            "product_approval.record",
            "approval registry record is incomplete or oversized",
        )
    try:
        value = json.loads(
            raw[:-1].decode("utf-8", errors="strict"),
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise ProductApprovalError(
            "product_approval.record", "approval registry contains invalid JSON"
        ) from None
    if not isinstance(value, dict) or list(_VALIDATOR.iter_errors(value)):
        raise ProductApprovalError(
            "product_approval.record",
            "approval registry record failed schema validation",
        )
    if _canonical_json(value) + b"\n" != raw:
        raise ProductApprovalError(
            "product_approval.noncanonical", "approval registry is not canonical JSONL"
        )
    _canonical_uuid(value["record_id"], label="record_id")
    _recorded_at(value["recorded_at"])
    unsigned = dict(value)
    claimed = cast(str, unsigned.pop("record_sha256"))
    if not secrets.compare_digest(claimed, _digest(unsigned)):
        raise ProductApprovalError(
            "product_approval.record_digest", "approval registry digest is invalid"
        )
    attributes = cast(dict[str, Any], value["attributes"])
    event_type = value["event_type"]
    _operator_reference(attributes["operator_reference"])
    if event_type == APPROVAL_EVENT:
        _canonical_stored_path(attributes["canonical_path"])
        if "fingerprint" not in attributes or "basis" not in attributes:
            raise ProductApprovalError(
                "product_approval.record",
                "approval event does not contain approval attributes",
            )
        fingerprint = ExecutableFingerprint(**attributes["fingerprint"])
        expected = _candidate_digest(
            attributes["vendor"], attributes["canonical_path"], fingerprint
        )
        if not secrets.compare_digest(attributes["fingerprint_sha256"], expected):
            raise ProductApprovalError(
                "product_approval.fingerprint_digest",
                "approval record fingerprint digest is invalid",
            )
        if (attributes["basis"] == "grandfathered_roster") != (
            attributes["migration"] is not None
        ):
            raise ProductApprovalError(
                "product_approval.migration",
                "approval record basis and migration evidence are inconsistent",
            )
        migration = attributes["migration"]
        if migration is not None:
            _canonical_uuid(migration["project_id"], label="migration.project_id")
            _canonical_uuid(
                migration["participant_id"],
                label="migration.participant_id",
            )
            _canonical_uuid(
                migration["source_reference"],
                label="migration.source_reference",
            )
    elif event_type == REVOCATION_EVENT:
        _canonical_stored_path(attributes["canonical_path"])
        if "approval_record_id" not in attributes or "fingerprint" in attributes:
            raise ProductApprovalError(
                "product_approval.record",
                "revocation event does not contain revocation attributes",
            )
        _canonical_uuid(
            attributes["approval_record_id"],
            label="approval_record_id",
        )
    elif event_type == RECOVERY_EVENT:
        if "archive_file" not in attributes:
            raise ProductApprovalError(
                "product_approval.record",
                "recovery event does not contain recovery attributes",
            )
        _recovery.validate_archive_name(attributes["archive_file"])
        _bounded_text(attributes["reason"], label="reason", maximum=500)
        if (
            attributes["archive_sha256"] != attributes["damaged_registry_sha256"]
            or attributes["archive_byte_length"]
            != attributes["damaged_registry_byte_length"]
            or attributes["verified_prefix_byte_length"]
            + attributes["partial_tail_byte_length"]
            != attributes["damaged_registry_byte_length"]
            or attributes["verified_prefix_record_count"] != value["sequence"] - 1
            or attributes["verified_prefix_last_record_sha256"]
            != value["previous_record_sha256"]
            or attributes["partial_tail_fragment_count"] != 1
        ):
            raise ProductApprovalError(
                "product_approval.recovery_record",
                "approval recovery record guards are internally inconsistent",
            )
    else:
        raise ProductApprovalError(
            "product_approval.record",
            "approval registry event type is unsupported",
        )
    return value


def _verify(handle: BinaryIO) -> tuple[list[dict[str, Any]], int]:
    records: list[dict[str, Any]] = []
    total = 0
    previous: str | None = None
    seen: set[str] = set()
    while True:
        raw = handle.readline(MAX_RECORD_BYTES + 1)
        if not raw:
            break
        total += len(raw)
        if total > MAX_REGISTRY_BYTES or len(records) >= MAX_REGISTRY_RECORDS:
            raise ProductApprovalError(
                "product_approval.registry_limit",
                "approval registry exceeds its bounded limits",
            )
        record = _parse_record(raw)
        if record["sequence"] != len(records) + 1:
            raise ProductApprovalError(
                "product_approval.sequence",
                "approval registry sequence is not contiguous",
            )
        if record["record_id"] in seen:
            raise ProductApprovalError(
                "product_approval.record_id",
                "approval registry record ID is duplicated",
            )
        if record["previous_record_sha256"] != previous:
            raise ProductApprovalError(
                "product_approval.chain", "approval registry hash chain is broken"
            )
        seen.add(record["record_id"])
        previous = record["record_sha256"]
        records.append(record)
    return records, total


def _active_records(
    records: list[dict[str, Any]],
) -> dict[tuple[str, str], dict[str, Any]]:
    active: dict[tuple[str, str], dict[str, Any]] = {}
    for record in records:
        if record["event_type"] == RECOVERY_EVENT:
            continue
        attributes = cast(dict[str, Any], record["attributes"])
        key = (cast(str, attributes["vendor"]), cast(str, attributes["canonical_path"]))
        if record["event_type"] == APPROVAL_EVENT:
            if key in active:
                raise ProductApprovalError(
                    "product_approval.duplicate_active",
                    "approval registry contains overlapping active approvals",
                )
            active[key] = record
            continue
        current = active.get(key)
        if (
            current is None
            or attributes["approval_record_id"] != current["record_id"]
            or attributes["fingerprint_sha256"]
            != current["attributes"]["fingerprint_sha256"]
        ):
            raise ProductApprovalError(
                "product_approval.revocation_target",
                "approval registry contains an invalid revocation target",
            )
        del active[key]
    return active


def _registry_identity(descriptor: int) -> tuple[int, ...]:
    metadata = os.fstat(descriptor)
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_uid,
        stat.S_IMODE(metadata.st_mode),
        metadata.st_nlink,
        metadata.st_ctime_ns,
        metadata.st_mtime_ns,
    )


def _metadata_tuple(value: dict[str, Any]) -> tuple[int, ...]:
    return tuple(cast(int, value[field]) for field in _EXECUTABLE_METADATA_FIELDS)


def _approval_cache_key(
    registry: Path, vendor: str, canonical_path: str
) -> tuple[str, str, str]:
    return str(registry), vendor, canonical_path


def _cache_verified_approval(
    *,
    registry: Path,
    descriptor: int,
    vendor: str,
    canonical_path: str,
    record: dict[str, Any],
) -> _VerifiedApproval:
    attributes = cast(dict[str, Any], record["attributes"])
    fingerprint = cast(dict[str, Any], attributes["fingerprint"])
    verified = _VerifiedApproval(
        registry=str(registry),
        registry_identity=_registry_identity(descriptor),
        vendor=vendor,
        canonical_path=canonical_path,
        record_id=cast(str, record["record_id"]),
        record_sha256=cast(str, record["record_sha256"]),
        fingerprint_sha256=cast(str, attributes["fingerprint_sha256"]),
        basis=cast(str, attributes["basis"]),
        executable_metadata=_metadata_tuple(fingerprint),
    )
    with _VERIFIED_APPROVALS_LOCK:
        _VERIFIED_APPROVALS[_approval_cache_key(registry, vendor, canonical_path)] = (
            verified
        )
    return verified


def _approval_required(vendor: str, canonical_path: str) -> ProductApprovalError:
    recovery = discovery_command(vendor, canonical_path)
    return ProductApprovalError(
        "product_approval.required",
        "product executable has no active account-scoped approval; run "
        f"{shlex.join(recovery)}, review its card, then run the exact "
        "approval_command it returns after replacing DIRECT_OPERATOR_REFERENCE",
    )


def _append_locked(
    descriptor: int,
    records: list[dict[str, Any]],
    total: int,
    *,
    event_type: str,
    attributes: dict[str, Any],
    now: dt.datetime | None,
) -> dict[str, Any]:
    if len(records) >= MAX_REGISTRY_RECORDS:
        raise ProductApprovalError(
            "product_approval.registry_limit",
            "approval registry exceeds its bounded limits",
        )
    record, raw = _build_record(
        records,
        event_type=event_type,
        attributes=attributes,
        now=now,
    )
    if total + len(raw) > MAX_REGISTRY_BYTES:
        raise ProductApprovalError(
            "product_approval.registry_limit",
            "approval registry exceeds its bounded limits",
        )
    os.lseek(descriptor, 0, os.SEEK_END)
    try:
        _write_all(descriptor, raw)
        os.fsync(descriptor)
    except (OSError, ProjectError) as error:
        try:
            os.ftruncate(descriptor, total)
            os.fsync(descriptor)
        except OSError:
            pass
        raise ProductApprovalError(
            "product_approval.write", "approval registry append did not complete"
        ) from error
    metadata = os.fstat(descriptor)
    if metadata.st_size != total + len(raw):
        raise ProductApprovalError(
            "product_approval.write",
            "approval registry size did not match the completed append",
        )
    return record


def _build_record(
    records: list[dict[str, Any]],
    *,
    event_type: str,
    attributes: dict[str, Any],
    now: dt.datetime | None,
) -> tuple[dict[str, Any], bytes]:
    """Build and locally verify one canonical record without mutating the ledger."""

    unsigned = {
        "format": REGISTRY_FORMAT,
        "sequence": len(records) + 1,
        "record_id": str(uuid.uuid4()),
        "recorded_at": _utc_text(now),
        "event_type": event_type,
        "previous_record_sha256": (records[-1]["record_sha256"] if records else None),
        "attributes": attributes,
    }
    record = {**unsigned, "record_sha256": _digest(unsigned)}
    raw = _canonical_json(record) + b"\n"
    _parse_record(raw)
    return record, raw


def _approve_fingerprinted_candidate(
    candidate: ExecutableCandidate,
    *,
    expected: str,
    operator_reference: str,
    basis: str,
    migration: dict[str, Any] | None,
    now: dt.datetime | None,
) -> dict[str, Any]:
    """Append an approval for one in-operation, fully hashed candidate."""

    normalized_vendor = _vendor(candidate.vendor)
    reference = _operator_reference(operator_reference)
    if basis not in {"operator_confirmation", "grandfathered_roster"}:
        raise ProductApprovalError(
            "product_approval.basis",
            "product executable approval basis is unsupported",
        )
    if (basis == "grandfathered_roster") != (migration is not None):
        raise ProductApprovalError(
            "product_approval.migration",
            "grandfathered approval requires migration evidence and direct approval "
            "must omit it",
        )
    if candidate.fingerprint_sha256 != _candidate_digest(
        normalized_vendor,
        candidate.canonical_path,
        candidate.fingerprint,
    ):
        raise ProductApprovalError(
            "product_approval.candidate_invalid",
            "product executable candidate fingerprint is internally inconsistent",
        )
    if candidate.fingerprint_sha256 != expected:
        raise ProductApprovalError(
            "product_approval.candidate_changed",
            "product executable no longer matches the reviewed candidate card",
        )
    registry, descriptor, handle = _open_registry(exclusive=True, create=True)
    try:
        records, total = _verify(handle)
        current_metadata = _metadata_opened(Path(candidate.canonical_path))
        if _metadata_tuple(current_metadata) != _metadata_tuple(
            candidate.fingerprint.as_dict()
        ):
            raise ProductApprovalError(
                "product_approval.candidate_changed",
                "product executable no longer matches the reviewed candidate card",
            )
        active = _active_records(records)
        key = (normalized_vendor, candidate.canonical_path)
        current = active.get(key)
        if current is not None:
            if current["attributes"]["fingerprint_sha256"] == expected:
                _cache_verified_approval(
                    registry=registry,
                    descriptor=descriptor,
                    vendor=normalized_vendor,
                    canonical_path=candidate.canonical_path,
                    record=current,
                )
                return {
                    "ok": True,
                    "status": "already_approved",
                    "registry": str(registry),
                    "approval": current,
                    "candidate": candidate.as_dict(),
                }
            raise ProductApprovalError(
                "product_approval.drift",
                "an active approval exists but its fingerprint no longer matches; "
                "run product-status, directly confirm a product-revoke using the "
                "active record ID and fingerprint guards, then rediscover and "
                "approve the replacement",
            )
        if basis == "grandfathered_roster" and any(
            record["event_type"] == APPROVAL_EVENT
            and record["attributes"]["vendor"] == normalized_vendor
            and record["attributes"]["canonical_path"] == candidate.canonical_path
            for record in records
        ):
            raise ProductApprovalError(
                "product_approval.grandfather_used",
                "this product path has prior approval history and cannot be grandfathered",
            )
        attributes = {
            "vendor": normalized_vendor,
            "canonical_path": candidate.canonical_path,
            "fingerprint": candidate.fingerprint.as_dict(),
            "fingerprint_sha256": candidate.fingerprint_sha256,
            "basis": basis,
            "operator_reference": reference,
            "migration": migration,
        }
        record = _append_locked(
            descriptor,
            records,
            total,
            event_type=APPROVAL_EVENT,
            attributes=attributes,
            now=now,
        )
        _cache_verified_approval(
            registry=registry,
            descriptor=descriptor,
            vendor=normalized_vendor,
            canonical_path=candidate.canonical_path,
            record=record,
        )
        return {
            "ok": True,
            "status": "approved",
            "registry": str(registry),
            "approval": record,
            "candidate": candidate.as_dict(),
        }
    finally:
        handle.close()
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def approve_candidate(
    *,
    vendor: str,
    product_bin: str,
    expected_fingerprint_sha256: str,
    operator_reference: str,
    basis: str = "operator_confirmation",
    migration: dict[str, Any] | None = None,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Append one approval only if the reviewed candidate is still unchanged."""

    normalized_vendor = _vendor(vendor)
    expected = _sha256(
        expected_fingerprint_sha256,
        label="expected_fingerprint_sha256",
    )
    candidate = discover_candidate(
        normalized_vendor,
        product_bin,
        allow_path_lookup=False,
    )
    return _approve_fingerprinted_candidate(
        candidate,
        expected=expected,
        operator_reference=operator_reference,
        basis=basis,
        migration=migration,
        now=now,
    )


def grandfather_candidate(
    *,
    vendor: str,
    product_bin: str,
    operator_reference: str,
    migration: dict[str, Any],
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Hash and grandfather one migration-eligible legacy roster path once."""

    candidate = discover_candidate(
        vendor,
        product_bin,
        allow_path_lookup=False,
    )
    return _approve_fingerprinted_candidate(
        candidate,
        expected=candidate.fingerprint_sha256,
        operator_reference=operator_reference,
        basis="grandfathered_roster",
        migration=migration,
        now=now,
    )


def require_approved_executable(
    *,
    vendor: str,
    product_bin: str,
    allow_path_lookup: bool = False,
) -> tuple[str, dict[str, Any]]:
    """Return an executable only when its full current fingerprint is approved."""

    normalized_vendor = _vendor(vendor)
    path, _source = _resolved_candidate_path(
        normalized_vendor,
        product_bin,
        allow_path_lookup=allow_path_lookup,
    )
    try:
        registry, descriptor, handle = _open_registry(
            exclusive=False,
            create=False,
        )
    except ProductApprovalError as error:
        if error.code.endswith("missing"):
            raise _approval_required(normalized_vendor, str(path)) from error
        raise
    try:
        canonical_path = str(path)
        cache_key = _approval_cache_key(
            registry,
            normalized_vendor,
            canonical_path,
        )
        with _VERIFIED_APPROVALS_LOCK:
            cached = _VERIFIED_APPROVALS.get(cache_key)
        if cached is not None:
            if cached.registry_identity != _registry_identity(descriptor):
                records, _ = _verify(handle)
                current = _active_records(records).get(
                    (normalized_vendor, canonical_path)
                )
                if current is None:
                    raise _approval_required(normalized_vendor, canonical_path)
                if (
                    current["record_id"] != cached.record_id
                    or current["record_sha256"] != cached.record_sha256
                    or current["attributes"]["fingerprint_sha256"]
                    != cached.fingerprint_sha256
                ):
                    raise ProductApprovalError(
                        "product_approval.attestation_stale",
                        "active approval changed after the executable was fully "
                        "verified; start a fresh operation",
                    )
                cached = _cache_verified_approval(
                    registry=registry,
                    descriptor=descriptor,
                    vendor=normalized_vendor,
                    canonical_path=canonical_path,
                    record=current,
                )
            current_metadata = _metadata_opened(path)
            if _metadata_tuple(current_metadata) != cached.executable_metadata:
                raise ProductApprovalError(
                    "product_approval.drift",
                    "product executable metadata changed after approval; fresh "
                    "approval is required",
                )
            return canonical_path, cached.summary()

        records, _ = _verify(handle)
        active = _active_records(records)
        record = active.get((normalized_vendor, canonical_path))
        if record is None:
            raise _approval_required(normalized_vendor, canonical_path)
        candidate = discover_candidate(
            normalized_vendor,
            canonical_path,
            allow_path_lookup=False,
        )
        if record["attributes"]["fingerprint_sha256"] != candidate.fingerprint_sha256:
            raise ProductApprovalError(
                "product_approval.drift",
                "product executable fingerprint changed after approval; run "
                "product-status, directly confirm a product-revoke using the active "
                "record ID and fingerprint guards, then rediscover and approve the "
                "replacement",
            )
        verified = _cache_verified_approval(
            registry=registry,
            descriptor=descriptor,
            vendor=candidate.vendor,
            canonical_path=candidate.canonical_path,
            record=record,
        )
        return canonical_path, verified.summary()
    finally:
        handle.close()
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def require_approved_metadata(
    *,
    vendor: str,
    product_bin: str,
) -> tuple[str, dict[str, Any]]:
    """Cheaply recheck an already content-approved executable before launch.

    A content write necessarily changes ctime on supported local filesystems.
    The complete SHA-256 remains bound in the verified approval record; this
    recheck compares every other bound field immediately before later launches
    in the same operation.
    """

    normalized_vendor = _vendor(vendor)
    path, _source = _resolved_candidate_path(
        normalized_vendor, product_bin, allow_path_lookup=False
    )
    registry, descriptor, handle = _open_registry(exclusive=False, create=False)
    try:
        canonical_path = str(path)
        cache_key = _approval_cache_key(
            registry,
            normalized_vendor,
            canonical_path,
        )
        with _VERIFIED_APPROVALS_LOCK:
            verified = _VERIFIED_APPROVALS.get(cache_key)
        if verified is None:
            raise ProductApprovalError(
                "product_approval.attestation_missing",
                "full executable approval verification is required before the "
                "cheap prelaunch metadata check",
            )
        if verified.registry_identity != _registry_identity(descriptor):
            records, _ = _verify(handle)
            record = _active_records(records).get((normalized_vendor, canonical_path))
            if record is None:
                raise ProductApprovalError(
                    "product_approval.required",
                    "product executable has no active account-scoped approval",
                )
            if (
                record["record_id"] != verified.record_id
                or record["record_sha256"] != verified.record_sha256
                or record["attributes"]["fingerprint_sha256"]
                != verified.fingerprint_sha256
            ):
                raise ProductApprovalError(
                    "product_approval.attestation_stale",
                    "active approval changed after the executable was fully verified",
                )
            verified = _cache_verified_approval(
                registry=registry,
                descriptor=descriptor,
                vendor=normalized_vendor,
                canonical_path=canonical_path,
                record=record,
            )
        current = _metadata_opened(path)
        if _metadata_tuple(current) != verified.executable_metadata:
            raise ProductApprovalError(
                "product_approval.drift",
                "product executable metadata changed after approval; fresh approval is required",
            )
        return canonical_path, verified.summary()
    finally:
        handle.close()
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def approval_status(
    *, vendor: str | None = None, product_bin: str | None = None
) -> dict[str, Any]:
    """Return verified active approvals without executing or requiring a product."""

    normalized_vendor = _vendor(vendor) if vendor is not None else None
    canonical_filter: str | None = None
    if product_bin is not None:
        canonical_filter = _canonical_cli_path(
            product_bin,
            label="status product executable filter",
        )
    try:
        registry, descriptor, handle = _open_registry(exclusive=False, create=False)
    except ProductApprovalError as error:
        if error.code.endswith("missing"):
            return {"ok": True, "status": "empty", "registry": None, "active": []}
        raise
    try:
        records, _ = _verify(handle)
        active = [
            record
            for (record_vendor, path), record in _active_records(records).items()
            if (normalized_vendor is None or record_vendor == normalized_vendor)
            and (canonical_filter is None or path == canonical_filter)
        ]
        return {
            "ok": True,
            "status": "verified",
            "registry": str(registry),
            "record_count": len(records),
            "active": sorted(
                active,
                key=lambda item: (
                    item["attributes"]["vendor"],
                    item["attributes"]["canonical_path"],
                ),
            ),
        }
    finally:
        handle.close()
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _recovery_api() -> _recovery.RecoveryLedgerApi:
    return _recovery.RecoveryLedgerApi(
        recovery_event=RECOVERY_EVENT,
        max_registry_bytes=MAX_REGISTRY_BYTES,
        max_record_bytes=MAX_RECORD_BYTES,
        max_registry_records=MAX_REGISTRY_RECORDS,
        open_registry=_open_registry,
        verify=_verify,
        active_records=_active_records,
        build_record=_build_record,
        operator_reference=_operator_reference,
        begin_operation=begin_operation,
    )


def approval_recovery_status() -> dict[str, Any]:
    """Inspect the ledger for one recoverable EOF fragment without mutation."""

    return _recovery.call_with_stable_errors(
        _recovery.approval_recovery_status, api=_recovery_api()
    )


def recover_partial_tail(**kwargs: Any) -> dict[str, Any]:
    """Archive and remove one operator-confirmed incomplete EOF fragment."""

    return _recovery.call_with_stable_errors(
        _recovery.recover_partial_tail, api=_recovery_api(), **kwargs
    )


def revoke_approval(
    *,
    vendor: str,
    product_bin: str,
    approval_record_id: str,
    expected_fingerprint_sha256: str,
    operator_reference: str,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Append a revocation for one exact currently active approval."""

    normalized_vendor = _vendor(vendor)
    reference = _operator_reference(operator_reference)
    expected = _sha256(expected_fingerprint_sha256, label="expected_fingerprint_sha256")
    try:
        expected_record_id = str(uuid.UUID(approval_record_id))
    except (ValueError, AttributeError):
        raise ProductApprovalError(
            "product_approval.record_id", "approval_record_id must be a UUID"
        ) from None
    canonical = _canonical_cli_path(
        product_bin,
        label="revoked product executable",
    )
    registry, descriptor, handle = _open_registry(exclusive=True, create=False)
    try:
        records, total = _verify(handle)
        active = _active_records(records)
        current = active.get((normalized_vendor, canonical))
        if current is None:
            raise ProductApprovalError(
                "product_approval.not_active",
                "product executable approval is not active",
            )
        if (
            current["record_id"] != expected_record_id
            or current["attributes"]["fingerprint_sha256"] != expected
        ):
            raise ProductApprovalError(
                "product_approval.revocation_target",
                "revocation guards do not match the active approval",
            )
        record = _append_locked(
            descriptor,
            records,
            total,
            event_type=REVOCATION_EVENT,
            attributes={
                "vendor": normalized_vendor,
                "canonical_path": canonical,
                "fingerprint_sha256": expected,
                "approval_record_id": expected_record_id,
                "operator_reference": reference,
            },
            now=now,
        )
        return {
            "ok": True,
            "status": "revoked",
            "registry": str(registry),
            "revocation": record,
        }
    finally:
        handle.close()
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
