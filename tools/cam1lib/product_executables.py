# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Safe discovery and fingerprinting for local product executables.

This module never executes a candidate.  It resolves a product path, opens the
canonical target without following a final symlink, and captures the stable
file identity used by the separate account approval ledger.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import stat
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ProjectError
from .secure_fs import _open_directory_fd

VENDORS = frozenset({"codex", "claude-code"})
PRODUCT_COMMANDS = {"codex": "codex", "claude-code": "claude"}
MAX_EXECUTABLE_BYTES = 1_073_741_824
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_EXECUTABLE_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_TRANSPORT_HELPER = Path(__file__).resolve().parents[1] / "cam1_transport.py"


class ProductApprovalError(ProjectError):
    """A product-executable candidate or approval ledger is unsafe."""


@dataclass(frozen=True, slots=True)
class ExecutableFingerprint:
    sha256: str
    size: int
    uid: int
    mode: int
    dev: int
    inode: int
    ctime_ns: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "sha256": self.sha256,
            "size": self.size,
            "uid": self.uid,
            "mode": self.mode,
            "dev": self.dev,
            "inode": self.inode,
            "ctime_ns": self.ctime_ns,
        }


@dataclass(frozen=True, slots=True)
class ExecutableCandidate:
    vendor: str
    canonical_path: str
    source: str
    fingerprint: ExecutableFingerprint
    fingerprint_sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "vendor": self.vendor,
            "canonical_path": self.canonical_path,
            "source": self.source,
            "fingerprint": self.fingerprint.as_dict(),
            "fingerprint_sha256": self.fingerprint_sha256,
        }


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
        raise ProductApprovalError(
            "product_approval.json_invalid",
            "product executable approval data must contain finite JSON values",
        ) from None


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _bounded_text(value: Any, *, label: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or not value.strip()
        or len(value) > maximum
        or any(
            unicodedata.category(character) in {"Cc", "Cf", "Cs", "Zl", "Zp"}
            for character in value
        )
    ):
        raise ProductApprovalError(
            "product_approval.field",
            f"{label} must be a bounded nonempty single-line string",
        )
    return value


def _vendor(value: Any) -> str:
    vendor = _bounded_text(value, label="vendor", maximum=32)
    if vendor not in VENDORS:
        raise ProductApprovalError(
            "product_approval.vendor",
            "product executable vendor is unsupported",
        )
    return vendor


def _sha256(value: Any, *, label: str) -> str:
    digest = _bounded_text(value, label=label, maximum=64)
    if _SHA256.fullmatch(digest) is None:
        raise ProductApprovalError(
            "product_approval.digest",
            f"{label} must be a lowercase SHA-256 digest",
        )
    return digest


def _candidate_digest(
    vendor: str,
    canonical_path: str,
    fingerprint: ExecutableFingerprint,
) -> str:
    return _digest(
        {
            "vendor": vendor,
            "canonical_path": canonical_path,
            "fingerprint": fingerprint.as_dict(),
        }
    )


def _resolved_candidate_path(
    vendor: str,
    supplied: str | None,
    *,
    allow_path_lookup: bool,
) -> tuple[Path, str]:
    command = PRODUCT_COMMANDS[vendor]
    value = (
        command
        if supplied is None
        else _bounded_text(supplied, label="product_bin", maximum=4_096)
    )
    raw = Path(value)
    if not raw.is_absolute():
        if not allow_path_lookup or os.path.sep in value:
            raise ProductApprovalError(
                "product_approval.absolute_path_required",
                "product executable must be an absolute path outside discovery",
            )
        located = shutil.which(value)
        if located is None:
            raise ProductApprovalError(
                "product_approval.not_found",
                "product executable was not found",
            )
        raw = Path(located)
        source = "path_candidate"
    else:
        source = "explicit_candidate"
    try:
        canonical = raw.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        raise ProductApprovalError(
            "product_approval.not_found",
            "product executable could not be resolved",
        ) from None
    canonical_text = _bounded_text(
        str(canonical),
        label="canonical product path",
        maximum=4_096,
    )
    canonical = Path(canonical_text)
    if not canonical.is_absolute():
        raise ProductApprovalError(
            "product_approval.absolute_path_required",
            "resolved product executable must be absolute",
        )
    return canonical, source


