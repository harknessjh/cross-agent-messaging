# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Thin CLI control plane for staged, atomic compatibility gates."""

from __future__ import annotations

import argparse
import datetime as dt
import re
import uuid
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

from . import compatibility, journal, profile, project, state
from .participants import ParticipantStatus

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def register_parser(domains: Any) -> None:
    """Register the compatibility command family on the project CLI."""

    parser = domains.add_parser(
        "compatibility",
        help="stage and atomically activate project compatibility gates",
    )
    commands = parser.add_subparsers(
        dest="compatibility_command",
        required=True,
    )
    commands.add_parser(
        "status",
        help="inspect compatibility state even when normal replay needs an upgrade",
    )

    plan = commands.add_parser(
        "plan",
        help="freeze the complete bound roster for one inert upgrade plan",
    )
    plan.add_argument("--feature-id", required=True)
    plan.add_argument("--feature-version", required=True, type=int)
    plan.add_argument("--expires-at", required=True)
    plan.add_argument("--operator-reference", required=True)
    plan.add_argument("--plan-id")
    plan.add_argument(
        "--feature-config-file",
        help="optional owner-only JSON object containing bounded feature config",
    )
    plan.add_argument(
        "--required-reader-epoch",
        type=int,
        default=compatibility.CURRENT_READER_EPOCH,
    )
    plan.add_argument(
        "--required-capability",
        action="append",
        help=(
            "additional reader capability; repeat for multiple values; the fixed "
            "kernel capability and exact feature/version capability are always required"
        ),
    )

    ready = commands.add_parser(
        "ready",
        help="record operator-confirmed reader compatibility for one participant",
    )
    ready.add_argument("--plan-id", required=True)
    ready.add_argument("--participant", required=True)
    ready.add_argument("--operator-reference", required=True)
    ready.add_argument(
        "--expected-validation-profile-sha256",
        help="optional exact guard for the running CAM source profile",
    )

    activate = commands.add_parser(
        "activate",
        help="activate a fully ready, unchanged plan exactly once",
    )
    activate.add_argument("--plan-id", required=True)
    activate.add_argument("--operator-reference", required=True)


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(microsecond=0)


