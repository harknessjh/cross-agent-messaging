# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Permanent compatibility kernel for journal-backed CAM project state.

Compatibility plans and participant readiness are deliberately non-state
journal events: older readers can skip them.  One fixed-header state event
activates a fully staged gate.  A reader that understands the fixed header but
cannot satisfy its epoch or capability requirements stops with an actionable
upgrade diagnostic before it reaches feature-specific state.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from jsonschema import Draft202012Validator, FormatChecker

from .causal import CAUSAL_CAPABILITY
from .errors import ProjectError
from .participants import Participant, ParticipantRoster, ParticipantStatus

COMPATIBILITY_FORMAT = "CAM-COMPAT/1"
CURRENT_READER_EPOCH = 1
COMPATIBILITY_KERNEL_FEATURE_ID = "compatibility.kernel"
COMPATIBILITY_KERNEL_FEATURE_VERSION = 1
COMPATIBILITY_KERNEL_CAPABILITY = "compatibility.kernel/1"
SUPPORTED_READER_CAPABILITIES = frozenset(
    {COMPATIBILITY_KERNEL_CAPABILITY, CAUSAL_CAPABILITY}
)

COMPATIBILITY_PLAN_EVENT = "compatibility.upgrade.planned"
COMPATIBILITY_READINESS_EVENT = "compatibility.participant.ready"
COMPATIBILITY_GATE_ACTIVATED_EVENT = "state.compatibility.gate_activated"
COMPATIBILITY_STAGING_EVENT_TYPES = frozenset(
    {COMPATIBILITY_PLAN_EVENT, COMPATIBILITY_READINESS_EVENT}
)
COMPATIBILITY_EVENT_TYPES = COMPATIBILITY_STAGING_EVENT_TYPES | {
    COMPATIBILITY_GATE_ACTIVATED_EVENT
}

MAX_FEATURE_CONFIG_BYTES = 16_384
MAX_FEATURE_CONFIG_DEPTH = 8
MAX_PLAN_LIFETIME = dt.timedelta(days=7)

_SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "schemas"
    / "cam-compatibility-event-1.schema.json"
)


class CompatibilityEventError(ProjectError):
    """A recognized compatibility event is malformed or inconsistent."""


class CompatibilityUpgradeRequired(CompatibilityEventError):
    """The journal's fixed gate header requires a newer state reader."""

    def __init__(
        self,
        *,
        plan_id: str,
        feature_id: str,
        feature_version: int,
        validation_profile_sha256: str,
        required_reader_epoch: int,
        required_capabilities: tuple[str, ...],
        current_reader_epoch: int = CURRENT_READER_EPOCH,
        supported_capabilities: frozenset[str] = SUPPORTED_READER_CAPABILITIES,
        journal_sequence: int | None = None,
    ) -> None:
        self.plan_id = plan_id
        self.feature_id = feature_id
        self.feature_version = feature_version
        self.validation_profile_sha256 = validation_profile_sha256
        self.required_reader_epoch = required_reader_epoch
        self.required_capabilities = required_capabilities
        self.current_reader_epoch = current_reader_epoch
        self.supported_capabilities = tuple(sorted(supported_capabilities))
        self.missing_capabilities = tuple(
            sorted(set(required_capabilities) - supported_capabilities)
        )
        self.journal_sequence = journal_sequence
        position = (
            f" at journal sequence {journal_sequence}"
            if journal_sequence is not None
            else ""
        )
        missing = ", ".join(self.missing_capabilities) or "none"
        super().__init__(
            "compatibility.upgrade_required",
            (
                f"active {feature_id}/{feature_version} compatibility gate{position} "
                f"requires reader epoch {required_reader_epoch} and capabilities "
                f"[{', '.join(required_capabilities)}]; this reader is epoch "
                f"{current_reader_epoch} and is missing [{missing}]. Upgrade the CAM "
                "checkout before replaying or mutating this project"
            ),
        )

    def at_sequence(self, sequence: int) -> CompatibilityUpgradeRequired:
        """Return the same structured diagnostic bound to a journal position."""

        return CompatibilityUpgradeRequired(
            plan_id=self.plan_id,
            feature_id=self.feature_id,
            feature_version=self.feature_version,
            validation_profile_sha256=self.validation_profile_sha256,
            required_reader_epoch=self.required_reader_epoch,
            required_capabilities=self.required_capabilities,
            current_reader_epoch=self.current_reader_epoch,
            supported_capabilities=frozenset(self.supported_capabilities),
            journal_sequence=sequence,
        )

    def as_dict(self) -> dict[str, Any]:
        """Return bounded machine-readable upgrade requirements."""

        return {
            "code": self.code,
            "detail": self.detail,
            "plan_id": self.plan_id,
            "feature_id": self.feature_id,
            "feature_version": self.feature_version,
            "validation_profile_sha256": self.validation_profile_sha256,
            "required_reader_epoch": self.required_reader_epoch,
            "required_capabilities": list(self.required_capabilities),
            "current_reader_epoch": self.current_reader_epoch,
            "supported_capabilities": list(self.supported_capabilities),
            "missing_capabilities": list(self.missing_capabilities),
            "journal_sequence": self.journal_sequence,
        }