def discovery_command(vendor: str, canonical_path: str) -> tuple[str, ...]:
    """Return the exact no-execution recovery command for one resolved path."""

    return (
        sys.executable,
        str(_TRANSPORT_HELPER),
        "product-discover",
        "--vendor",
        _vendor(vendor),
        "--product-bin",
        _bounded_text(
            canonical_path,
            label="canonical product path",
            maximum=4_096,
        ),
    )


def _fingerprint_opened(path: Path) -> ExecutableFingerprint:
    try:
        parent_descriptor = _open_directory_fd(path.parent)
    except ProjectError as error:
        raise ProductApprovalError(error.code, error.detail) from error
    descriptor: int | None = None
    try:
        path_metadata = os.stat(
            path.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if not stat.S_ISREG(path_metadata.st_mode):
            raise ProductApprovalError(
                "product_approval.file_type",
                "product executable must be a regular file",
            )
        descriptor = os.open(path.name, _EXECUTABLE_FLAGS, dir_fd=parent_descriptor)
        before = os.fstat(descriptor)
        if (before.st_dev, before.st_ino) != (
            path_metadata.st_dev,
            path_metadata.st_ino,
        ):
            raise ProductApprovalError(
                "product_approval.changed",
                "product executable identity changed while it was opened",
            )
        if not stat.S_ISREG(before.st_mode):
            raise ProductApprovalError(
                "product_approval.file_type",
                "product executable must be a regular file",
            )
        if not (stat.S_IMODE(before.st_mode) & 0o111):
            raise ProductApprovalError(
                "product_approval.not_executable",
                "product executable has no execute bit",
            )
        if before.st_size < 0 or before.st_size > MAX_EXECUTABLE_BYTES:
            raise ProductApprovalError(
                "product_approval.size_limit",
                f"product executable exceeds {MAX_EXECUTABLE_BYTES} bytes",
            )
        digest = hashlib.sha256()
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1_048_576))
            if not chunk:
                raise ProductApprovalError(
                    "product_approval.changed",
                    "product executable changed while it was hashed",
                )
            digest.update(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise ProductApprovalError(
                "product_approval.changed",
                "product executable changed while it was hashed",
            )
        after = os.fstat(descriptor)
        stable_fields = (
            "st_dev",
            "st_ino",
            "st_size",
            "st_mode",
            "st_uid",
            "st_ctime_ns",
            "st_mtime_ns",
        )
        if any(
            getattr(before, field) != getattr(after, field) for field in stable_fields
        ):
            raise ProductApprovalError(
                "product_approval.changed",
                "product executable metadata changed while it was hashed",
            )
        return ExecutableFingerprint(
            sha256=digest.hexdigest(),
            size=after.st_size,
            uid=after.st_uid,
            mode=stat.S_IMODE(after.st_mode),
            dev=after.st_dev,
            inode=after.st_ino,
            ctime_ns=after.st_ctime_ns,
        )
    except ProductApprovalError:
        raise
    except OSError:
        raise ProductApprovalError(
            "product_approval.open",
            "product executable could not be inspected safely",
        ) from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent_descriptor)


def _metadata_opened(path: Path) -> dict[str, int]:
    """Read the approved non-content tuple through a no-follow descriptor."""

    try:
        parent_descriptor = _open_directory_fd(path.parent)
    except ProjectError as error:
        raise ProductApprovalError(error.code, error.detail) from error
    descriptor: int | None = None
    try:
        path_metadata = os.stat(
            path.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if not stat.S_ISREG(path_metadata.st_mode):
            raise ProductApprovalError(
                "product_approval.file_type",
                "product executable must be a regular file",
            )
        descriptor = os.open(path.name, _EXECUTABLE_FLAGS, dir_fd=parent_descriptor)
        opened = os.fstat(descriptor)
        if (path_metadata.st_dev, path_metadata.st_ino) != (
            opened.st_dev,
            opened.st_ino,
        ):
            raise ProductApprovalError(
                "product_approval.changed",
                "product executable identity changed while it was opened",
            )
        if not stat.S_ISREG(opened.st_mode):
            raise ProductApprovalError(
                "product_approval.file_type",
                "product executable must be a regular file",
            )
        if not (stat.S_IMODE(opened.st_mode) & 0o111):
            raise ProductApprovalError(
                "product_approval.not_executable",
                "product executable has no execute bit",
            )
        return {
            "size": opened.st_size,
            "uid": opened.st_uid,
            "mode": stat.S_IMODE(opened.st_mode),
            "dev": opened.st_dev,
            "inode": opened.st_ino,
            "ctime_ns": opened.st_ctime_ns,
        }
    except ProductApprovalError:
        raise
    except OSError:
        raise ProductApprovalError(
            "product_approval.open",
            "product executable could not be inspected safely",
        ) from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent_descriptor)


