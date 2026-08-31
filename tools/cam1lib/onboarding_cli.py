# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""CLI orchestration for self-inspection and journaled enrollment."""

from __future__ import annotations

import argparse
import datetime as dt
import uuid
from dataclasses import replace
from typing import Any

from . import onboarding, project, state
from .protocol import CamUsageError
from .state_store import ParticipantAlreadyEnrolled, StateStore


def add_parser(domains: Any) -> None:
    onboarding_parser = domains.add_parser("onboarding")
    onboarding_commands = onboarding_parser.add_subparsers(
        dest="onboarding_command", required=True
    )
    for command, help_text in (
        ("inspect-self", "inspect this agent without changing CAM state"),
        ("prepare", "record or reuse one unconfirmed self-enrollment proposal"),
    ):
        self_parser = onboarding_commands.add_parser(command, help=help_text)
        self_parser.add_argument(
            "--vendor", choices=("codex", "claude-code"), required=True
        )
        self_parser.add_argument("--common-name")
        self_parser.add_argument("--display-name")
        self_parser.add_argument("--role")
        self_parser.add_argument("--session-id")
        self_parser.add_argument("--session-label")
        self_parser.add_argument("--session-kind")
        self_parser.add_argument("--product-bin")
    confirm = onboarding_commands.add_parser(
        "confirm", help="confirm one exact displayed self-enrollment proposal"
    )
    confirm.add_argument("--proposal-id", required=True)
    confirm.add_argument("--confirmation-code", required=True)
    confirm.add_argument("--operator-reference", required=True)
    status = onboarding_commands.add_parser(
        "status", help="list enrollment proposals and the project roster"
    )
    status.add_argument("--show-identifiers", action="store_true")


def _uuid_values_equal(left: Any, right: Any) -> bool:
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    try:
        return uuid.UUID(left) == uuid.UUID(right)
    except (ValueError, AttributeError):
        return False


def _utc_now() -> tuple[dt.datetime, str]:
    observed = dt.datetime.now(dt.UTC)
    timespec = "microseconds" if observed.microsecond else "seconds"
    return observed, observed.isoformat(timespec=timespec).replace("+00:00", "Z")


def _inspection(
    args: argparse.Namespace, binding: project.ProjectBinding
) -> onboarding.SelfInspection:
    return onboarding.inspect_self(
        binding,
        vendor=args.vendor,
        common_name=args.common_name,
        display_name=args.display_name,
        role=args.role,
        session_id=args.session_id,
        session_label=args.session_label,
        session_kind=args.session_kind,
        product_bin=args.product_bin,
    )


def _available_common_name(
    inspection: onboarding.SelfInspection,
    snapshot: state.StateSnapshot,
) -> str:
    conflicts = {
        participant.common_name.casefold()
        for participant in snapshot.roster.participants.values()
    }
    if inspection.common_name.casefold() not in conflicts:
        return inspection.common_name
    suffix = inspection.session_id.split("-", 1)[0]
    prefix = inspection.common_name[: 63 - len(suffix) - 1].rstrip("-")
    candidate = f"{prefix or inspection.vendor}-{suffix}"
    if candidate.casefold() in conflicts:
        raise CamUsageError(
            "onboarding.common_name_conflict",
            "generated common name is already assigned; choose a project-local name",
        )
    return candidate


def _proposal_matches_inspection(
    proposal: Any, inspection: onboarding.SelfInspection
) -> bool:
    return (
        proposal.project_id == inspection.project_id
        and proposal.project_display_name == inspection.project_display_name
        and proposal.common_name == inspection.common_name
        and proposal.display_name == inspection.display_name
        and proposal.role == inspection.role
        and proposal.vendor == inspection.vendor
        and _uuid_values_equal(proposal.session_id, inspection.session_id)
        and proposal.session_label == inspection.session_label
        and proposal.session_kind == inspection.session_kind
        and proposal.session_git_top_level == inspection.session_git_top_level
        and proposal.session_git_common_dir == inspection.session_git_common_dir
        and proposal.discovery_source == inspection.discovery_source
        and proposal.execution_context.cam_checkout == inspection.cam_checkout
        and proposal.execution_context.validation_profile_sha256
        == inspection.validation_profile_sha256
        and proposal.execution_context.project_root == inspection.project_root
        and proposal.execution_context.product_executable
        == inspection.product_executable
    )