@dataclass(frozen=True, slots=True)
class FrozenParticipant:
    participant_id: str
    binding_generation: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "participant_id": self.participant_id,
            "binding_generation": self.binding_generation,
        }


@dataclass(frozen=True, slots=True)
class ReadinessReference:
    participant_id: str
    record_id: str
    record_sha256: str

    def as_dict(self) -> dict[str, str]:
        return {
            "participant_id": self.participant_id,
            "record_id": self.record_id,
            "record_sha256": self.record_sha256,
        }


@dataclass(frozen=True, slots=True)
class CompatibilityPlan:
    plan_id: str
    feature_id: str
    feature_version: int
    feature_config: Mapping[str, Any]
    validation_profile_sha256: str
    required_reader_epoch: int
    required_capabilities: tuple[str, ...]
    frozen_participants: tuple[FrozenParticipant, ...]
    expires_at: str
    operator_reference: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "format": COMPATIBILITY_FORMAT,
            "plan_id": self.plan_id,
            "feature_id": self.feature_id,
            "feature_version": self.feature_version,
            "feature_config": deepcopy(dict(self.feature_config)),
            "validation_profile_sha256": self.validation_profile_sha256,
            "required_reader_epoch": self.required_reader_epoch,
            "required_capabilities": list(self.required_capabilities),
            "frozen_participants": [
                participant.as_dict() for participant in self.frozen_participants
            ],
            "expires_at": self.expires_at,
            "operator_reference": self.operator_reference,
        }


@dataclass(frozen=True, slots=True)
class CompatibilityReadiness:
    plan_id: str
    plan_record_id: str
    plan_record_sha256: str
    participant_id: str
    binding_generation: int
    reader_epoch: int
    capabilities: tuple[str, ...]
    validation_profile_sha256: str
    ready_at: str
    operator_reference: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "format": COMPATIBILITY_FORMAT,
            "plan_id": self.plan_id,
            "plan_record_id": self.plan_record_id,
            "plan_record_sha256": self.plan_record_sha256,
            "participant_id": self.participant_id,
            "binding_generation": self.binding_generation,
            "reader_epoch": self.reader_epoch,
            "capabilities": list(self.capabilities),
            "validation_profile_sha256": self.validation_profile_sha256,
            "ready_at": self.ready_at,
            "operator_reference": self.operator_reference,
        }


@dataclass(frozen=True, slots=True)
class CompatibilityGate:
    plan_id: str
    plan_record_id: str
    plan_record_sha256: str
    feature_id: str
    feature_version: int
    validation_profile_sha256: str
    required_reader_epoch: int
    required_capabilities: tuple[str, ...]
    readiness: tuple[ReadinessReference, ...]
    activated_at: str
    operator_reference: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "format": COMPATIBILITY_FORMAT,
            "plan_id": self.plan_id,
            "plan_record_id": self.plan_record_id,
            "plan_record_sha256": self.plan_record_sha256,
            "feature_id": self.feature_id,
            "feature_version": self.feature_version,
            "validation_profile_sha256": self.validation_profile_sha256,
            "required_reader_epoch": self.required_reader_epoch,
            "required_capabilities": list(self.required_capabilities),
            "readiness": [reference.as_dict() for reference in self.readiness],
            "activated_at": self.activated_at,
            "operator_reference": self.operator_reference,
        }


