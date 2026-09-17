# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Account-scoped product executable approval and CLI policy facade."""

from __future__ import annotations

import shlex
import shutil
import sys
from pathlib import Path
from typing import Any

from tools import cam1_transport_native as _native
from tools.cam1lib import (
    participants,
    product_approvals,
    product_executables,
    product_installations,
    project,
    state,
)

TransportError = _native.TransportError
_domain_transport_error = _native._domain_transport_error


def _require_approved_product_executable(
    participant: participants.Participant,
    supplied_path: str,
) -> None:
    """Fail before product I/O unless the exact rostered executable is used."""

    approved = participant.approved_product_executable
    if approved is None:
        raise TransportError(
            "roster.product_executable_missing",
            "participant has no operator-approved product executable; update its "
            "metadata before live transport",
        )
    selection = product_installations.selected_path(participant.vendor, supplied_path)
    if selection != approved:
        raise TransportError(
            "roster.product_executable_mismatch",
            f"participant {participant.common_name!r} expects {approved!r}; "
            f"supplied executable selection is {selection!r} (target {supplied_path!r}). "
            "After a product update, run product-discover with --vendor and "
            "--participant to review approval and roster-update guidance; "
            "do not re-enroll the session",
        )


def resolve_product_binary(
    value: str,
    *,
    vendor: str,
    binding: project.ProjectBinding | None = None,
) -> str:
    """Resolve an approved fingerprint; a legacy roster path is not approval.

    The binding argument remains accepted for project-aware callers.
    Resolving an executable never creates an account or project record.
    """

    try:
        resolved, _approval = product_approvals.require_approved_executable(
            vendor=vendor,
            product_bin=value,
            allow_path_lookup=False,
        )
        return resolved
    except product_approvals.ProductApprovalError as error:
        if error.code == "product_approval.not_found":
            raise TransportError(
                error.code,
                f"{vendor} executable {value!r} could not be resolved. "
                "An update may have moved or removed the recorded version. "
                f"Run product-discover --vendor {vendor} "
                "(with --participant NAME for a project) to inspect a current "
                "candidate without executing it; no fallback was attempted",
            ) from error
        if error.code in {
            "product_approval.required",
            "product_approval.registry_missing",
            "path.missing",
        }:
            recovery = product_executables.discovery_command(vendor, value)
            raise TransportError(
                "product_approval.required",
                "product executable has no account approval; run "
                f"{shlex.join(recovery)}, review its card, then run the exact "
                "approval_command it returns after replacing DIRECT_OPERATOR_REFERENCE",
            ) from error
        raise TransportError(error.code, error.detail, audit=error.audit) from error


def discover_product_executable(
    *,
    vendor: str,
    product_bin: str | None,
    binding: project.ProjectBinding | None = None,
    participant_selector: str | None = None,
) -> dict[str, Any]:
    """Inspect a candidate and optionally plan a roster update without mutation."""

    participant = None
    if participant_selector is not None:
        if binding is None:
            raise TransportError(
                "argument.project_required",
                "participant discovery requires a CAM project",
            )
        snapshot = state.StateStore(binding).snapshot()
        participant = snapshot.roster.select(participant_selector)
        if participant.vendor != vendor:
            raise TransportError(
                "roster.vendor_mismatch", "candidate vendor does not match participant"
            )
        if (
            participant.binding is None
            or participant.status != participants.ParticipantStatus.BOUND
        ):
            raise TransportError(
                "roster.participant_stale", "participant must be active and bound"
            )

    card = _discover_product_card(vendor=vendor, product_bin=product_bin)
    if participant is not None:
        assert binding is not None
        card["participant_update"] = _participant_update_guidance(
            binding,
            participant,
            card.get("selection_path", card["candidate"]["canonical_path"]),
        )
    return card