def discover_candidate(
    vendor: str,
    product_bin: str | None = None,
    *,
    allow_path_lookup: bool = True,
) -> ExecutableCandidate:
    """Inspect without executing and return a human-reviewable candidate."""

    normalized_vendor = _vendor(vendor)
    path, source = _resolved_candidate_path(
        normalized_vendor,
        product_bin,
        allow_path_lookup=allow_path_lookup,
    )
    fingerprint = _fingerprint_opened(path)
    return ExecutableCandidate(
        vendor=normalized_vendor,
        canonical_path=str(path),
        source=source,
        fingerprint=fingerprint,
        fingerprint_sha256=_candidate_digest(
            normalized_vendor,
            str(path),
            fingerprint,
        ),
    )


def resolve_candidate_path(
    vendor: str,
    product_bin: str | None = None,
    *,
    allow_path_lookup: bool = True,
) -> tuple[str, str]:
    """Resolve a candidate path without hashing, approving, or executing it."""

    normalized_vendor = _vendor(vendor)
    path, source = _resolved_candidate_path(
        normalized_vendor,
        product_bin,
        allow_path_lookup=allow_path_lookup,
    )
    return str(path), source


def candidate_card(candidate: ExecutableCandidate) -> dict[str, Any]:
    vendor = _vendor(candidate.vendor)
    canonical_path = _bounded_text(
        candidate.canonical_path,
        label="canonical product path",
        maximum=4_096,
    )
    if (
        not Path(canonical_path).is_absolute()
        or os.path.normpath(canonical_path) != canonical_path
    ):
        raise ProductApprovalError(
            "product_approval.path",
            "product executable candidate path must be normalized and absolute",
        )
    fingerprint = candidate.fingerprint
    _sha256(fingerprint.sha256, label="candidate executable sha256")
    expected_candidate_digest = _candidate_digest(
        vendor,
        canonical_path,
        fingerprint,
    )
    if not secrets.compare_digest(
        _sha256(candidate.fingerprint_sha256, label="candidate fingerprint_sha256"),
        expected_candidate_digest,
    ):
        raise ProductApprovalError(
            "product_approval.candidate_invalid",
            "product executable candidate fingerprint is internally inconsistent",
        )
    approval_arguments = (
        "product-approve",
        "--vendor",
        vendor,
        "--product-bin",
        canonical_path,
        "--expected-fingerprint-sha256",
        candidate.fingerprint_sha256,
        "--operator-reference",
        "DIRECT_OPERATOR_REFERENCE",
    )
    approval_command = (
        sys.executable,
        str(_TRANSPORT_HELPER),
        *approval_arguments,
    )
    return {
        "ok": True,
        "status": "approval_candidate",
        "candidate": candidate.as_dict(),
        "approval_arguments": list(approval_arguments),
        "approval_command": list(approval_command),
        "approval_command_text": shlex.join(approval_command),
        "next_step": (
            "Verify the displayed canonical target and fingerprint, then run "
            "product-approve with the returned approval_arguments after replacing "
            "DIRECT_OPERATOR_REFERENCE. Approval is normally once per unchanged "
            "executable fingerprint for this account, not once per project or session."
        ),
        "human_card": "\n".join(
            (
                "CAM/1 product executable candidate",
                f"Vendor: {vendor}",
                f"Canonical target: {canonical_path}",
                f"SHA-256: {fingerprint.sha256}",
                (
                    f"Size: {fingerprint.size} bytes; uid: {fingerprint.uid}; "
                    f"mode: {fingerprint.mode:04o}"
                ),
                (
                    "Device/inode/ctime: "
                    f"{fingerprint.dev}/{fingerprint.inode}/{fingerprint.ctime_ns}"
                ),
                f"Candidate fingerprint: {candidate.fingerprint_sha256}",
                (
                    "This card does not execute, authenticate, authorize, or "
                    "approve the program. The fingerprint covers this executable "
                    "file and path metadata, not dependencies or vendor trust."
                ),
            )
        ),
    }