@dataclass(frozen=True, slots=True)
class StagedPlan:
    plan: CompatibilityPlan
    record_id: str
    record_sha256: str
    sequence: int


@dataclass(frozen=True, slots=True)
class StagedReadiness:
    readiness: CompatibilityReadiness
    record_id: str
    record_sha256: str
    sequence: int


def _load_schema() -> dict[str, Any]:
    with _SCHEMA_PATH.open("r", encoding="utf-8") as handle:
        schema = cast(dict[str, Any], json.load(handle))
    Draft202012Validator.check_schema(schema)
    return schema


_VALIDATOR = Draft202012Validator(_load_schema(), format_checker=FormatChecker())


def _validate_schema(event_type: str, attributes: Mapping[str, Any]) -> None:
    instance = {"event_type": event_type, "attributes": dict(attributes)}
    errors = sorted(
        _VALIDATOR.iter_errors(instance),
        key=lambda error: tuple(str(component) for component in error.absolute_path),
    )
    if errors:
        path = ".".join(str(component) for component in errors[0].absolute_path)
        location = f" at {path}" if path else ""
        raise CompatibilityEventError(
            "compatibility.event_schema",
            f"compatibility event does not match its fixed contract{location}",
        )


def _timestamp(value: str) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(value[:-1] + "+00:00")
    except (TypeError, ValueError):
        raise CompatibilityEventError(
            "compatibility.timestamp", "compatibility timestamp is invalid"
        ) from None
    if not value.endswith("Z") or parsed.utcoffset() != dt.timedelta(0):
        raise CompatibilityEventError(
            "compatibility.timestamp", "compatibility timestamp must be UTC"
        )
    return parsed


def _require_recorded_time(
    declared: str,
    recorded_at: str,
    *,
    label: str,
) -> None:
    if _timestamp(declared) != _timestamp(recorded_at):
        raise CompatibilityEventError(
            "compatibility.record_chronology",
            f"{label} must equal the authoritative journal record time",
        )


def require_plan_window(expires_at: str, recorded_at: str) -> None:
    """Require a future, bounded plan window from authoritative journal time."""

    expires = _timestamp(expires_at)
    recorded = _timestamp(recorded_at)
    if expires <= recorded:
        raise CompatibilityEventError(
            "compatibility.plan_expired",
            "compatibility plan must expire after its journal record time",
        )
    if expires - recorded > MAX_PLAN_LIFETIME:
        raise CompatibilityEventError(
            "compatibility.plan_lifetime",
            "compatibility plan lifetime cannot exceed seven days",
        )


def _feature_config_depth(value: Any, depth: int = 0) -> int:
    if depth > MAX_FEATURE_CONFIG_DEPTH:
        return depth
    if isinstance(value, dict):
        return max(
            (_feature_config_depth(item, depth + 1) for item in value.values()),
            default=depth,
        )
    if isinstance(value, list):
        return max(
            (_feature_config_depth(item, depth + 1) for item in value),
            default=depth,
        )
    return depth


def _normalized_feature_config(value: Mapping[str, Any]) -> dict[str, Any]:
    if _feature_config_depth(value) > MAX_FEATURE_CONFIG_DEPTH:
        raise CompatibilityEventError(
            "compatibility.feature_config_depth",
            "feature configuration is nested too deeply",
        )
    try:
        raw = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, UnicodeEncodeError, ValueError):
        raise CompatibilityEventError(
            "compatibility.feature_config",
            "feature configuration must contain finite JSON values",
        ) from None
    if len(raw) > MAX_FEATURE_CONFIG_BYTES:
        raise CompatibilityEventError(
            "compatibility.feature_config_size",
            f"feature configuration exceeds {MAX_FEATURE_CONFIG_BYTES} bytes",
        )
    return cast(dict[str, Any], json.loads(raw.decode("utf-8")))