def _participant_update_guidance(
    binding: project.ProjectBinding,
    participant: participants.Participant,
    candidate_path: str,
) -> dict[str, Any]:
    """Return a revision-guarded command, never apply it or infer approval."""

    changed = participant.approved_product_executable != candidate_path
    result: dict[str, Any] = {
        "status": "metadata_update_required" if changed else "roster_path_current",
        "participant_id": participant.participant_id,
        "common_name": participant.common_name,
        "metadata_revision": participant.metadata_revision,
        "recorded_path": participant.approved_product_executable,
        "candidate_path": candidate_path,
        "next_step": (
            "Complete the candidate's account approval steps first. Then obtain "
            "direct operator confirmation of any roster update and run its exact "
            "command with a truthful operator reference. No session re-enrollment "
            "is needed. A current roster path alone is not product approval."
        ),
    }
    if changed:
        command = [
            sys.executable,
            str(Path(__file__).resolve().with_name("cam1_project.py")),
            "--project-root",
            str(binding.git_top_level),
            "--state-root",
            str(binding.state_root),
            "--git-bin",
            binding.git_bin,
            "participant",
            "update-metadata",
            "--participant",
            participant.participant_id,
            "--expected-revision",
            str(participant.metadata_revision),
            "--product-bin",
            candidate_path,
            "--operator-reference",
            "DIRECT_OPERATOR_REFERENCE",
        ]
        result["command"] = command
        result["command_text"] = shlex.join(command)
    return result


def _discover_product_card(*, vendor: str, product_bin: str | None) -> dict[str, Any]:
    try:
        # Discovery alone may inspect PATH. Live callers must pass the explicit
        # stable launcher or strict canonical path returned by the card.
        explicit = product_bin
        if explicit is None:
            command = product_executables.PRODUCT_COMMANDS.get(vendor)
            explicit = shutil.which(command) if command is not None else None
        installation = (
            product_installations.resolve(vendor=vendor, product_bin=explicit)
            if explicit is not None
            else None
        )
        if installation is not None:
            canonical, evidence = installation
            return {
                "ok": True,
                "status": "installation_approved",
                "candidate": {
                    "vendor": vendor,
                    "canonical_path": canonical,
                    "fingerprint": evidence["fingerprint"],
                    "fingerprint_sha256": evidence["fingerprint_sha256"],
                    "source": "explicit_installation",
                },
                "selection_path": evidence["selection"]["launcher"],
                "installation_approval": evidence,
                "next_step": "Use selection_path for onboarding, participant metadata and live commands. Normal in-root updates need no new approval or roster change. The observed fingerprint is not a separate release approval.",
            }
        card = product_executables.candidate_card(
            product_executables.discover_candidate(vendor, product_bin)
        )
        candidate = card["candidate"]
        status = product_approvals.approval_status(
            vendor=vendor,
            product_bin=candidate["canonical_path"],
        )
        active = status["active"]
        if not active:
            return card

        current = active[0]
        attributes = current["attributes"]
        card["existing_approval"] = {
            "record_id": current["record_id"],
            "fingerprint_sha256": attributes["fingerprint_sha256"],
            "recorded_at": current["recorded_at"],
        }
        if attributes["fingerprint_sha256"] == candidate["fingerprint_sha256"]:
            card["status"] = "already_approved"
            card["next_step"] = (
                "This exact executable fingerprint already has an active account "
                "approval; no new approval is required."
            )
            return card

        revocation_arguments = (
            "product-revoke",
            "--vendor",
            vendor,
            "--product-bin",
            candidate["canonical_path"],
            "--approval-record-id",
            current["record_id"],
            "--expected-fingerprint-sha256",
            attributes["fingerprint_sha256"],
            "--operator-reference",
            "DIRECT_OPERATOR_REFERENCE",
        )
        command_prefix = tuple(card["approval_command"][:2])
        revocation_command = (*command_prefix, *revocation_arguments)
        card.update(
            {
                "status": "replacement_approval_required",
                "revocation_arguments": list(revocation_arguments),
                "revocation_command": list(revocation_command),
                "revocation_command_text": shlex.join(revocation_command),
                "next_step": (
                    "The canonical path has a different active fingerprint. Review "
                    "product-status, directly confirm and run the returned guarded "
                    "product-revoke command, then run product-discover again and "
                    "directly approve the new candidate."
                ),
            }
        )
        return card
    except product_approvals.ProductApprovalError as error:
        raise TransportError(error.code, error.detail) from error


