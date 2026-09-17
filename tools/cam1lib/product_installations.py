# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Explicit local installation trust, separate from strict file approvals.

We trust the operator-selected installation and updater. Fingerprints below are
observations for one operation, never fabricated approvals of future releases.
This module does not execute, install, search for, or update a product.
"""

from __future__ import annotations

import copy
import fcntl
import json
import os
import shlex
import stat
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from tools._cam1_executable import ExecutablePolicyError, inspect_installation_directory

from . import product_approvals as ledger
from .product_executables import (
    ExecutableCandidate,
    ProductApprovalError,
    _bounded_text,
    _digest,
    _metadata_opened,
    _sha256,
    _vendor,
    discover_candidate,
)

REGISTRY_NAME = "product-installations-v1.jsonl"
REGISTRY_FORMAT = "CAM-PRODUCT-INSTALLATION-APPROVAL/1"
APPROVED = "product_installation.approved"
REVOKED = "product_installation.revoked"
STATUS_COMMAND = "product-installation-status"
_SCHEMA = (
    Path(__file__).resolve().parents[2]
    / "schemas/cam-product-installation-approval-1.schema.json"
)
with _SCHEMA.open(encoding="utf-8") as _handle:
    _VALIDATOR = Draft202012Validator(
        json.load(_handle), format_checker=FormatChecker()
    )


def _absolute(value: str) -> str:
    text = _bounded_text(value, label="installation path", maximum=4096)
    if not Path(text).is_absolute() or os.path.normpath(text) != text:
        raise ProductApprovalError(
            "installation.path", "use a normalized absolute installation path"
        )
    return text


def _directory(
    path: Path, *, include_filesystem_identity: bool = False
) -> dict[str, Any]:
    try:
        metadata, identity = inspect_installation_directory(
            path, include_filesystem_identity=include_filesystem_identity
        )
    except (ExecutablePolicyError, OSError) as error:
        raise ProductApprovalError("installation.directory", str(error)) from error
    observed = {
        "dev": metadata.st_dev,
        "inode": metadata.st_ino,
        "uid": metadata.st_uid,
    }
    if include_filesystem_identity:
        return {**observed, "filesystem": identity}
    return observed


def _root_identity(observed: dict[str, Any]) -> dict[str, Any]:
    return {key: observed[key] for key in ("filesystem", "inode", "uid")}


def _target(launcher: str) -> str:
    """Resolve a bounded link chain, checking even intermediate alias parents."""

    pending = list(Path(_absolute(launcher)).parts[1:])
    current = Path("/")
    links = 0
    try:
        _directory(current)
        while pending:
            part = pending.pop(0)
            if part == "..":
                current = current.parent
                continue
            if part == ".":
                continue
            candidate = current / part
            metadata = candidate.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                links += 1
                if links > 40 or metadata.st_uid not in (0, os.geteuid()):
                    raise ProductApprovalError(
                        "installation.link", "unsafe or looping launcher link"
                    )
                target = Path(os.readlink(candidate))
                if target.is_absolute():
                    current = Path("/")
                    pending = list(target.parts[1:]) + pending
                else:
                    pending = list(target.parts) + pending
            else:
                current = candidate
                if pending:
                    _directory(current)
        return str(current)
    except OSError as error:
        raise ProductApprovalError(
            "installation.unavailable", "installation launcher is unavailable"
        ) from error


def _selection(vendor: str, launcher: str, root: str) -> dict[str, Any]:
    normalized = _vendor(vendor)
    launcher = _absolute(launcher)
    root = _absolute(root)
    root_path = Path(root)
    home = ledger.account_home()
    if root_path == Path("/") or root_path == home or root_path in home.parents:
        raise ProductApprovalError(
            "installation.root",
            "select a bounded product installation, not an account or system root",
        )
    return {
        "vendor": normalized,
        "launcher": launcher,
        "root": root,
        "root_identity": _root_identity(
            _directory(root_path, include_filesystem_identity=True)
        ),
    }


def _inspect(
    selection: dict[str, Any], *, fingerprint: bool
) -> tuple[ExecutableCandidate | dict[str, int], int]:
    if ledger.has_approval_history(
        vendor=selection["vendor"], canonical_path=selection["launcher"]
    ):
        raise ProductApprovalError(
            "installation.strict_path_conflict",
            "launcher path has strict-file approval history; select a different "
            "stable launcher and explicitly update each participant choosing it",
        )
    root = Path(selection["root"])
    observed_root = _directory(root, include_filesystem_identity=True)
    if _root_identity(observed_root) != selection["root_identity"]:
        raise ProductApprovalError(
            "installation.root_changed",
            "installation root identity changed; review a new installation card",
        )
    target = _target(selection["launcher"])
    if not Path(target).is_relative_to(root) or Path(target) == root:
        raise ProductApprovalError(
            "installation.escape",
            "launcher target is outside the approved installation root",
        )
    if fingerprint:
        candidate = discover_candidate(
            selection["vendor"], target, allow_path_lookup=False
        )
        if candidate.canonical_path != target:
            raise ProductApprovalError(
                "installation.drift", "launcher target changed during inspection"
            )
        return candidate, observed_root["dev"]
    return _metadata_opened(Path(target)), observed_root["dev"]


def discover(*, vendor: str, launcher: str, installation_root: str) -> dict[str, Any]:
    selection = _selection(vendor, launcher, installation_root)
    candidate, root_device = _inspect(selection, fingerprint=True)
    assert isinstance(candidate, ExecutableCandidate)
    if candidate.canonical_path == launcher:
        raise ProductApprovalError(
            "installation.stable_launcher",
            "installation mode requires a stable symlink launcher distinct from its native target; use strict approval for a direct binary path",
        )
    card = {
        "selection": selection,
        "candidate": candidate.as_dict(),
        "root_device": root_device,
    }
    command = [
        sys.executable,
        str(Path(__file__).resolve().parents[1] / "cam1_transport.py"),
        "product-installation-approve",
        "--vendor",
        vendor,
        "--launcher",
        launcher,
        "--installation-root",
        installation_root,
        "--expected-card-sha256",
        _digest(card),
        "--operator-reference",
        "DIRECT_OPERATOR_REFERENCE",
    ]
    return {
        "ok": True,
        "status": "installation_candidate",
        **card,
        "card_sha256": _digest(card),
        "policy_sha256": _digest(selection),
        "approval_command": command,
        "approval_command_text": shlex.join(command),
        "trust": "Trust this installation and its updater, including future native releases selected by this launcher inside this root. CAM does not authenticate publishers or detect malicious updates.",
        "next_step": "Review this entire card directly with the operator before product-installation-approve. Existing participants are not migrated.",
    }


def _parse_record(raw: bytes) -> dict[str, Any]:
    record = ledger._decode_record(raw, _VALIDATOR)
    attributes = record["attributes"]
    ledger._operator_reference(attributes["operator_reference"])
    if record["event_type"] == APPROVED:
        selection = attributes.get("selection")
        if selection is None:
            raise ProductApprovalError(
                "installation.record", "approval requires a selection"
            )
        for key in ("launcher", "root"):
            _absolute(selection[key])
        _absolute(attributes["initial_path"])
        if _digest(selection) != attributes["policy_sha256"]:
            raise ProductApprovalError(
                "installation.digest", "installation policy digest does not match"
            )
    else:
        if "approval_record_id" not in attributes:
            raise ProductApprovalError(
                "installation.record", "revocation requires an approval ID"
            )
        _absolute(attributes["launcher"])
        ledger._canonical_uuid(
            attributes["approval_record_id"], label="approval_record_id"
        )
    return record


def _active(records: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for record in records:
        attributes = record["attributes"]
        selection = attributes.get("selection", attributes)
        key = (selection["vendor"], selection["launcher"])
        if record["event_type"] == APPROVED:
            if key in result:
                raise ProductApprovalError(
                    "installation.overlap", "overlapping installation approvals"
                )
            result[key] = record
        else:
            previous = result.get(key)
            if previous is None or (
                previous["record_id"],
                previous["attributes"]["policy_sha256"],
            ) != (attributes["approval_record_id"], attributes["policy_sha256"]):
                raise ProductApprovalError(
                    "installation.revocation_target",
                    "installation revocation does not match an active approval",
                )
            del result[key]
    return result


def status() -> dict[str, Any]:
    try:
        registry, descriptor, handle = ledger._open_registry(
            exclusive=False, create=False, registry_name=REGISTRY_NAME
        )
    except ProductApprovalError as error:
        if error.code.endswith("missing"):
            return {
                "ok": True,
                "status": "empty",
                "registry": None,
                "active": [],
                "revoked": [],
            }
        raise
    try:
        records, _ = ledger._verify(handle, parse_record=_parse_record)
        active = _active(records)
        revoked = [
            item["attributes"]
            for item in records
            if item["event_type"] == REVOKED
            and (item["attributes"]["vendor"], item["attributes"]["launcher"])
            not in active
        ]
        return {
            "ok": True,
            "status": "verified",
            "registry": str(registry),
            "record_count": len(records),
            "active": list(active.values()),
            "revoked": revoked,
        }
    finally:
        handle.close()
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _mutate(
    *, attributes: dict[str, Any], event_type: str, expected_record: str | None = None
) -> dict[str, Any]:
    registry, descriptor, handle = ledger._open_registry(
        exclusive=True, create=event_type == APPROVED, registry_name=REGISTRY_NAME
    )
    appended = None
    failure = None
    try:
        records, total = ledger._verify(handle, parse_record=_parse_record)
        selection = attributes.get("selection", attributes)
        current = _active(records).get((selection["vendor"], selection["launcher"]))
        if event_type == APPROVED:
            if current is not None:
                if (
                    current["attributes"]["policy_sha256"]
                    != attributes["policy_sha256"]
                ):
                    raise ProductApprovalError(
                        "installation.overlap",
                        "revoke the existing installation approval explicitly before changing its selection",
                    )
                return {
                    "ok": True,
                    "status": "already_approved",
                    "approval": current,
                    "registry": str(registry),
                }
            # The reviewed card must still identify the same executable at append.
            candidate, root_device = _inspect(selection, fingerprint=True)
            assert isinstance(candidate, ExecutableCandidate)
            if (
                candidate.fingerprint_sha256 != attributes["initial_fingerprint_sha256"]
                or root_device != attributes["initial_root_device"]
            ):
                raise ProductApprovalError(
                    "installation.card_stale",
                    "installation candidate changed before approval",
                )
        elif (
            current is None
            or current["record_id"] != expected_record
            or current["attributes"]["policy_sha256"] != attributes["policy_sha256"]
        ):
            raise ProductApprovalError(
                "installation.revocation_target",
                "revocation guards do not match the active installation approval",
            )
        appended = ledger._append_locked(
            descriptor,
            records,
            total,
            event_type=event_type,
            attributes=attributes,
            now=None,
            registry_format=REGISTRY_FORMAT,
            parse_record=_parse_record,
        )
        return {
            "ok": True,
            "status": "approved" if event_type == APPROVED else "revoked",
            "registry": str(registry),
            "record": appended,
        }
    except BaseException as error:
        failure = error
        raise
    finally:
        begin_operation()
        ledger._finish_registry_mutation(
            registry,
            descriptor,
            handle,
            appended,
            failure,
            reconciliation_command=STATUS_COMMAND,
        )


def approve(
    *,
    vendor: str,
    launcher: str,
    installation_root: str,
    expected_card_sha256: str,
    operator_reference: str,
) -> dict[str, Any]:
    reference = ledger._operator_reference(operator_reference)
    expected = _sha256(expected_card_sha256, label="expected_card_sha256")
    card = discover(
        vendor=vendor, launcher=launcher, installation_root=installation_root
    )
    if expected != card["card_sha256"]:
        raise ProductApprovalError(
            "installation.card_stale", "installation card changed; review a fresh card"
        )
    return _mutate(
        event_type=APPROVED,
        attributes={
            "selection": card["selection"],
            "policy_sha256": card["policy_sha256"],
            "initial_path": card["candidate"]["canonical_path"],
            "initial_fingerprint_sha256": card["candidate"]["fingerprint_sha256"],
            "initial_root_device": card["root_device"],
            "operator_reference": reference,
        },
    )


def revoke(
    *,
    vendor: str,
    launcher: str,
    approval_record_id: str,
    expected_policy_sha256: str,
    operator_reference: str,
) -> dict[str, Any]:
    return _mutate(
        event_type=REVOKED,
        expected_record=ledger._canonical_uuid(
            approval_record_id, label="approval_record_id"
        ),
        attributes={
            "vendor": _vendor(vendor),
            "launcher": _absolute(launcher),
            "approval_record_id": approval_record_id,
            "policy_sha256": _sha256(
                expected_policy_sha256, label="expected_policy_sha256"
            ),
            "operator_reference": ledger._operator_reference(operator_reference),
        },
    )


@dataclass(frozen=True)
class _VerifiedInstallation:
    record: dict[str, Any]
    candidate: ExecutableCandidate
    registry: str
    root_device: int

    def summary(self) -> dict[str, Any]:
        return {
            "basis": "operator_selected_installation",
            "registry": self.registry,
            "record_id": self.record["record_id"],
            "record_sha256": self.record["record_sha256"],
            "policy_sha256": self.record["attributes"]["policy_sha256"],
            "selection": copy.deepcopy(self.record["attributes"]["selection"]),
            "root_device": self.root_device,
            "vendor": self.candidate.vendor,
            "canonical_path": self.candidate.canonical_path,
            "fingerprint_sha256": self.candidate.fingerprint_sha256,
            "fingerprint": self.candidate.fingerprint.as_dict(),
            "fingerprint_is_release_approval": False,
        }


_VERIFIED: dict[tuple[str, str], _VerifiedInstallation] = {}
_VERIFIED_LOCK = threading.RLock()


def begin_operation() -> None:
    with _VERIFIED_LOCK:
        _VERIFIED.clear()


def _cached_installations() -> tuple[_VerifiedInstallation, ...]:
    with _VERIFIED_LOCK:
        return tuple(_VERIFIED.values())


def resolve(
    *, vendor: str, product_bin: str, prelaunch: bool = False
) -> tuple[str, dict[str, Any]] | None:
    """Use only an explicitly selected launcher or its operation-local target.

    Return None for strict paths. Never search an installation for another file
    and never reinterpret a strict fingerprint approval as installation trust.
    """

    if not Path(product_bin).is_absolute():
        return None
    normalized_vendor = _vendor(vendor)
    # Match installation selections without changing strict-mode path handling.
    # A matching installation still requires its exact normalized spelling.
    selected = os.path.normpath(
        _bounded_text(product_bin, label="product_bin", maximum=4096)
    )
    cached = next(
        (
            value
            for value in _cached_installations()
            if value.candidate.vendor == normalized_vendor
            and selected
            in (
                value.candidate.canonical_path,
                value.record["attributes"]["selection"]["launcher"],
            )
        ),
        None,
    )
    # With no installation ledger, strict callers retain their original path.
    ledger_status = status()
    key_path = (
        cached.record["attributes"]["selection"]["launcher"] if cached else selected
    )
    record = next(
        (
            item
            for item in ledger_status["active"]
            if (
                item["attributes"]["selection"]["vendor"],
                item["attributes"]["selection"]["launcher"],
            )
            == (normalized_vendor, key_path)
        ),
        None,
    )
    revoked = any(
        (item["vendor"], item["launcher"]) == (normalized_vendor, selected)
        for item in ledger_status["revoked"]
    )
    if cached or record is not None or revoked:
        _absolute(product_bin)
    if cached:
        if record is None or record["record_sha256"] != cached.record["record_sha256"]:
            raise ProductApprovalError(
                "installation.approval_changed",
                "installation approval changed during this operation",
            )
        selection = cached.record["attributes"]["selection"]
        current, root_device = _inspect(selection, fingerprint=False)
        if (
            root_device != cached.root_device
            or _target(selection["launcher"]) != cached.candidate.canonical_path
            or ledger._metadata_tuple(current)
            != ledger._metadata_tuple(cached.candidate.fingerprint.as_dict())
        ):
            raise ProductApprovalError(
                "installation.drift",
                "installation changed during the operation; stop without retry",
            )
        return cached.candidate.canonical_path, cached.summary()
    if record is None:
        if revoked:
            raise ProductApprovalError(
                "installation.revoked",
                "this installation approval was revoked; no strict fallback was attempted",
            )
        return None
    if prelaunch:
        raise ProductApprovalError(
            "installation.attestation_missing",
            "inspect the installation before its prelaunch check",
        )
    candidate, root_device = _inspect(
        record["attributes"]["selection"], fingerprint=True
    )
    assert isinstance(candidate, ExecutableCandidate)
    verified = _VerifiedInstallation(
        record, candidate, ledger_status["registry"], root_device
    )
    with _VERIFIED_LOCK:
        _VERIFIED[(normalized_vendor, key_path)] = verified
    return candidate.canonical_path, verified.summary()


def selected_path(vendor: str, canonical_path: str) -> str:
    """Return the stable selection only when this operation already verified it."""

    for value in _cached_installations():
        if (value.candidate.vendor, value.candidate.canonical_path) == (
            vendor,
            canonical_path,
        ):
            return value.record["attributes"]["selection"]["launcher"]
    return canonical_path