def _unique_participants(
    participants: tuple[FrozenParticipant, ...], *, label: str
) -> None:
    identifiers = [participant.participant_id for participant in participants]
    if len(identifiers) != len(set(identifiers)):
        raise CompatibilityEventError(
            "compatibility.participant_duplicate",
            f"{label} contains a participant more than once",
        )


def _required_kernel_capability(capabilities: tuple[str, ...]) -> None:
    if COMPATIBILITY_KERNEL_CAPABILITY not in capabilities:
        raise CompatibilityEventError(
            "compatibility.kernel_capability",
            "compatibility gates must require compatibility.kernel/1",
        )


def _required_feature_capability(
    feature_id: str,
    feature_version: int,
    capabilities: tuple[str, ...],
) -> None:
    required = f"{feature_id}/{feature_version}"
    if required not in capabilities:
        raise CompatibilityEventError(
            "compatibility.feature_capability",
            f"compatibility gate must require its feature capability {required}",
        )


def validate_plan(attributes: Mapping[str, Any]) -> CompatibilityPlan:
    """Validate and normalize one inert upgrade-plan event."""

    _validate_schema(COMPATIBILITY_PLAN_EVENT, attributes)
    participants = tuple(
        FrozenParticipant(
            participant_id=cast(str, item["participant_id"]),
            binding_generation=cast(int, item["binding_generation"]),
        )
        for item in cast(list[dict[str, Any]], attributes["frozen_participants"])
    )
    _unique_participants(participants, label="frozen participant set")
    capabilities = tuple(sorted(cast(list[str], attributes["required_capabilities"])))
    _required_kernel_capability(capabilities)
    _required_feature_capability(
        cast(str, attributes["feature_id"]),
        cast(int, attributes["feature_version"]),
        capabilities,
    )
    return CompatibilityPlan(
        plan_id=cast(str, attributes["plan_id"]),
        feature_id=cast(str, attributes["feature_id"]),
        feature_version=cast(int, attributes["feature_version"]),
        feature_config=_normalized_feature_config(
            cast(Mapping[str, Any], attributes["feature_config"])
        ),
        validation_profile_sha256=cast(str, attributes["validation_profile_sha256"]),
        required_reader_epoch=cast(int, attributes["required_reader_epoch"]),
        required_capabilities=capabilities,
        frozen_participants=tuple(
            sorted(participants, key=lambda participant: participant.participant_id)
        ),
        expires_at=cast(str, attributes["expires_at"]),
        operator_reference=cast(str, attributes["operator_reference"]),
    )


def validate_readiness(attributes: Mapping[str, Any]) -> CompatibilityReadiness:
    """Validate and normalize one participant readiness event."""

    _validate_schema(COMPATIBILITY_READINESS_EVENT, attributes)
    return CompatibilityReadiness(
        plan_id=cast(str, attributes["plan_id"]),
        plan_record_id=cast(str, attributes["plan_record_id"]),
        plan_record_sha256=cast(str, attributes["plan_record_sha256"]),
        participant_id=cast(str, attributes["participant_id"]),
        binding_generation=cast(int, attributes["binding_generation"]),
        reader_epoch=cast(int, attributes["reader_epoch"]),
        capabilities=tuple(sorted(cast(list[str], attributes["capabilities"]))),
        validation_profile_sha256=cast(str, attributes["validation_profile_sha256"]),
        ready_at=cast(str, attributes["ready_at"]),
        operator_reference=cast(str, attributes["operator_reference"]),
    )