def approve_product_executable(**kwargs: Any) -> dict[str, Any]:
    try:
        return product_approvals.approve_candidate(**kwargs)
    except product_approvals.ProductApprovalError as error:
        raise TransportError(error.code, error.detail, audit=error.audit) from error


def product_executable_status(**kwargs: Any) -> dict[str, Any]:
    try:
        return product_approvals.approval_status(**kwargs)
    except product_approvals.ProductApprovalError as error:
        raise TransportError(error.code, error.detail) from error


def product_recovery_status() -> dict[str, Any]:
    try:
        result = product_approvals.approval_recovery_status()
        arguments = result.get("recovery_arguments")
        if isinstance(arguments, list):
            command = (
                sys.executable,
                str(Path(__file__).resolve().with_name("cam1_transport.py")),
                *arguments,
            )
            result["recovery_command"] = list(command)
            result["recovery_command_text"] = shlex.join(command)
        return result
    except product_approvals.ProductApprovalError as error:
        raise TransportError(error.code, error.detail) from error


def recover_product_partial_tail(**kwargs: Any) -> dict[str, Any]:
    try:
        result = product_approvals.recover_partial_tail(**kwargs)
        arguments = result.get("reconciliation_arguments")
        if isinstance(arguments, list):
            command = (
                sys.executable,
                str(Path(__file__).resolve().with_name("cam1_transport.py")),
                *arguments,
            )
            result["reconciliation_command"] = list(command)
            result["reconciliation_command_text"] = shlex.join(command)
        return result
    except product_approvals.ProductApprovalError as error:
        audit = getattr(error, "audit", None)
        if isinstance(audit, dict):
            arguments = audit.get("reconciliation_arguments")
            if isinstance(arguments, list):
                command = (
                    sys.executable,
                    str(Path(__file__).resolve().with_name("cam1_transport.py")),
                    *arguments,
                )
                audit["reconciliation_command"] = list(command)
                audit["reconciliation_command_text"] = shlex.join(command)
        raise TransportError(error.code, error.detail, audit=audit) from error


def revoke_product_executable(**kwargs: Any) -> dict[str, Any]:
    try:
        return product_approvals.revoke_approval(**kwargs)
    except product_approvals.ProductApprovalError as error:
        raise TransportError(error.code, error.detail, audit=error.audit) from error


def _require_current_product_approval(vendor: str, path: str) -> None:
    """Recheck an operation-local approval immediately before product I/O."""

    try:
        product_approvals.require_approved_metadata(
            vendor=vendor,
            product_bin=path,
        )
    except product_approvals.ProductApprovalError as error:
        raise TransportError(error.code, error.detail) from error


def begin_operation() -> None:
    """Start one operation-local product-approval attestation scope."""

    product_approvals.begin_operation()


def installation_command(args: Any) -> dict[str, Any]:
    """Dispatch non-executing installation policy commands behind the source gate."""

    operation = args.command.removeprefix("product-installation-")
    fields = {
        "discover": ("vendor", "launcher", "installation_root"),
        "approve": (
            "vendor",
            "launcher",
            "installation_root",
            "expected_card_sha256",
            "operator_reference",
        ),
        "revoke": (
            "vendor",
            "launcher",
            "approval_record_id",
            "expected_policy_sha256",
            "operator_reference",
        ),
        "status": (),
    }
    try:
        return getattr(product_installations, operation)(
            **{name: getattr(args, name) for name in fields[operation]}
        )
    except product_approvals.ProductApprovalError as error:
        raise TransportError(error.code, error.detail, audit=error.audit) from error
