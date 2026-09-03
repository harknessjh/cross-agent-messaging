# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Append-only, owner-only CAM project journal with exact-byte preservation."""

from __future__ import annotations

import base64
import binascii
import copy
import datetime as dt
import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import subprocess
import uuid
from collections import deque
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any, TypeAlias, cast

from jsonschema import Draft202012Validator, FormatChecker

from .journal_recovery import (
    _create_recovery_archive,
    _inspect_partial_tail_locked,
    _replace_partial_journal,
)
from .journal_types import (
    MAX_JOURNAL_BYTES,
    JournalError,
    JournalVerification,
    PartialTailRecovery,
    PartialTailReport,
)
from .project import (
    REPOSITORY_ROOT,
    ProjectBinding,
    ProjectTransaction,
    _transaction_cache,
    current_project_transaction,
    project_transaction,
    require_project_transaction,
)
from .project_git import _git_environment, _git_probe_prefix
from .secure_fs import (
    _object_without_duplicates,
    _open_private_directory,
    _reject_constant,
    _validate_private_file,
    _write_all,
)

JOURNAL_SCHEMA_PATH = REPOSITORY_ROOT / "schemas" / "cam-journal-record-1.schema.json"
MAX_EXACT_MESSAGE_BYTES = 1_048_576
MAX_ATTRIBUTES_BYTES = 65_536
MAX_RECORD_BYTES = 1_500_000
MAX_JOURNAL_RECORDS = 100_000
MAX_ATTRIBUTE_NESTING = 12
MAX_TAIL_RECORDS = 100
MAX_GIT_PROVENANCE_BYTES = 1_048_576
_GIT_OBJECT_ID = re.compile(r"^[0-9a-f]{40,64}$")
_EVENT_TYPE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_JOURNAL_READ_FLAGS = (
    os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
)
_JOURNAL_APPEND_FLAGS = (
    os.O_RDWR
    | os.O_APPEND
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

JSONValue: TypeAlias = (
    str | int | float | bool | None | list["JSONValue"] | dict[str, "JSONValue"]
)


@dataclass(slots=True)
class _JournalTransactionCache:
    """Verified journal view scoped to one held project transaction."""

    records: list[dict[str, Any]]
    verification: JournalVerification
    device: int
    inode: int
    size: int
    modified_ns: int
    changed_ns: int


_JOURNAL_CACHE_KEY = "cam1.journal.verified"


def _load_schema() -> dict[str, Any]:
    with JOURNAL_SCHEMA_PATH.open("r", encoding="utf-8") as handle:
        schema = cast(dict[str, Any], json.load(handle))
    Draft202012Validator.check_schema(schema)
    return schema


_JOURNAL_VALIDATOR = Draft202012Validator(
    _load_schema(), format_checker=FormatChecker()
)


def _utc_text(value: dt.datetime | None = None) -> str:
    observed = value or dt.datetime.now(dt.UTC)
    if observed.tzinfo is None or observed.utcoffset() is None:
        raise JournalError("journal.timestamp", "timestamp must be timezone-aware")
    return (
        observed.astimezone(dt.UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, UnicodeEncodeError, ValueError):
        raise JournalError(
            "journal.json_invalid", "journal data must contain finite JSON values"
        ) from None


def _git_probe(
    project: ProjectBinding,
    *arguments: str,
    allowed_returncodes: tuple[int, ...] = (0,),
) -> tuple[int, bytes]:
    try:
        completed = subprocess.run(
            [
                *_git_probe_prefix(project.git_bin, project.git_top_level),
                *arguments,
            ],
            check=False,
            capture_output=True,
            env=_git_environment(),
            shell=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise JournalError(
            "journal.provenance",
            "Git provenance capture did not complete",
        ) from None
    if (
        completed.returncode not in allowed_returncodes
        or len(completed.stdout) > MAX_GIT_PROVENANCE_BYTES
    ):
        raise JournalError(
            "journal.provenance",
            "Git provenance capture failed",
        )
    return completed.returncode, completed.stdout


def _single_line(value: bytes, *, label: str, maximum: int) -> str:
    try:
        text = value.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError:
        raise JournalError(
            "journal.provenance",
            f"Git {label} was not valid UTF-8",
        ) from None
    if (
        not text
        or len(text) > maximum
        or any(character in text for character in "\r\n\x00")
    ):
        raise JournalError(
            "journal.provenance",
            f"Git {label} was malformed",
        )
    return text


def _git_provenance(
    project: ProjectBinding,
    *,
    captured_at: str,
) -> dict[str, Any]:
    _, status_raw = _git_probe(
        project,
        "status",
        "--porcelain=v2",
        "--branch",
        "--untracked-files=normal",
        "--ignore-submodules=all",
    )
    head_sha, branch, dirty = _parse_git_status_snapshot(status_raw)
    head_tree_sha: str | None = None
    if head_sha is not None:
        _, tree_raw = _git_probe(
            project,
            "rev-parse",
            "--verify",
            f"{head_sha}^{{tree}}",
        )
        head_tree_sha = _single_line(tree_raw, label="tree", maximum=64)
        if _GIT_OBJECT_ID.fullmatch(head_tree_sha) is None:
            raise JournalError("journal.provenance", "Git tree object ID was malformed")
    return {
        "git_top_level": str(project.git_top_level),
        "head_sha": head_sha,
        "head_tree_sha": head_tree_sha,
        "branch": branch,
        "dirty": dirty,
        "captured_at": captured_at,
    }


def _parse_git_status_snapshot(raw: bytes) -> tuple[str | None, str | None, bool]:
    """Parse the identity and dirty bit from one porcelain-v2 status snapshot."""

    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise JournalError(
            "journal.provenance", "Git status snapshot was not valid UTF-8"
        ) from None
    head_values: list[str] = []
    branch_values: list[str] = []
    dirty = False
    for line in text.splitlines():
        if line.startswith("# branch.oid "):
            head_values.append(line.removeprefix("# branch.oid "))
        elif line.startswith("# branch.head "):
            branch_values.append(line.removeprefix("# branch.head "))
        elif line.startswith("# "):
            continue
        elif line:
            dirty = True
    if len(head_values) != 1 or len(branch_values) != 1:
        raise JournalError(
            "journal.provenance", "Git status snapshot omitted branch identity"
        )
    head_value = head_values[0]
    if head_value == "(initial)":
        head_sha = None
    elif _GIT_OBJECT_ID.fullmatch(head_value):
        head_sha = head_value
    else:
        raise JournalError(
            "journal.provenance", "Git status snapshot contained a malformed HEAD"
        )
    branch_value = branch_values[0]
    if branch_value == "(detached)":
        branch = None
    elif (
        not branch_value
        or len(branch_value) > 512
        or any(character in branch_value for character in "\r\n\x00")
    ):
        raise JournalError(
            "journal.provenance", "Git status snapshot contained a malformed branch"
        )
    else:
        branch = branch_value
    return head_sha, branch, dirty


def _json_depth(value: Any, depth: int = 0) -> int:
    if depth > MAX_ATTRIBUTE_NESTING:
        return depth
    if isinstance(value, dict):
        return max(
            (_json_depth(item, depth + 1) for item in value.values()), default=depth
        )
    if isinstance(value, list):
        return max((_json_depth(item, depth + 1) for item in value), default=depth)
    return depth


def _require_json_value(value: Any, *, depth: int = 0) -> None:
    if depth > MAX_ATTRIBUTE_NESTING:
        raise JournalError(
            "journal.attributes_nesting", "journal attributes are nested too deeply"
        )
    if value is None or isinstance(value, (bool, int, str)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise JournalError(
                "journal.json_invalid", "journal attributes must use finite numbers"
            )
        return
    if isinstance(value, list):
        for item in value:
            _require_json_value(item, depth=depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise JournalError(
                    "journal.json_invalid", "journal attribute keys must be strings"
                )
            _require_json_value(item, depth=depth + 1)
        return
    raise JournalError(
        "journal.json_invalid", "journal attributes must contain only JSON values"
    )


def _normalized_attributes(
    attributes: Mapping[str, JSONValue] | None,
) -> dict[str, JSONValue]:
    value = dict(attributes or {})
    if len(value) > 64:
        raise JournalError(
            "journal.attributes_count", "journal attributes contain more than 64 keys"
        )
    _require_json_value(value)
    raw = _canonical_json(value)
    if len(raw) > MAX_ATTRIBUTES_BYTES:
        raise JournalError(
            "journal.attributes_size",
            f"journal attributes exceed {MAX_ATTRIBUTES_BYTES} bytes",
        )
    # Round-trip once so nested caller-owned dicts/lists cannot change between
    # digesting, serialization, and transaction-cache advancement.
    normalized = json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=_object_without_duplicates,
        parse_constant=_reject_constant,
    )
    return cast(dict[str, JSONValue], normalized)


def _encoded_message(exact_message: bytes | None) -> dict[str, Any] | None:
    if exact_message is None:
        return None
    if not isinstance(exact_message, bytes):
        raise JournalError("journal.message_type", "exact message must be bytes")
    if len(exact_message) > MAX_EXACT_MESSAGE_BYTES:
        raise JournalError(
            "journal.message_size",
            f"exact message exceeds {MAX_EXACT_MESSAGE_BYTES} bytes",
        )
    return {
        "encoding": "base64",
        "byte_length": len(exact_message),
        "sha256": hashlib.sha256(exact_message).hexdigest(),
        "content": base64.b64encode(exact_message).decode("ascii"),
    }


def _record_digest(record_without_digest: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(record_without_digest)).hexdigest()


def _serialized_record(record: Mapping[str, Any]) -> bytes:
    raw = _canonical_json(record) + b"\n"
    if len(raw) > MAX_RECORD_BYTES:
        raise JournalError(
            "journal.record_size", f"journal record exceeds {MAX_RECORD_BYTES} bytes"
        )
    return raw


def _validate_binding(project: ProjectBinding) -> None:
    if project.journal_path != project.project_dir / "journal.jsonl":
        raise JournalError(
            "journal.binding", "journal path does not match the project binding"
        )
    if project.project_dir.parent != project.state_root:
        raise JournalError(
            "journal.binding", "project directory is outside the bound journal root"
        )
    if not project.project_dir.name.endswith(f"--{project.project_id}"):
        raise JournalError(
            "journal.binding", "project directory does not match the project ID"
        )
    if project.transaction_lock_path != project.project_dir / "transaction.lock":
        raise JournalError(
            "journal.binding", "transaction lock does not match the project binding"
        )


def decode_exact_message(record: Mapping[str, Any]) -> bytes | None:
    """Decode and authenticate exact message bytes stored in one verified record."""

    message = record.get("message")
    if message is None:
        return None
    if not isinstance(message, dict) or message.get("encoding") != "base64":
        raise JournalError("journal.message_invalid", "message encoding is invalid")
    content = message.get("content")
    if not isinstance(content, str):
        raise JournalError("journal.message_invalid", "message content is invalid")
    try:
        decoded = base64.b64decode(content, validate=True)
    except (binascii.Error, ValueError):
        raise JournalError(
            "journal.message_invalid", "message base64 is invalid"
        ) from None
    if len(decoded) > MAX_EXACT_MESSAGE_BYTES:
        raise JournalError("journal.message_size", "stored message is too large")
    if len(decoded) != message.get("byte_length"):
        raise JournalError(
            "journal.message_length", "stored message byte length does not match"
        )
    digest = hashlib.sha256(decoded).hexdigest()
    claimed = message.get("sha256")
    if not isinstance(claimed, str) or not secrets.compare_digest(digest, claimed):
        raise JournalError(
            "journal.message_digest", "stored message digest does not match"
        )
    return decoded


def _parse_record(raw_line: bytes) -> dict[str, Any]:
    if not raw_line.endswith(b"\n"):
        raise JournalError(
            "journal.partial_record", "journal ends with a partial record"
        )
    try:
        record = json.loads(
            raw_line[:-1].decode("utf-8", errors="strict"),
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise JournalError(
            "journal.json_invalid", "journal record is not strict JSON"
        ) from None
    if not isinstance(record, dict):
        raise JournalError("journal.record_type", "journal record must be an object")
    if _json_depth(record) > MAX_ATTRIBUTE_NESTING + 3:
        raise JournalError(
            "journal.record_nesting", "journal record is nested too deeply"
        )
    if list(_JOURNAL_VALIDATOR.iter_errors(record)):
        raise JournalError(
            "journal.record_invalid", "journal record failed schema validation"
        )
    attributes = record.get("attributes")
    if not isinstance(attributes, dict):
        raise JournalError(
            "journal.record_invalid", "journal record attributes are invalid"
        )
    _normalized_attributes(attributes)
    if _serialized_record(record) != raw_line:
        raise JournalError(
            "journal.noncanonical", "journal record is not canonical JSONL"
        )
    unsigned = dict(record)
    claimed_digest = cast(str, unsigned.pop("record_sha256"))
    computed_digest = _record_digest(unsigned)
    if not secrets.compare_digest(claimed_digest, computed_digest):
        raise JournalError("journal.record_digest", "journal record digest is invalid")
    return record


def _verify_records(
    handle: Any,
    *,
    project_id: str,
    collect: bool = True,
    tail_limit: int | None = None,
    byte_limit: int | None = None,
) -> tuple[list[dict[str, Any]], JournalVerification]:
    records: list[dict[str, Any]] = []
    tail: deque[dict[str, Any]] | None = (
        deque(maxlen=tail_limit) if tail_limit is not None else None
    )
    previous_digest: str | None = None
    seen_ids: set[str] = set()
    total_bytes = 0
    record_count = 0
    remaining = byte_limit
    while True:
        if remaining == 0:
            break
        read_limit = MAX_RECORD_BYTES + 1
        if remaining is not None:
            read_limit = min(read_limit, remaining)
        raw_line = handle.readline(read_limit)
        if not raw_line:
            break
        if remaining is not None:
            remaining -= len(raw_line)
        total_bytes += len(raw_line)
        if len(raw_line) > MAX_RECORD_BYTES:
            raise JournalError(
                "journal.record_size",
                f"journal record exceeds {MAX_RECORD_BYTES} bytes",
            )
        if total_bytes > MAX_JOURNAL_BYTES:
            raise JournalError(
                "journal.size_limit", f"journal exceeds {MAX_JOURNAL_BYTES} bytes"
            )
        if record_count >= MAX_JOURNAL_RECORDS:
            raise JournalError(
                "journal.record_limit",
                f"journal contains more than {MAX_JOURNAL_RECORDS} records",
            )
        record = _parse_record(raw_line)
        sequence = record_count + 1
        if record["sequence"] != sequence:
            raise JournalError(
                "journal.sequence", "journal record sequence is not contiguous"
            )
        if record["project_id"] != project_id:
            raise JournalError(
                "journal.project_id", "journal record belongs to another project"
            )
        if record["record_id"] in seen_ids:
            raise JournalError("journal.record_id", "journal record ID is duplicated")
        if record["previous_record_sha256"] != previous_digest:
            raise JournalError("journal.chain", "journal hash chain is broken")
        claimed_digest = cast(str, record["record_sha256"])
        decode_exact_message(record)
        seen_ids.add(cast(str, record["record_id"]))
        previous_digest = claimed_digest
        record_count += 1
        if collect:
            records.append(record)
        elif tail is not None:
            tail.append(record)
    if remaining not in {None, 0}:
        raise JournalError(
            "journal.read_short", "journal changed while it was being verified"
        )
    verification = JournalVerification(
        project_id=project_id,
        record_count=record_count,
        last_sequence=record_count,
        last_record_sha256=previous_digest,
        total_bytes=total_bytes,
    )
    return (list(tail) if tail is not None else records), verification


def _open_locked_journal(
    project: ProjectBinding, *, exclusive: bool
) -> tuple[int, Any]:
    _validate_binding(project)
    parent_descriptor = _open_private_directory(
        project.project_dir, label="project.directory"
    )
    try:
        try:
            descriptor = os.open(
                project.journal_path.name,
                _JOURNAL_APPEND_FLAGS if exclusive else _JOURNAL_READ_FLAGS,
                dir_fd=parent_descriptor,
            )
        except OSError:
            raise JournalError("journal.open", "journal could not be opened") from None
        try:
            _validate_private_file(descriptor, label="journal.file")
            fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            os.lseek(descriptor, 0, os.SEEK_SET)
            handle = os.fdopen(os.dup(descriptor), "rb")
            return descriptor, handle
        except Exception:
            os.close(descriptor)
            raise
    finally:
        os.close(parent_descriptor)


def _require_cached_journal_identity(
    cached: _JournalTransactionCache, metadata: os.stat_result
) -> None:
    if (
        metadata.st_dev != cached.device
        or metadata.st_ino != cached.inode
        or metadata.st_size != cached.size
        or metadata.st_mtime_ns != cached.modified_ns
        or metadata.st_ctime_ns != cached.changed_ns
    ):
        raise JournalError(
            "journal.changed",
            "journal changed during the active project transaction",
        )


def _journal_cache_locked(
    project: ProjectBinding,
    transaction: ProjectTransaction,
    descriptor: int,
    handle: Any,
) -> _JournalTransactionCache:
    """Return one verified view while retaining per-operation journal locks."""

    caches = _transaction_cache(project, transaction)
    metadata = os.fstat(descriptor)
    cached = caches.get(_JOURNAL_CACHE_KEY)
    if cached is not None:
        if not isinstance(cached, _JournalTransactionCache):
            raise JournalError("journal.cache", "journal transaction cache is invalid")
        _require_cached_journal_identity(cached, metadata)
        return cached

    records, verification = _verify_records(handle, project_id=project.project_id)
    verified_metadata = os.fstat(descriptor)
    if (
        verified_metadata.st_dev != metadata.st_dev
        or verified_metadata.st_ino != metadata.st_ino
        or verified_metadata.st_size != verification.total_bytes
    ):
        raise JournalError(
            "journal.changed",
            "journal changed while the transaction cache was initialized",
        )
    cached = _JournalTransactionCache(
        records=records,
        verification=verification,
        device=verified_metadata.st_dev,
        inode=verified_metadata.st_ino,
        size=verified_metadata.st_size,
        modified_ns=verified_metadata.st_mtime_ns,
        changed_ns=verified_metadata.st_ctime_ns,
    )
    caches[_JOURNAL_CACHE_KEY] = cached
    return cached


def _verified_records_for_transaction(
    project: ProjectBinding, transaction: ProjectTransaction
) -> list[dict[str, Any]]:
    """Return the internal verified record view for state projection replay."""

    require_project_transaction(project, transaction)
    descriptor, handle = _open_locked_journal(project, exclusive=False)
    try:
        cached = _journal_cache_locked(project, transaction, descriptor, handle)
        return cached.records
    finally:
        handle.close()
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def verify_journal(project: ProjectBinding) -> JournalVerification:
    """Verify the complete bounded journal without modifying it."""

    transaction = current_project_transaction(project)
    if transaction is not None:
        descriptor, handle = _open_locked_journal(project, exclusive=False)
        try:
            return _journal_cache_locked(
                project, transaction, descriptor, handle
            ).verification
        finally:
            handle.close()
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    descriptor, handle = _open_locked_journal(project, exclusive=False)
    try:
        _, verification = _verify_records(
            handle, project_id=project.project_id, collect=False
        )
        return verification
    finally:
        handle.close()
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def inspect_partial_tail(project: ProjectBinding) -> PartialTailReport:
    """Describe a recoverable incomplete EOF record without modifying history."""

    descriptor, handle = _open_locked_journal(project, exclusive=False)
    try:
        return _inspect_partial_tail_locked(
            descriptor,
            handle,
            project_id=project.project_id,
            verify_records=_verify_records,
        )
    finally:
        handle.close()
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def replay_records(
    project: ProjectBinding,
    *,
    event_types: Collection[str] | None = None,
    record_ids: Collection[str] | None = None,
) -> tuple[dict[str, Any], ...]:
    """Return verified journal records selected before caller-owned copies are made."""

    requested = set(event_types) if event_types is not None else None
    if requested is not None and any(
        not _EVENT_TYPE.fullmatch(item) for item in requested
    ):
        raise JournalError("journal.event_type", "event type filter is invalid")
    requested_ids = set(record_ids) if record_ids is not None else None
    if requested_ids is not None:
        for record_id in requested_ids:
            try:
                canonical = str(uuid.UUID(record_id))
            except (AttributeError, TypeError, ValueError) as error:
                raise JournalError(
                    "journal.record_id", "record ID filter is invalid"
                ) from error
            if canonical != record_id:
                raise JournalError("journal.record_id", "record ID filter is invalid")
    transaction = current_project_transaction(project)
    if transaction is not None:
        records = _verified_records_for_transaction(project, transaction)
    else:
        descriptor, handle = _open_locked_journal(project, exclusive=False)
        try:
            records, _ = _verify_records(handle, project_id=project.project_id)
        finally:
            handle.close()
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
    selected = [
        record
        for record in records
        if (requested is None or record["event_type"] in requested)
        and (requested_ids is None or record["record_id"] in requested_ids)
    ]
    return tuple(copy.deepcopy(record) for record in selected)


def append_record(
    project: ProjectBinding,
    *,
    event_type: str,
    exact_message: bytes | None = None,
    attributes: Mapping[str, JSONValue] | None = None,
    now: dt.datetime | None = None,
    transaction: ProjectTransaction | None = None,
) -> dict[str, Any]:
    """Append after verifying the chain once per active project transaction."""

    if not isinstance(event_type, str) or not _EVENT_TYPE.fullmatch(event_type):
        raise JournalError("journal.event_type", "event type is invalid")
    normalized_attributes = _normalized_attributes(attributes)
    encoded_message = _encoded_message(exact_message)
    if transaction is None:
        transaction = current_project_transaction(project)
    if transaction is None:
        with project_transaction(project) as acquired:
            return _append_record_locked(
                project,
                event_type=event_type,
                encoded_message=encoded_message,
                normalized_attributes=normalized_attributes,
                now=now,
                transaction=acquired,
            )
    return _append_record_locked(
        project,
        event_type=event_type,
        encoded_message=encoded_message,
        normalized_attributes=normalized_attributes,
        now=now,
        transaction=transaction,
    )


def _append_record_locked(
    project: ProjectBinding,
    *,
    event_type: str,
    encoded_message: dict[str, Any] | None,
    normalized_attributes: dict[str, JSONValue],
    now: dt.datetime | None,
    transaction: ProjectTransaction,
) -> dict[str, Any]:
    require_project_transaction(project, transaction)
    descriptor, handle = _open_locked_journal(project, exclusive=True)
    try:
        cached = _journal_cache_locked(project, transaction, descriptor, handle)
        verification = cached.verification
        if verification.record_count >= MAX_JOURNAL_RECORDS:
            raise JournalError(
                "journal.record_limit",
                f"journal contains {MAX_JOURNAL_RECORDS} records",
            )
        recorded_at = _utc_text(now)
        unsigned: dict[str, Any] = {
            "format": "CAM-JOURNAL/1",
            "sequence": verification.last_sequence + 1,
            "record_id": str(uuid.uuid4()),
            "project_id": project.project_id,
            "worktree_id": project.worktree_id,
            "recorded_at": recorded_at,
            "provenance": _git_provenance(project, captured_at=recorded_at),
            "event_type": event_type,
            "previous_record_sha256": verification.last_record_sha256,
            "message": encoded_message,
            "attributes": normalized_attributes,
        }
        record = {**unsigned, "record_sha256": _record_digest(unsigned)}
        raw = _serialized_record(record)
        verified_record = _parse_record(raw)
        if verification.total_bytes + len(raw) > MAX_JOURNAL_BYTES:
            raise JournalError(
                "journal.size_limit", f"journal exceeds {MAX_JOURNAL_BYTES} bytes"
            )
        os.lseek(descriptor, 0, os.SEEK_END)
        _write_all(descriptor, raw)
        os.fsync(descriptor)
        metadata = os.fstat(descriptor)
        expected_size = verification.total_bytes + len(raw)
        if (
            metadata.st_dev != cached.device
            or metadata.st_ino != cached.inode
            or metadata.st_size != expected_size
        ):
            raise JournalError(
                "journal.changed",
                "journal identity or size changed during append",
            )
        cached.records.append(verified_record)
        cached.verification = JournalVerification(
            project_id=project.project_id,
            record_count=verification.record_count + 1,
            last_sequence=cast(int, verified_record["sequence"]),
            last_record_sha256=cast(str, verified_record["record_sha256"]),
            total_bytes=expected_size,
        )
        cached.size = metadata.st_size
        cached.modified_ns = metadata.st_mtime_ns
        cached.changed_ns = metadata.st_ctime_ns
        return copy.deepcopy(verified_record)
    finally:
        handle.close()
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _recovery_text(value: str, *, label: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or any(character in value for character in "\x00\r\n")
    ):
        raise JournalError(
            f"journal.recovery_{label}",
            f"recovery {label.replace('_', ' ')} must be a nonempty single line",
        )
    return value


def recover_partial_tail(
    project: ProjectBinding,
    *,
    expected_journal_sha256: str,
    confirm_project_id: str,
    reason: str,
    operator_reference: str,
    now: dt.datetime | None = None,
    transaction: ProjectTransaction | None = None,
) -> PartialTailRecovery:
    """Archive an incomplete EOF record and append an explicit recovery event."""

    if not isinstance(expected_journal_sha256, str) or not _SHA256.fullmatch(
        expected_journal_sha256
    ):
        raise JournalError(
            "journal.recovery_digest", "expected journal SHA-256 is invalid"
        )
    if confirm_project_id != project.project_id:
        raise JournalError(
            "journal.recovery_project",
            "confirmed project ID does not match the bound project",
        )
    normalized_reason = _recovery_text(reason, label="reason", maximum=500)
    normalized_reference = _recovery_text(
        operator_reference, label="operator_reference", maximum=1000
    )
    if transaction is None:
        transaction = current_project_transaction(project)
    if transaction is None:
        with project_transaction(project) as acquired:
            return recover_partial_tail(
                project,
                expected_journal_sha256=expected_journal_sha256,
                confirm_project_id=confirm_project_id,
                reason=normalized_reason,
                operator_reference=normalized_reference,
                now=now,
                transaction=acquired,
            )
    require_project_transaction(project, transaction)
    descriptor, handle = _open_locked_journal(project, exclusive=True)
    archive_path: str | None = None
    recovery_record: dict[str, Any] | None = None
    try:
        source_metadata = os.fstat(descriptor)
        report = _inspect_partial_tail_locked(
            descriptor,
            handle,
            project_id=project.project_id,
            verify_records=_verify_records,
        )
        if report.prefix_verification.record_count >= MAX_JOURNAL_RECORDS:
            raise JournalError(
                "journal.record_limit",
                "verified prefix cannot accommodate a recovery record",
            )
        if not secrets.compare_digest(report.journal_sha256, expected_journal_sha256):
            raise JournalError(
                "journal.recovery_digest_mismatch",
                "journal SHA-256 changed or does not match the operator-confirmed value",
            )
        recorded_at = _utc_text(now)
        provenance = _git_provenance(project, captured_at=recorded_at)
        _archive_name, archive_path = _create_recovery_archive(
            project,
            source_descriptor=descriptor,
            report=report,
        )
        unsigned: dict[str, Any] = {
            "format": "CAM-JOURNAL/1",
            "sequence": report.prefix_verification.last_sequence + 1,
            "record_id": str(uuid.uuid4()),
            "project_id": project.project_id,
            "worktree_id": project.worktree_id,
            "recorded_at": recorded_at,
            "provenance": provenance,
            "event_type": "journal.recovered_partial_tail",
            "previous_record_sha256": report.prefix_verification.last_record_sha256,
            "message": None,
            "attributes": {
                "archive_file": archive_path,
                "archive_sha256": report.journal_sha256,
                "archive_byte_length": report.journal_bytes,
                "verified_prefix_byte_length": report.verified_prefix_bytes,
                "verified_prefix_sha256": report.verified_prefix_sha256,
                "verified_prefix_last_record_sha256": report.prefix_verification.last_record_sha256,
                "partial_tail_byte_length": report.partial_tail_bytes,
                "partial_tail_sha256": report.partial_tail_sha256,
                "reason": normalized_reason,
                "operator_reference": normalized_reference,
            },
        }
        generated_record = {**unsigned, "record_sha256": _record_digest(unsigned)}
        recovery_record_raw = _serialized_record(generated_record)
        recovery_record = _parse_record(recovery_record_raw)
        _replace_partial_journal(
            project,
            source_descriptor=descriptor,
            source_metadata=source_metadata,
            report=report,
            recovery_record_raw=recovery_record_raw,
        )
        # Atomic replacement changes the journal inode and canonical history.
        # Discard every projection derived from the old descriptor; the
        # verification below reseeds the journal view from the installed file.
        _transaction_cache(project, transaction).clear()
    finally:
        handle.close()
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
    if archive_path is None or recovery_record is None:
        raise JournalError("journal.recovery_failed", "journal recovery did not finish")
    verification = verify_journal(project)
    return PartialTailRecovery(
        archive_path=str(project.project_dir / archive_path),
        original_sha256=expected_journal_sha256,
        original_bytes=report.journal_bytes,
        recovered_record=recovery_record,
        verification=verification,
    )


def _redacted_record(record: Mapping[str, Any]) -> dict[str, Any]:
    redacted = dict(record)
    message = record.get("message")
    if isinstance(message, dict):
        redacted["message"] = {
            "encoding": message.get("encoding"),
            "byte_length": message.get("byte_length"),
            "sha256": message.get("sha256"),
            "content": "<redacted>",
        }
    attribute_bytes = _canonical_json(record.get("attributes", {}))
    redacted["attributes"] = {
        "redacted": True,
        "byte_length": len(attribute_bytes),
        "sha256": hashlib.sha256(attribute_bytes).hexdigest(),
    }
    return redacted


def tail_records(
    project: ProjectBinding, *, limit: int = 20, redact: bool = True
) -> tuple[dict[str, Any], ...]:
    """Return a bounded tail after verifying the full journal."""

    if type(limit) is not int or limit < 1 or limit > MAX_TAIL_RECORDS:
        raise JournalError(
            "journal.tail_limit",
            f"tail limit must be between 1 and {MAX_TAIL_RECORDS}",
        )
    transaction = current_project_transaction(project)
    if transaction is not None:
        records = list(_verified_records_for_transaction(project, transaction)[-limit:])
    else:
        descriptor, handle = _open_locked_journal(project, exclusive=False)
        try:
            records, _ = _verify_records(
                handle,
                project_id=project.project_id,
                collect=False,
                tail_limit=limit,
            )
        finally:
            handle.close()
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
    if redact:
        return tuple(_redacted_record(record) for record in records)
    return tuple(copy.deepcopy(record) for record in records)