def validate_activation(attributes: Mapping[str, Any]) -> CompatibilityGate:
    """Validate and normalize the permanent fixed activation header."""

    _validate_schema(COMPATIBILITY_GATE_ACTIVATED_EVENT, attributes)
    references = tuple(
        ReadinessReference(
            participant_id=cast(str, item["participant_id"]),
            record_id=cast(str, item["record_id"]),
            record_sha256=cast(str, item["record_sha256"]),
        )
        for item in cast(list[dict[str, Any]], attributes["readiness"])
    )
    participant_ids = [reference.participant_id for reference in references]
    if len(participant_ids) != len(set(participant_ids)):
        raise CompatibilityEventError(
            "compatibility.participant_duplicate",
            "activation readiness contains a participant more than once",
        )
    capabilities = tuple(sorted(cast(list[str], attributes["required_capabilities"])))
    _required_kernel_capability(capabilities)
    _required_feature_capability(
        cast(str, attributes["feature_id"]),
        cast(int, attributes["feature_version"]),
        capabilities,
    )
    return CompatibilityGate(
        plan_id=cast(str, attributes["plan_id"]),
        plan_record_id=cast(str, attributes["plan_record_id"]),
        plan_record_sha256=cast(str, attributes["plan_record_sha256"]),
        feature_id=cast(str, attributes["feature_id"]),
        feature_version=cast(int, attributes["feature_version"]),
        validation_profile_sha256=cast(str, attributes["validation_profile_sha256"]),
        required_reader_epoch=cast(int, attributes["required_reader_epoch"]),
        required_capabilities=capabilities,
        readiness=tuple(
            sorted(references, key=lambda reference: reference.participant_id)
        ),
        activated_at=cast(str, attributes["activated_at"]),
        operator_reference=cast(str, attributes["operator_reference"]),
    )


def validate_event(
    event_type: str, attributes: Mapping[str, Any]
) -> CompatibilityPlan | CompatibilityReadiness | CompatibilityGate:
    """Validate one recognized compatibility event by its journal type."""

    if event_type == COMPATIBILITY_PLAN_EVENT:
        return validate_plan(attributes)
    if event_type == COMPATIBILITY_READINESS_EVENT:
        return validate_readiness(attributes)
    if event_type == COMPATIBILITY_GATE_ACTIVATED_EVENT:
        return validate_activation(attributes)
    raise CompatibilityEventError(
        "compatibility.event_type", "compatibility event type is unsupported"
    )


def require_reader_support(
    gate: CompatibilityGate,
    *,
    current_reader_epoch: int = CURRENT_READER_EPOCH,
    supported_capabilities: frozenset[str] = SUPPORTED_READER_CAPABILITIES,
) -> None:
    """Raise a structured upgrade diagnostic when a gate is unsupported."""

    missing = set(gate.required_capabilities) - supported_capabilities
    if gate.required_reader_epoch > current_reader_epoch or missing:
        raise CompatibilityUpgradeRequired(
            plan_id=gate.plan_id,
            feature_id=gate.feature_id,
            feature_version=gate.feature_version,
            validation_profile_sha256=gate.validation_profile_sha256,
            required_reader_epoch=gate.required_reader_epoch,
            required_capabilities=gate.required_capabilities,
            current_reader_epoch=current_reader_epoch,
            supported_capabilities=supported_capabilities,
        )


def _require_record_link(
    *,
    expected_id: str,
    expected_sha256: str,
    actual_id: str,
    actual_sha256: str,
    label: str,
) -> None:
    if expected_id != actual_id or expected_sha256 != actual_sha256:
        raise CompatibilityEventError(
            "compatibility.record_link",
            f"{label} does not match the referenced journal record",
        )


def _require_readiness_for_plan(
    staged: StagedReadiness,
    plan_record: StagedPlan,
) -> None:
    readiness = staged.readiness
    plan = plan_record.plan
    _require_record_link(
        expected_id=readiness.plan_record_id,
        expected_sha256=readiness.plan_record_sha256,
        actual_id=plan_record.record_id,
        actual_sha256=plan_record.record_sha256,
        label="readiness plan reference",
    )
    if readiness.plan_id != plan.plan_id:
        raise CompatibilityEventError(
            "compatibility.plan_mismatch",
            "readiness names a different compatibility plan",
        )
    frozen = {
        participant.participant_id: participant.binding_generation
        for participant in plan.frozen_participants
    }
    if frozen.get(readiness.participant_id) != readiness.binding_generation:
        raise CompatibilityEventError(
            "compatibility.participant_mismatch",
            "readiness participant or binding generation is outside the frozen plan",
        )
    if readiness.reader_epoch < plan.required_reader_epoch or not set(
        plan.required_capabilities
    ).issubset(readiness.capabilities):
        raise CompatibilityEventError(
            "compatibility.readiness_insufficient",
            "participant readiness does not satisfy the plan reader requirements",
        )
    if readiness.validation_profile_sha256 != plan.validation_profile_sha256:
        raise CompatibilityEventError(
            "compatibility.validation_profile_mismatch",
            "participant readiness was produced by a different validation profile",
        )
    if _timestamp(readiness.ready_at) > _timestamp(plan.expires_at):
        raise CompatibilityEventError(
            "compatibility.plan_expired",
            "participant readiness was recorded after the plan expired",
        )


