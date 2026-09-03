# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Account-scoped product executable approval and CLI policy facade."""

from __future__ import annotations

import shlex
import sys
from pathlib import Path
from typing import Any

from tools import cam1
from tools import cam1_transport_native as _native
from tools.cam1lib import (
    participants,
    product_approvals,
    product_executables,
    project,
    state,
)

TransportError = _native.TransportError
_domain_transport_error = _native._domain_transport_error

# Clean, source-profiled readers released before account executable approval
# existed.  Automatic grandfathering is deliberately limited to enrollment
# proposals produced by these exact historical profiles.  Later enrollment or
# unversioned metadata records must use the ordinary candidate-card approval.
LEGACY_PRODUCT_APPROVAL_PROFILES = frozenset(
    {
        # 22d49669337792cdad9631b7928eaa72a1164a35
        "c1752841229245561fcbd34570749978d1229098f8b09861aae44305b666c06d",
        # 71a69f7558d88192fc48ace124e019003e40179b
        "1f6864186c9a9ca763b5cef949dd4319b59b8b154be281ca5b2665c8ae997fd8",
        # 9710ff4e74baaf3416acaf324dee1308ae338b0c
        "324c2a06e6a909eb15722ebc3a8b086e77decee08860200b74f85d4a37914f0e",
    }
)


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
    if supplied_path != approved:
        raise TransportError(
            "roster.product_executable_mismatch",
            "live transport executable does not match the operator-approved roster "
            "path",
        )


def resolve_product_binary(
    value: str,
    *,
    vendor: str,
    binding: project.ProjectBinding | None = None,
) -> str:
    """Resolve one account-approved product, migrating a legacy roster once."""

    try:
        resolved, _approval = product_approvals.require_approved_executable(
            vendor=vendor,
            product_bin=value,
            allow_path_lookup=False,
        )
        return resolved
    except product_approvals.ProductApprovalError as error:
        if (
            error.code
            not in {
                "product_approval.required",
                "product_approval.registry_missing",
                "path.missing",
            }
            or binding is None
        ):
            raise TransportError(error.code, error.detail) from error

    try:
        candidate_path, _candidate_source = product_executables.resolve_candidate_path(
            vendor, value, allow_path_lookup=False
        )
    except product_approvals.ProductApprovalError as error:
        raise TransportError(error.code, error.detail) from error

    try:
        snapshot = state.StateStore(binding).snapshot()
    except (cam1.CamUsageError, project.ProjectError) as error:
        raise _domain_transport_error(error) from error
    eligible = sorted(
        (
            participant
            for participant in snapshot.roster.participants.values()
            if participant.vendor == vendor
            and participant.status == participants.ParticipantStatus.BOUND
            and participant.binding is not None
            and participant.approved_product_executable == candidate_path
        ),
        key=lambda participant: participant.participant_id,
    )
    evidenced = [
        (participant, _legacy_product_confirmation(snapshot, participant))
        for participant in eligible
    ]
    evidenced = [item for item in evidenced if item[1] is not None]
    if not evidenced:
        recovery = product_executables.discovery_command(vendor, candidate_path)
        raise TransportError(
            "product_approval.required",
            "product executable has no account approval; run "
            f"{shlex.join(recovery)}, review its card, then run the exact "
            "approval_command it returns after replacing DIRECT_OPERATOR_REFERENCE",
        )
    legacy, confirmation = evidenced[0]
    assert confirmation is not None
    assert legacy.binding is not None
    try:
        approved = product_approvals.grandfather_candidate(
            vendor=vendor,
            product_bin=candidate_path,
            operator_reference=confirmation[0],
            migration={
                "project_id": binding.project_id,
                "participant_id": legacy.participant_id,
                "binding_generation": legacy.binding.generation,
                "source": confirmation[1],
                "source_reference": confirmation[2],
            },
        )
        return approved["candidate"]["canonical_path"]
    except product_approvals.ProductApprovalError as error:
        raise TransportError(error.code, error.detail) from error


def _legacy_product_confirmation(
    snapshot: state.StateSnapshot,
    participant: participants.Participant,
) -> tuple[str, str, str] | None:
    """Find direct historical operator evidence for the current roster path."""

    executable = participant.approved_product_executable
    confirmed_proposals = sorted(
        (
            proposal
            for proposal in snapshot.enrollment.proposals.values()
            if proposal.participant_id == participant.participant_id
            and proposal.status.value == "confirmed"
            and proposal.operator_reference is not None
            and proposal.execution_context.product_executable == executable
            and proposal.execution_context.validation_profile_sha256
            in LEGACY_PRODUCT_APPROVAL_PROFILES
        ),
        key=lambda proposal: (proposal.confirmed_at or "", proposal.proposal_id),
        reverse=True,
    )
    if confirmed_proposals:
        proposal = confirmed_proposals[0]
        assert proposal.operator_reference is not None
        return (
            proposal.operator_reference,
            "confirmed_enrollment",
            proposal.proposal_id,
        )
    return None


def discover_product_executable(
    *, vendor: str, product_bin: str | None
) -> dict[str, Any]:
    try:
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
        raise TransportError(error.code, error.detail) from error


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
        return product_approvals.recover_partial_tail(**kwargs)
    except product_approvals.ProductApprovalError as error:
        raise TransportError(error.code, error.detail) from error


def revoke_product_executable(**kwargs: Any) -> dict[str, Any]:
    try:
        return product_approvals.revoke_approval(**kwargs)
    except product_approvals.ProductApprovalError as error:
        raise TransportError(error.code, error.detail) from error


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