def _prepare(
    args: argparse.Namespace,
    binding: project.ProjectBinding,
    store: StateStore,
) -> dict[str, Any]:
    inspection = _inspection(args, binding)
    if args.common_name is None:
        selected_name = _available_common_name(inspection, store.snapshot())
        if selected_name != inspection.common_name:
            inspection = replace(inspection, common_name=selected_name)
    try:
        proposal, reused = store.participant_enrollment_propose(
            common_name=inspection.common_name,
            display_name=inspection.display_name,
            role=inspection.role,
            vendor=inspection.vendor,
            session_id=inspection.session_id,
            session_label=inspection.session_label,
            session_kind=inspection.session_kind,
            session_git_top_level=inspection.session_git_top_level,
            session_git_common_dir=inspection.session_git_common_dir,
            discovery_source=inspection.discovery_source,
            execution_context={
                "cam_checkout": inspection.cam_checkout,
                "validation_profile_sha256": inspection.validation_profile_sha256,
                "project_root": inspection.project_root,
                "product_executable": inspection.product_executable,
                "product_executable_source": (inspection.product_executable_source),
            },
        )
    except ParticipantAlreadyEnrolled as error:
        participant = error.participant
        return {
            "ok": True,
            "status": "already_enrolled",
            "participant": participant.as_dict(redact=True),
            "operator_confirmation_required": False,
        }
    return {
        "ok": True,
        "status": "proposal_reused" if reused else "proposal_recorded",
        "identity_card": onboarding.identity_card(proposal),
        "operator_confirmation_required": True,
        "message_sent": False,
    }


def _confirm(
    args: argparse.Namespace,
    binding: project.ProjectBinding,
    store: StateStore,
) -> dict[str, Any]:
    proposal = store.snapshot().enrollment.select(args.proposal_id)
    if args.confirmation_code != proposal.confirmation_code:
        raise CamUsageError(
            "onboarding.confirmation_code_mismatch",
            "confirmation code does not match the displayed identity card",
        )
    if proposal.status.value == "superseded":
        raise CamUsageError(
            "onboarding.proposal_superseded",
            "superseded enrollment proposal cannot be confirmed",
        )
    inspection = onboarding.inspect_self(
        binding,
        vendor=proposal.vendor,
        common_name=proposal.common_name,
        display_name=proposal.display_name,
        role=proposal.role,
        session_id=proposal.session_id,
        session_label=proposal.session_label,
        session_kind=proposal.session_kind,
        product_bin=proposal.execution_context.product_executable,
    )
    if not _proposal_matches_inspection(proposal, inspection):
        raise CamUsageError(
            "onboarding.proposal_stale",
            "current session, project, executable, or CAM source no longer "
            "matches the displayed proposal; prepare a fresh card",
        )
    event_now, confirmed_at = _utc_now()
    participant, reused = store.participant_enrollment_confirm(
        proposal.proposal_id,
        expected_proposal_sha256=proposal.proposal_sha256,
        operator_reference=args.operator_reference,
        confirmed_at=confirmed_at,
        now=event_now,
    )
    return {
        "ok": True,
        "status": "already_confirmed" if reused else "enrolled",
        "participant": participant.as_dict(redact=True),
        "proposal_id": proposal.proposal_id,
        "proposal_sha256": proposal.proposal_sha256,
        "message_sent": False,
    }


def handle(
    args: argparse.Namespace,
    binding: project.ProjectBinding,
    store: StateStore,
) -> dict[str, Any]:
    if args.onboarding_command == "status":
        snapshot = store.snapshot()
        redact = not args.show_identifiers
        return {
            "ok": True,
            "status": "listed",
            "project": binding.summary(),
            "roster": snapshot.roster.as_dict(redact=redact),
            "enrollment": snapshot.enrollment.as_dict(redact=redact),
        }
    if args.onboarding_command == "inspect-self":
        inspection = _inspection(args, binding)
        return {
            "ok": True,
            "status": "inspected",
            "inspection": inspection.as_dict(),
            "state_changed": False,
            "operator_confirmation_required": True,
        }
    if args.onboarding_command == "prepare":
        return _prepare(args, binding, store)
    return _confirm(args, binding, store)