def _require_roster_matches_plan(
    plan: CompatibilityPlan,
    participants: Mapping[str, Participant],
) -> None:
    if not plan.frozen_participants and participants:
        raise CompatibilityEventError(
            "compatibility.roster_drift",
            "an empty compatibility roster is valid only before participant history exists",
        )
    current: dict[str, int] = {}
    for participant_id, participant in participants.items():
        if participant.status == ParticipantStatus.RETIRED:
            continue
        if participant.status != ParticipantStatus.BOUND or participant.binding is None:
            raise CompatibilityEventError(
                "compatibility.roster_unbound",
                "every non-retired participant must be bound before gate activation",
            )
        current[participant_id] = participant.binding.generation
    frozen = {
        participant.participant_id: participant.binding_generation
        for participant in plan.frozen_participants
    }
    if current != frozen:
        raise CompatibilityEventError(
            "compatibility.roster_drift",
            "the active roster or a binding generation changed after planning",
        )


def _resolve_activation_plan(
    gate: CompatibilityGate,
    plans_by_record: Mapping[str, StagedPlan],
) -> StagedPlan:
    plan_record = plans_by_record.get(gate.plan_record_id)
    if plan_record is None:
        raise CompatibilityEventError(
            "compatibility.event_order",
            "gate activation precedes its referenced compatibility plan",
        )
    _require_record_link(
        expected_id=gate.plan_record_id,
        expected_sha256=gate.plan_record_sha256,
        actual_id=plan_record.record_id,
        actual_sha256=plan_record.record_sha256,
        label="activation plan reference",
    )
    plan = plan_record.plan
    if (
        gate.plan_id != plan.plan_id
        or gate.feature_id != plan.feature_id
        or gate.feature_version != plan.feature_version
        or gate.validation_profile_sha256 != plan.validation_profile_sha256
        or gate.required_reader_epoch != plan.required_reader_epoch
        or gate.required_capabilities != plan.required_capabilities
    ):
        raise CompatibilityEventError(
            "compatibility.plan_mismatch",
            "gate header does not match the referenced compatibility plan",
        )
    if _timestamp(gate.activated_at) > _timestamp(plan.expires_at):
        raise CompatibilityEventError(
            "compatibility.plan_expired",
            "compatibility gate was activated after the plan expired",
        )
    return plan_record


def _require_activation_readiness(
    gate: CompatibilityGate,
    plan_record: StagedPlan,
    readiness_by_record: Mapping[str, StagedReadiness],
) -> None:
    references = {reference.participant_id: reference for reference in gate.readiness}
    frozen_ids = {
        participant.participant_id
        for participant in plan_record.plan.frozen_participants
    }
    if set(references) != frozen_ids:
        raise CompatibilityEventError(
            "compatibility.readiness_incomplete",
            "activation must reference readiness for every frozen participant",
        )
    for participant_id, reference in references.items():
        staged = readiness_by_record.get(reference.record_id)
        if staged is None:
            raise CompatibilityEventError(
                "compatibility.event_order",
                "gate activation precedes referenced participant readiness",
            )
        _require_record_link(
            expected_id=reference.record_id,
            expected_sha256=reference.record_sha256,
            actual_id=staged.record_id,
            actual_sha256=staged.record_sha256,
            label="activation readiness reference",
        )
        if staged.readiness.participant_id != participant_id:
            raise CompatibilityEventError(
                "compatibility.participant_mismatch",
                "activation readiness reference names a different participant",
            )
        _require_readiness_for_plan(staged, plan_record)
        if _timestamp(staged.readiness.ready_at) > _timestamp(gate.activated_at):
            raise CompatibilityEventError(
                "compatibility.activation_chronology",
                "gate activation predates referenced participant readiness",
            )