def _utc_text(value: dt.datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise compatibility.CompatibilityEventError(
            "compatibility.timestamp",
            "compatibility timestamp must be timezone-aware",
        )
    return value.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")


def _parsed_utc(value: str) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(value[:-1] + "+00:00")
    except (TypeError, ValueError):
        raise compatibility.CompatibilityEventError(
            "compatibility.timestamp",
            "compatibility timestamp is invalid",
        ) from None
    if not value.endswith("Z") or parsed.utcoffset() != dt.timedelta(0):
        raise compatibility.CompatibilityEventError(
            "compatibility.timestamp",
            "compatibility timestamp must be UTC",
        )
    return parsed


def _canonical_uuid(value: str, *, field_name: str) -> str:
    try:
        return str(uuid.UUID(value))
    except (AttributeError, ValueError):
        raise compatibility.CompatibilityEventError(
            "compatibility.identifier",
            f"{field_name} must be a UUID",
        ) from None


def _record_reference(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "sequence": record["sequence"],
        "record_id": record["record_id"],
        "record_sha256": record["record_sha256"],
        "event_type": record["event_type"],
    }


def _staged_reference(
    staged: compatibility.StagedPlan | compatibility.StagedReadiness,
) -> dict[str, Any]:
    return {
        "sequence": staged.sequence,
        "record_id": staged.record_id,
        "record_sha256": staged.record_sha256,
    }


def _require_future(expires_at: str, *, now: dt.datetime) -> None:
    if _parsed_utc(expires_at) <= now.astimezone(dt.UTC):
        raise compatibility.CompatibilityEventError(
            "compatibility.plan_expired",
            "compatibility plan expiry must be in the future",
        )


def _frozen_roster(snapshot: state.StateSnapshot) -> list[dict[str, Any]]:
    all_participants = tuple(snapshot.roster.participants.values())
    if not all_participants:
        return []
    active = tuple(
        participant
        for participant in all_participants
        if participant.status != ParticipantStatus.RETIRED
    )
    if not active:
        raise compatibility.CompatibilityEventError(
            "compatibility.roster_empty",
            "compatibility plan requires at least one non-retired bound participant",
        )
    frozen: list[dict[str, Any]] = []
    for participant in active:
        if participant.status != ParticipantStatus.BOUND or participant.binding is None:
            raise compatibility.CompatibilityEventError(
                "compatibility.roster_unbound",
                "every non-retired participant must be bound before planning",
            )
        frozen.append(
            {
                "participant_id": participant.participant_id,
                "binding_generation": participant.binding.generation,
            }
        )
    return sorted(frozen, key=lambda item: item["participant_id"])


def _plan_attributes(
    snapshot: state.StateSnapshot,
    *,
    plan_id: str,
    feature_id: str,
    feature_version: int,
    feature_config: Mapping[str, Any],
    required_reader_epoch: int,
    required_capabilities: list[str],
    validation_profile_sha256: str,
    expires_at: str,
    operator_reference: str,
    now: dt.datetime,
) -> dict[str, Any]:
    attributes = {
        "format": compatibility.COMPATIBILITY_FORMAT,
        "plan_id": _canonical_uuid(plan_id, field_name="plan_id"),
        "feature_id": feature_id,
        "feature_version": feature_version,
        "feature_config": deepcopy(dict(feature_config)),
        "required_reader_epoch": required_reader_epoch,
        "required_capabilities": required_capabilities,
        "validation_profile_sha256": validation_profile_sha256,
        "frozen_participants": _frozen_roster(snapshot),
        "expires_at": expires_at,
        "operator_reference": operator_reference,
    }
    plan = compatibility.validate_plan(attributes)
    compatibility.require_plan_window(plan.expires_at, _utc_text(now))
    return plan.as_dict()


def _current_profile_digest(expected: str | None) -> str:
    if expected is not None and _SHA256.fullmatch(expected) is None:
        raise compatibility.CompatibilityEventError(
            "profile.digest_invalid",
            "expected validation profile must be 64 lowercase hexadecimal characters",
        )
    try:
        current = profile.require_live_profile(
            allow_dirty=False,
            expected_sha256=expected,
        )
    except profile.ValidationProfileError as error:
        raise project.ProjectError(error.code, error.detail) from None
    digest = current.validation_profile_sha256
    return digest


def _require_plan(
    snapshot: state.StateSnapshot,
    plan_id: str,
) -> compatibility.StagedPlan:
    staged = snapshot.compatibility.staged_plan(plan_id)
    if staged is None:
        raise compatibility.CompatibilityEventError(
            "compatibility.plan_unknown",
            "compatibility plan is not present in the verified journal",
        )
    return staged


def _ready_attributes(
    snapshot: state.StateSnapshot,
    *,
    plan_id: str,
    participant_selector: str,
    validation_profile_sha256: str,
    operator_reference: str,
    now: dt.datetime,
) -> dict[str, Any]:
    staged = _require_plan(snapshot, plan_id)
    _require_future(staged.plan.expires_at, now=now)
    participant = snapshot.roster.select(participant_selector)
    if participant.status != ParticipantStatus.BOUND or participant.binding is None:
        raise compatibility.CompatibilityEventError(
            "compatibility.roster_unbound",
            "readiness requires a currently bound participant",
        )
    frozen = {
        item.participant_id: item.binding_generation
        for item in staged.plan.frozen_participants
    }
    if frozen.get(participant.participant_id) != participant.binding.generation:
        raise compatibility.CompatibilityEventError(
            "compatibility.roster_drift",
            "participant or binding generation changed after planning",
        )
    if (
        compatibility.CURRENT_READER_EPOCH < staged.plan.required_reader_epoch
        or not set(staged.plan.required_capabilities).issubset(
            compatibility.SUPPORTED_READER_CAPABILITIES
        )
    ):
        raise compatibility.CompatibilityEventError(
            "compatibility.readiness_insufficient",
            "this reader does not satisfy the planned compatibility requirements",
        )
    if validation_profile_sha256 != staged.plan.validation_profile_sha256:
        raise compatibility.CompatibilityEventError(
            "compatibility.profile_drift",
            "current validation profile differs from the frozen compatibility plan",
        )
    attributes = {
        "format": compatibility.COMPATIBILITY_FORMAT,
        "plan_id": staged.plan.plan_id,
        "plan_record_id": staged.record_id,
        "plan_record_sha256": staged.record_sha256,
        "participant_id": participant.participant_id,
        "binding_generation": participant.binding.generation,
        "reader_epoch": compatibility.CURRENT_READER_EPOCH,
        "capabilities": sorted(compatibility.SUPPORTED_READER_CAPABILITIES),
        "validation_profile_sha256": validation_profile_sha256,
        "ready_at": _utc_text(now),
        "operator_reference": operator_reference,
    }
    return compatibility.validate_readiness(attributes).as_dict()


def _latest_readiness(
    projection: compatibility.CompatibilityProjection,
    plan: compatibility.CompatibilityPlan,
) -> list[compatibility.StagedReadiness]:
    latest: dict[str, compatibility.StagedReadiness] = {}
    for staged in projection.readiness_for_plan(plan.plan_id):
        latest[staged.readiness.participant_id] = staged
    expected = {participant.participant_id for participant in plan.frozen_participants}
    if set(latest) != expected:
        raise compatibility.CompatibilityEventError(
            "compatibility.readiness_incomplete",
            "every frozen participant must record readiness before activation",
        )
    return [latest[participant_id] for participant_id in sorted(latest)]


def _activation_attributes(
    snapshot: state.StateSnapshot,
    *,
    staged: compatibility.StagedPlan,
    validation_profile_sha256: str,
    operator_reference: str,
    now: dt.datetime,
) -> dict[str, Any]:
    _require_future(staged.plan.expires_at, now=now)
    if staged.plan.validation_profile_sha256 != validation_profile_sha256:
        raise compatibility.CompatibilityEventError(
            "compatibility.profile_drift",
            "current validation profile differs from the frozen compatibility plan",
        )
    readiness = _latest_readiness(snapshot.compatibility, staged.plan)
    readiness_profiles = {
        item.readiness.validation_profile_sha256 for item in readiness
    }
    if readiness_profiles and readiness_profiles != {validation_profile_sha256}:
        raise compatibility.CompatibilityEventError(
            "compatibility.profile_drift",
            "participant readiness does not match the current validation profile",
        )
    attributes = {
        "format": compatibility.COMPATIBILITY_FORMAT,
        "plan_id": staged.plan.plan_id,
        "plan_record_id": staged.record_id,
        "plan_record_sha256": staged.record_sha256,
        "feature_id": staged.plan.feature_id,
        "feature_version": staged.plan.feature_version,
        "required_reader_epoch": staged.plan.required_reader_epoch,
        "required_capabilities": list(staged.plan.required_capabilities),
        "validation_profile_sha256": staged.plan.validation_profile_sha256,
        "readiness": [
            {
                "participant_id": item.readiness.participant_id,
                "record_id": item.record_id,
                "record_sha256": item.record_sha256,
            }
            for item in readiness
        ],
        "activated_at": _utc_text(now),
        "operator_reference": operator_reference,
    }
    normalized = compatibility.validate_activation(attributes).as_dict()
    candidate = deepcopy(snapshot.compatibility)
    candidate.activate(
        normalized,
        participants=snapshot.roster.participants,
        recorded_at=_utc_text(now),
    )
    return normalized


def _find_activation_record(
    binding: project.ProjectBinding,
    *,
    plan_id: str,
) -> dict[str, Any]:
    records = journal.replay_records(
        binding,
        event_types={compatibility.COMPATIBILITY_GATE_ACTIVATED_EVENT},
    )
    for record in reversed(records):
        attributes = record.get("attributes")
        if isinstance(attributes, dict) and attributes.get("plan_id") == plan_id:
            return _record_reference(record)
    raise compatibility.CompatibilityEventError(
        "compatibility.activation_missing",
        "active compatibility gate has no matching journal record",
    )


def _staging_summary(
    inspection: compatibility.CompatibilityInspection,
) -> dict[str, Any]:
    projection = inspection.compatibility
    plans = []
    for staged in projection.staged_plans():
        readiness = projection.readiness_for_plan(staged.plan.plan_id)
        plans.append(
            {
                "record": _staged_reference(staged),
                "plan": staged.plan.as_dict(),
                "readiness": [
                    {
                        "record": _staged_reference(item),
                        "readiness": item.readiness.as_dict(),
                    }
                    for item in readiness
                ],
            }
        )
    return {
        "reader": {
            "epoch": compatibility.CURRENT_READER_EPOCH,
            "capabilities": sorted(compatibility.SUPPORTED_READER_CAPABILITIES),
        },
        "journal_sequence": inspection.journal_sequence,
        "journal_record_sha256": inspection.journal_record_sha256,
        "verified_journal_sequence": inspection.verified_journal_sequence,
        "verified_journal_record_sha256": (inspection.verified_journal_record_sha256),
        "plans": plans,
        "active_gates": [
            projection.active_gates[feature_id].as_dict()
            for feature_id in sorted(projection.active_gates)
        ],
    }


def _status(
    binding: project.ProjectBinding,
) -> tuple[int, dict[str, Any]]:
    inspection = state.inspect_compatibility(binding)
    summary = _staging_summary(inspection)
    if inspection.upgrade_required is not None:
        return 2, {
            "ok": False,
            "status": "upgrade_required",
            "compatibility": summary,
            "upgrade_required": inspection.upgrade_required.as_dict(),
        }
    return 0, {
        "ok": True,
        "status": "compatible",
        "compatibility": summary,
    }


def _plan(
    args: argparse.Namespace,
    binding: project.ProjectBinding,
    store: state.StateStore,
    *,
    now: dt.datetime,
) -> tuple[int, dict[str, Any]]:
    feature_config = (
        project.read_private_json(Path(args.feature_config_file))
        if args.feature_config_file
        else {}
    )
    capabilities = sorted(
        {
            compatibility.COMPATIBILITY_KERNEL_CAPABILITY,
            f"{args.feature_id}/{args.feature_version}",
            *(args.required_capability or []),
        }
    )
    validation_profile_sha256 = _current_profile_digest(None)
    plan_id = _canonical_uuid(args.plan_id or str(uuid.uuid4()), field_name="plan_id")
    with project.project_transaction(binding) as transaction:
        snapshot = store.snapshot(transaction=transaction)
        attributes = _plan_attributes(
            snapshot,
            plan_id=plan_id,
            feature_id=args.feature_id,
            feature_version=args.feature_version,
            feature_config=feature_config,
            required_reader_epoch=args.required_reader_epoch,
            required_capabilities=capabilities,
            validation_profile_sha256=validation_profile_sha256,
            expires_at=args.expires_at,
            operator_reference=args.operator_reference,
            now=now,
        )
        existing = snapshot.compatibility.staged_plan(plan_id)
        if existing is not None:
            if existing.plan.as_dict() != attributes:
                raise compatibility.CompatibilityEventError(
                    "compatibility.plan_conflict",
                    "compatibility plan ID was already used for different content",
                )
            return 0, {
                "ok": True,
                "status": "already_planned",
                "record": {
                    **_staged_reference(existing),
                    "event_type": compatibility.COMPATIBILITY_PLAN_EVENT,
                },
                "plan": attributes,
            }
        record = journal.append_record(
            binding,
            event_type=compatibility.COMPATIBILITY_PLAN_EVENT,
            attributes=attributes,
            now=now,
            transaction=transaction,
        )
    return 0, {
        "ok": True,
        "status": "planned",
        "record": _record_reference(record),
        "plan": attributes,
    }


def _ready(
    args: argparse.Namespace,
    binding: project.ProjectBinding,
    store: state.StateStore,
    *,
    now: dt.datetime,
) -> tuple[int, dict[str, Any]]:
    plan_id = _canonical_uuid(args.plan_id, field_name="plan_id")
    digest = _current_profile_digest(args.expected_validation_profile_sha256)
    with project.project_transaction(binding) as transaction:
        snapshot = store.snapshot(transaction=transaction)
        attributes = _ready_attributes(
            snapshot,
            plan_id=plan_id,
            participant_selector=args.participant,
            validation_profile_sha256=digest,
            operator_reference=args.operator_reference,
            now=now,
        )
        comparable = dict(attributes)
        comparable.pop("ready_at")
        for existing in reversed(snapshot.compatibility.readiness_for_plan(plan_id)):
            prior = existing.readiness.as_dict()
            prior.pop("ready_at")
            if prior == comparable:
                return 0, {
                    "ok": True,
                    "status": "already_ready",
                    "record": {
                        **_staged_reference(existing),
                        "event_type": compatibility.COMPATIBILITY_READINESS_EVENT,
                    },
                    "readiness": existing.readiness.as_dict(),
                }
        record = journal.append_record(
            binding,
            event_type=compatibility.COMPATIBILITY_READINESS_EVENT,
            attributes=attributes,
            now=now,
            transaction=transaction,
        )
    return 0, {
        "ok": True,
        "status": "ready",
        "record": _record_reference(record),
        "readiness": attributes,
    }


def _activate(
    args: argparse.Namespace,
    binding: project.ProjectBinding,
    store: state.StateStore,
    *,
    now: dt.datetime,
) -> tuple[int, dict[str, Any]]:
    plan_id = _canonical_uuid(args.plan_id, field_name="plan_id")
    current_profile_sha256 = _current_profile_digest(None)
    with project.project_transaction(binding) as transaction:
        snapshot = store.snapshot(transaction=transaction)
        staged = _require_plan(snapshot, plan_id)
        active = snapshot.compatibility.active_gate(staged.plan.feature_id)
        if active is not None and active.plan_id == plan_id:
            if active.operator_reference != args.operator_reference:
                raise compatibility.CompatibilityEventError(
                    "compatibility.activation_conflict",
                    "plan is already active under a different operator reference",
                )
            return 0, {
                "ok": True,
                "status": "already_active",
                "record": _find_activation_record(binding, plan_id=plan_id),
                "gate": active.as_dict(),
            }
        attributes = _activation_attributes(
            snapshot,
            staged=staged,
            validation_profile_sha256=current_profile_sha256,
            operator_reference=args.operator_reference,
            now=now,
        )
        try:
            gate, record = store.compatibility_activate(
                attributes,
                now=now,
                transaction=transaction,
            )
        except state.ProjectionRefreshError as error:
            return 0, {
                "ok": True,
                "status": "activated_projection_stale",
                "projection_current": False,
                "record": {
                    "sequence": error.sequence,
                    "record_id": error.record_id,
                    "record_sha256": error.record_sha256,
                    "event_type": compatibility.COMPATIBILITY_GATE_ACTIVATED_EVENT,
                },
                "gate": attributes,
                "warning": {
                    "code": error.code,
                    "detail": error.detail,
                },
            }
    return 0, {
        "ok": True,
        "status": "activated",
        "projection_current": True,
        "record": _record_reference(record),
        "gate": gate.as_dict(),
    }


def handle(
    args: argparse.Namespace,
    binding: project.ProjectBinding,
    store: state.StateStore,
    *,
    now: dt.datetime | None = None,
) -> tuple[int, dict[str, Any]]:
    """Execute one compatibility subcommand and return its JSON result."""

    observed = now or _utc_now()
    if args.compatibility_command == "status":
        return _status(binding)
    if args.compatibility_command == "plan":
        return _plan(args, binding, store, now=observed)
    if args.compatibility_command == "ready":
        return _ready(args, binding, store, now=observed)
    if args.compatibility_command == "activate":
        return _activate(args, binding, store, now=observed)
    raise compatibility.CompatibilityEventError(
        "compatibility.command",
        "compatibility command is unsupported",
    )