@dataclass(slots=True)
class CompatibilityProjection:
    """Rebuildable compatibility staging data and active feature gates."""

    active_gates: dict[str, CompatibilityGate] = field(default_factory=dict)
    _plans_by_record: dict[str, StagedPlan] = field(default_factory=dict, repr=False)
    _plan_record_by_id: dict[str, str] = field(default_factory=dict, repr=False)
    _readiness_by_record: dict[str, StagedReadiness] = field(
        default_factory=dict, repr=False
    )

    def as_dict(self) -> dict[str, Any]:
        return {
            "format": COMPATIBILITY_FORMAT,
            "reader": {
                "epoch": CURRENT_READER_EPOCH,
                "capabilities": sorted(SUPPORTED_READER_CAPABILITIES),
            },
            "active_gates": [
                self.active_gates[feature_id].as_dict()
                for feature_id in sorted(self.active_gates)
            ],
        }

    def staged_plan(self, plan_id: str) -> StagedPlan | None:
        """Return one isolated staged plan selected by its semantic ID."""

        record_id = self._plan_record_by_id.get(plan_id)
        if record_id is None:
            return None
        return deepcopy(self._plans_by_record[record_id])

    def staged_plans(self) -> tuple[StagedPlan, ...]:
        """Return all staged plans in deterministic journal order."""

        return tuple(
            deepcopy(plan)
            for plan in sorted(
                self._plans_by_record.values(), key=lambda item: item.sequence
            )
        )

    def readiness_for_plan(self, plan_id: str) -> tuple[StagedReadiness, ...]:
        """Return isolated readiness records for one plan in journal order."""

        return tuple(
            deepcopy(readiness)
            for readiness in sorted(
                (
                    item
                    for item in self._readiness_by_record.values()
                    if item.readiness.plan_id == plan_id
                ),
                key=lambda item: item.sequence,
            )
        )

    def active_gate(self, feature_id: str) -> CompatibilityGate | None:
        """Return one isolated active gate, if the feature has been activated."""

        gate = self.active_gates.get(feature_id)
        return deepcopy(gate) if gate is not None else None

    def active_feature_config(self, feature_id: str) -> dict[str, Any] | None:
        """Resolve an active gate to its immutable staged feature configuration."""

        gate = self.active_gates.get(feature_id)
        if gate is None:
            return None
        plan_record = self._plans_by_record.get(gate.plan_record_id)
        if plan_record is None:
            raise CompatibilityEventError(
                "compatibility.plan_missing",
                "active gate no longer resolves to its staged plan",
            )
        return deepcopy(dict(plan_record.plan.feature_config))

    def observe_plan(
        self,
        attributes: Mapping[str, Any],
        *,
        record_id: str,
        record_sha256: str,
        sequence: int,
        recorded_at: str,
    ) -> CompatibilityPlan:
        plan = validate_plan(attributes)
        require_plan_window(plan.expires_at, recorded_at)
        prior_record = self._plan_record_by_id.get(plan.plan_id)
        if prior_record is not None:
            raise CompatibilityEventError(
                "compatibility.plan_conflict",
                "compatibility plan ID was already journaled",
            )
        staged = StagedPlan(deepcopy(plan), record_id, record_sha256, sequence)
        self._plans_by_record[record_id] = staged
        self._plan_record_by_id[plan.plan_id] = record_id
        return deepcopy(plan)

    def observe_readiness(
        self,
        attributes: Mapping[str, Any],
        *,
        record_id: str,
        record_sha256: str,
        sequence: int,
        recorded_at: str,
    ) -> CompatibilityReadiness:
        readiness = validate_readiness(attributes)
        _require_recorded_time(
            readiness.ready_at,
            recorded_at,
            label="ready_at",
        )
        plan_record = self._plans_by_record.get(readiness.plan_record_id)
        if plan_record is None:
            raise CompatibilityEventError(
                "compatibility.event_order",
                "participant readiness precedes its referenced plan",
            )
        staged = StagedReadiness(readiness, record_id, record_sha256, sequence)
        _require_readiness_for_plan(staged, plan_record)
        self._readiness_by_record[record_id] = staged
        return readiness

    def activate(
        self,
        attributes: Mapping[str, Any],
        *,
        participants: Mapping[str, Participant],
        recorded_at: str | None = None,
    ) -> CompatibilityGate:
        """Activate a supported gate for ordinary state replay or mutation."""

        return self._activate(
            attributes,
            participants=participants,
            recorded_at=recorded_at,
            enforce_reader_support=True,
        )

    def inspect_activation(
        self,
        attributes: Mapping[str, Any],
        *,
        participants: Mapping[str, Participant],
        recorded_at: str,
    ) -> CompatibilityGate:
        """Project a gate solely for compatibility reporting.

        This validates the fixed header, staging links, roster, readiness, and
        journal chronology but deliberately does not assert local reader
        support.  Callers must use a disposable inspection projection and MUST
        NOT use this result to authorize normal state replay or mutation.
        """

        return self._activate(
            attributes,
            participants=participants,
            recorded_at=recorded_at,
            enforce_reader_support=False,
        )

    def _activate(
        self,
        attributes: Mapping[str, Any],
        *,
        participants: Mapping[str, Participant],
        recorded_at: str | None,
        enforce_reader_support: bool,
    ) -> CompatibilityGate:
        gate = validate_activation(attributes)
        if recorded_at is not None:
            _require_recorded_time(
                gate.activated_at,
                recorded_at,
                label="activated_at",
            )
        plan_record = _resolve_activation_plan(gate, self._plans_by_record)
        _require_activation_readiness(
            gate,
            plan_record,
            self._readiness_by_record,
        )
        _require_roster_matches_plan(plan_record.plan, participants)
        prior = self.active_gates.get(gate.feature_id)
        kernel = self.active_gates.get(COMPATIBILITY_KERNEL_FEATURE_ID)
        if gate.feature_id != COMPATIBILITY_KERNEL_FEATURE_ID and kernel is None:
            raise CompatibilityEventError(
                "compatibility.kernel_inactive",
                "compatibility.kernel/1 must be active before another feature gate",
            )
        if (
            gate.feature_id == COMPATIBILITY_KERNEL_FEATURE_ID
            and prior is None
            and gate.feature_version != COMPATIBILITY_KERNEL_FEATURE_VERSION
        ):
            raise CompatibilityEventError(
                "compatibility.kernel_bootstrap",
                "the first compatibility kernel gate must activate version 1",
            )
        if prior == gate:
            return prior
        if prior is not None and gate.feature_version <= prior.feature_version:
            raise CompatibilityEventError(
                "compatibility.gate_conflict",
                "a feature gate can only advance to a higher feature version",
            )
        # Every link and invariant in the generic staging record is verified
        # before an unsupported header is reported as an active gate. The
        # feature configuration remains opaque to this permanent kernel.
        if enforce_reader_support:
            require_reader_support(gate)
        self.active_gates[gate.feature_id] = gate
        return gate


@dataclass(slots=True)
class CompatibilityInspection:
    """Narrow read-only compatibility view, never an ordinary state snapshot."""

    roster: ParticipantRoster
    compatibility: CompatibilityProjection
    journal_sequence: int = 0
    journal_record_sha256: str | None = None
    verified_journal_sequence: int = 0
    verified_journal_record_sha256: str | None = None
    upgrade_required: CompatibilityUpgradeRequired | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "format": "CAM-COMPATIBILITY-INSPECTION/1",
            "journal_position": {
                "sequence": self.journal_sequence,
                "record_sha256": self.journal_record_sha256,
            },
            "verified_journal_position": {
                "sequence": self.verified_journal_sequence,
                "record_sha256": self.verified_journal_record_sha256,
            },
            "compatibility": self.compatibility.as_dict(),
            "upgrade_required": (
                self.upgrade_required.as_dict()
                if self.upgrade_required is not None
                else None
            ),
        }
