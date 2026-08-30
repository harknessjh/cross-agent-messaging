# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Non-authoritative project participant roster for the audited local profile."""

from __future__ import annotations

import datetime as dt
import re
import uuid
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import PurePath
from typing import Any

from .protocol import CamUsageError

ROSTER_FORMAT = "CAM-PARTICIPANTS/1"
PARTICIPANT_NAME_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62})$")
VENDORS = frozenset({"codex", "claude-code"})
SUPPORTED_ROUTE_TRANSPORTS = frozenset({"codex_queue", "claude_send_message"})
VENDOR_ROUTE_TRANSPORT = {
    "codex": "codex_queue",
    "claude-code": "claude_send_message",
}
CLAUDE_ROUTE_PATTERN = re.compile(r"^(?P<name>.+) \[(?P<ref>[0-9a-fA-F]{6})\]$")
AGENT_VIEW_ID_PATTERN = re.compile(r"^[0-9a-fA-F]{8}$")


class ParticipantStatus(StrEnum):
    UNBOUND = "unbound"
    BOUND = "bound"
    STALE = "stale"
    RETIRED = "retired"


class RouteStatus(StrEnum):
    NOT_DISCOVERED = "not_discovered"
    CANDIDATE = "candidate"
    OPERATOR_CORRELATED = "operator_correlated"
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class SessionBinding:
    generation: int
    session_id: str
    session_label: str
    session_kind: str | None
    operator_reference: str
    bound_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "generation": self.generation,
            "session_id": self.session_id,
            "session_label": self.session_label,
            "session_kind": self.session_kind,
            "operator_reference": self.operator_reference,
            "bound_at": self.bound_at,
        }


@dataclass(frozen=True, slots=True)
class RouteObservation:
    transport: str
    address: str
    source: str
    observed_at: str
    binding_generation: int
    status: RouteStatus = RouteStatus.CANDIDATE
    agent_view_id: str | None = None
    list_agents_name: str | None = None
    list_agents_ref: str | None = None
    product_state: str | None = None
    agent_view_kind: str | None = None
    agent_view_started_at_ms: int | None = None
    session_git_top_level: str | None = None
    session_git_common_dir: str | None = None
    operator_reference: str | None = None
    confirmed_at: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "transport": self.transport,
            "address": self.address,
            "source": self.source,
            "observed_at": self.observed_at,
            "binding_generation": self.binding_generation,
            "status": self.status.value,
            "agent_view_id": self.agent_view_id,
            "list_agents_name": self.list_agents_name,
            "list_agents_ref": self.list_agents_ref,
            "product_state": self.product_state,
            "agent_view_kind": self.agent_view_kind,
            "agent_view_started_at_ms": self.agent_view_started_at_ms,
            "session_git_top_level": self.session_git_top_level,
            "session_git_common_dir": self.session_git_common_dir,
            "operator_reference": self.operator_reference,
            "confirmed_at": self.confirmed_at,
        }


@dataclass(frozen=True, slots=True)
class Participant:
    participant_id: str
    common_name: str
    display_name: str
    role: str
    vendor: str
    status: ParticipantStatus = ParticipantStatus.UNBOUND
    binding: SessionBinding | None = None
    route: RouteObservation | None = None
    invalidation_reason: str | None = None

    def as_dict(self, *, redact: bool = False) -> dict[str, Any]:
        binding = self.binding.as_dict() if self.binding is not None else None
        route = self.route.as_dict() if self.route is not None else None
        if redact and binding is not None:
            binding["session_id"] = "redacted"
            binding["session_label"] = "redacted"
            binding["session_kind"] = (
                "redacted" if binding["session_kind"] is not None else None
            )
            binding["operator_reference"] = "redacted"
        if redact and route is not None:
            for field_name in (
                "address",
                "source",
                "agent_view_id",
                "list_agents_name",
                "list_agents_ref",
                "product_state",
                "agent_view_kind",
                "agent_view_started_at_ms",
                "session_git_top_level",
                "session_git_common_dir",
                "operator_reference",
            ):
                route[field_name] = (
                    "redacted" if route[field_name] is not None else None
                )
        return {
            "participant_id": self.participant_id,
            "common_name": self.common_name,
            "display_name": self.display_name,
            "role": self.role,
            "vendor": self.vendor,
            "status": self.status.value,
            "binding": binding,
            "route": route,
            "invalidation_reason": self.invalidation_reason,
        }


def _bounded_text(
    value: Any,
    *,
    field_name: str,
    maximum: int,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str):
        raise CamUsageError(
            "roster.field",
            f"{field_name} must be a string",
        )
    if (not allow_empty and not value) or len(value) > maximum:
        raise CamUsageError(
            "roster.field",
            f"{field_name} is outside its allowed length",
        )
    return value


def _canonical_identifier(value: Any, *, field_name: str) -> str:
    text = _bounded_text(value, field_name=field_name, maximum=128)
    try:
        return str(uuid.UUID(text))
    except (ValueError, AttributeError):
        raise CamUsageError(
            "roster.identifier",
            f"{field_name} must be a valid UUID",
        ) from None


def _optional_text(value: Any, *, field_name: str, maximum: int) -> str | None:
    if value is None:
        return None
    return _bounded_text(value, field_name=field_name, maximum=maximum)


def _optional_nonnegative_int(value: Any, *, field_name: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 0 or value > 10**16:
        raise CamUsageError(
            "roster.field",
            f"{field_name} must be a bounded non-negative integer or null",
        )
    return value


def _timestamp(value: Any, *, field_name: str) -> str:
    text = _bounded_text(value, field_name=field_name, maximum=64)
    if not text.endswith("Z"):
        raise CamUsageError("roster.timestamp", f"{field_name} must be UTC")
    try:
        parsed = dt.datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError:
        raise CamUsageError(
            "roster.timestamp", f"{field_name} must be a valid UTC timestamp"
        ) from None
    if parsed.utcoffset() != dt.timedelta(0):
        raise CamUsageError("roster.timestamp", f"{field_name} must be UTC")
    return text


def _parsed_timestamp(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value[:-1] + "+00:00")


def _participant_name(value: Any) -> str:
    text = _bounded_text(value, field_name="common_name", maximum=63)
    if not PARTICIPANT_NAME_PATTERN.fullmatch(text):
        raise CamUsageError(
            "roster.common_name",
            "common name must be lowercase letters, digits, or hyphens",
        )
    return text


@dataclass(slots=True)
class ParticipantRoster:
    """Mutable current projection; journal records remain the history."""

    project_id: str
    participants: dict[str, Participant] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.project_id = _canonical_identifier(
            self.project_id, field_name="project_id"
        )

    def _select(self, selector: str) -> Participant:
        matches = [
            participant
            for participant in self.participants.values()
            if selector in {participant.participant_id, participant.common_name}
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise CamUsageError(
                "roster.participant_ambiguous",
                "participant selector matches more than one roster entry",
            )
        raise CamUsageError("roster.participant_unknown", "participant is not known")

    def select(self, selector: str) -> Participant:
        """Return one participant by stable ID or project-local common name."""

        return self._select(selector)

    def _ensure_session_available(
        self,
        participant: Participant,
        session_id: str,
    ) -> None:
        conflict = any(
            candidate.participant_id != participant.participant_id
            and candidate.status != ParticipantStatus.RETIRED
            and candidate.vendor == participant.vendor
            and candidate.binding is not None
            and candidate.binding.session_id == session_id
            for candidate in self.participants.values()
        )
        if conflict:
            raise CamUsageError(
                "roster.session_conflict",
                "session is already bound to another participant",
            )

    def _ensure_route_available(
        self,
        participant: Participant,
        *,
        transport: str,
        address: str,
    ) -> None:
        conflict = any(
            candidate.participant_id != participant.participant_id
            and candidate.status != ParticipantStatus.RETIRED
            and candidate.route is not None
            and candidate.route.transport == transport
            and candidate.route.address == address
            for candidate in self.participants.values()
        )
        if conflict:
            raise CamUsageError(
                "roster.route_conflict",
                "live route is already associated with another participant",
            )

    def add(
        self,
        *,
        common_name: str,
        display_name: str,
        role: str,
        vendor: str,
        participant_id: str | None = None,
    ) -> Participant:
        name = _participant_name(common_name)
        if any(
            participant.common_name.casefold() == name.casefold()
            or participant.participant_id == name
            for participant in self.participants.values()
        ):
            raise CamUsageError(
                "roster.name_conflict",
                "common name is already assigned in this project",
            )
        if vendor not in VENDORS:
            raise CamUsageError("roster.vendor", "participant vendor is unsupported")
        identifier = _canonical_identifier(
            participant_id or str(uuid.uuid4()), field_name="participant_id"
        )
        if identifier in self.participants:
            raise CamUsageError(
                "roster.identifier_conflict",
                "participant identifier already exists",
            )
        if any(
            participant.common_name == identifier
            for participant in self.participants.values()
        ):
            raise CamUsageError(
                "roster.identifier_conflict",
                "participant identifier conflicts with an existing common name",
            )
        participant = Participant(
            participant_id=identifier,
            common_name=name,
            display_name=_bounded_text(
                display_name, field_name="display_name", maximum=128
            ),
            role=_bounded_text(role, field_name="role", maximum=512),
            vendor=vendor,
        )
        self.participants[identifier] = participant
        return participant

    def bind(
        self,
        selector: str,
        *,
        session_id: str,
        session_label: str,
        session_kind: str | None,
        operator_reference: str,
        bound_at: str,
    ) -> Participant:
        current = self._select(selector)
        if current.status == ParticipantStatus.RETIRED:
            raise CamUsageError(
                "roster.participant_retired",
                "retired participant cannot be rebound",
            )
        opaque_session = _canonical_identifier(session_id, field_name="session_id")
        self._ensure_session_available(current, opaque_session)
        generation = 1 if current.binding is None else current.binding.generation + 1
        binding = SessionBinding(
            generation=generation,
            session_id=opaque_session,
            session_label=_bounded_text(
                session_label, field_name="session_label", maximum=256
            ),
            session_kind=_optional_text(
                session_kind,
                field_name="session_kind",
                maximum=64,
            ),
            operator_reference=_bounded_text(
                operator_reference,
                field_name="operator_reference",
                maximum=1024,
            ),
            bound_at=_timestamp(bound_at, field_name="bound_at"),
        )
        updated = replace(
            current,
            status=ParticipantStatus.BOUND,
            binding=binding,
            route=None,
            invalidation_reason=None,
        )
        self.participants[current.participant_id] = updated
        return updated

    def observe_route(
        self,
        selector: str,
        *,
        transport: str,
        address: str,
        source: str,
        observed_at: str,
        agent_view_id: str | None = None,
        list_agents_name: str | None = None,
        list_agents_ref: str | None = None,
        product_state: str | None = None,
        agent_view_kind: str | None = None,
        agent_view_started_at_ms: int | None = None,
        session_git_top_level: str | None = None,
        session_git_common_dir: str | None = None,
    ) -> Participant:
        current = self._select(selector)
        if current.binding is None or current.status == ParticipantStatus.RETIRED:
            raise CamUsageError(
                "roster.participant_unbound",
                "participant must have an active session binding",
            )
        if transport not in SUPPORTED_ROUTE_TRANSPORTS:
            raise CamUsageError(
                "roster.transport",
                "only supported product messaging transports may be recorded",
            )
        if transport != VENDOR_ROUTE_TRANSPORT[current.vendor]:
            raise CamUsageError(
                "roster.transport",
                "route transport does not match the participant vendor",
            )
        route_address = _bounded_text(address, field_name="address", maximum=512)
        observed = _timestamp(observed_at, field_name="observed_at")
        if _parsed_timestamp(observed) < _parsed_timestamp(current.binding.bound_at):
            raise CamUsageError(
                "roster.timestamp", "route observation predates the session binding"
            )
        if current.vendor == "codex":
            if _canonical_identifier(route_address, field_name="address") != (
                current.binding.session_id
            ):
                raise CamUsageError(
                    "roster.route_identity",
                    "Codex queue address must equal the bound session UUID",
                )
        else:
            matched = CLAUDE_ROUTE_PATTERN.fullmatch(route_address)
            if matched is None:
                raise CamUsageError(
                    "roster.route_identity",
                    "Claude route must be a qualified ListAgents name and ref",
                )
            if (
                (
                    agent_view_id is not None
                    and (
                        AGENT_VIEW_ID_PATTERN.fullmatch(agent_view_id) is None
                        or agent_view_id.lower()
                        != current.binding.session_id.split("-", 1)[0]
                    )
                )
                or list_agents_name != matched.group("name")
                or list_agents_ref is None
                or list_agents_ref.lower() != matched.group("ref").lower()
            ):
                raise CamUsageError(
                    "roster.route_identity",
                    "Claude route must correlate the bound session and ListAgents "
                    "identifiers; an observed Agent View id must match the session",
                )
        top_level = _optional_text(
            session_git_top_level,
            field_name="session_git_top_level",
            maximum=4_096,
        )
        common_dir = _optional_text(
            session_git_common_dir,
            field_name="session_git_common_dir",
            maximum=4_096,
        )
        if top_level is not None and not PurePath(top_level).is_absolute():
            raise CamUsageError(
                "roster.route_evidence",
                "session Git top-level evidence must be an absolute path",
            )
        if common_dir is not None and not PurePath(common_dir).is_absolute():
            raise CamUsageError(
                "roster.route_evidence",
                "session Git common-directory evidence must be an absolute path",
            )
        route_evidence = (
            agent_view_kind,
            agent_view_started_at_ms,
            top_level,
            common_dir,
        )
        if any(value is not None for value in route_evidence) and any(
            value is None for value in route_evidence
        ):
            raise CamUsageError(
                "roster.route_evidence",
                "Agent View route evidence must be recorded as one complete set",
            )
        self._ensure_route_available(
            current,
            transport=transport,
            address=route_address,
        )
        preserve_correlation = (
            current.status == ParticipantStatus.BOUND
            and current.route is not None
            and current.route.status == RouteStatus.OPERATOR_CORRELATED
            and current.route.binding_generation == current.binding.generation
            and current.route.transport == transport
            and current.route.address == route_address
            and current.route.agent_view_id == agent_view_id
            and current.route.list_agents_name == list_agents_name
            and current.route.list_agents_ref == list_agents_ref
            and current.route.agent_view_kind == agent_view_kind
            and current.route.agent_view_started_at_ms == agent_view_started_at_ms
            and current.route.session_git_top_level == top_level
            and current.route.session_git_common_dir == common_dir
        )
        route = RouteObservation(
            transport=transport,
            address=route_address,
            source=_bounded_text(source, field_name="source", maximum=128),
            observed_at=observed,
            binding_generation=current.binding.generation,
            agent_view_id=_optional_text(
                agent_view_id,
                field_name="agent_view_id",
                maximum=64,
            ),
            list_agents_name=_optional_text(
                list_agents_name,
                field_name="list_agents_name",
                maximum=256,
            ),
            list_agents_ref=_optional_text(
                list_agents_ref,
                field_name="list_agents_ref",
                maximum=64,
            ),
            product_state=_optional_text(
                product_state,
                field_name="product_state",
                maximum=64,
            ),
            agent_view_kind=_optional_text(
                agent_view_kind,
                field_name="agent_view_kind",
                maximum=64,
            ),
            agent_view_started_at_ms=_optional_nonnegative_int(
                agent_view_started_at_ms,
                field_name="agent_view_started_at_ms",
            ),
            session_git_top_level=top_level,
            session_git_common_dir=common_dir,
            status=(
                RouteStatus.OPERATOR_CORRELATED
                if preserve_correlation
                else RouteStatus.CANDIDATE
            ),
            operator_reference=(
                current.route.operator_reference if preserve_correlation else None
            ),
            confirmed_at=(current.route.confirmed_at if preserve_correlation else None),
        )
        updated = replace(
            current,
            status=ParticipantStatus.BOUND,
            route=route,
            invalidation_reason=None,
        )
        self.participants[current.participant_id] = updated
        return updated

    def confirm_route(
        self,
        selector: str,
        *,
        expected_address: str,
        operator_reference: str,
        confirmed_at: str,
    ) -> Participant:
        current = self._select(selector)
        if current.status == ParticipantStatus.RETIRED:
            raise CamUsageError(
                "roster.participant_retired",
                "retired participant route cannot be confirmed",
            )
        if current.binding is None or current.route is None:
            raise CamUsageError(
                "roster.route_missing",
                "participant has no observed route to confirm",
            )
        if current.status != ParticipantStatus.BOUND or (
            current.route.status != RouteStatus.CANDIDATE
        ):
            raise CamUsageError(
                "roster.route_not_candidate",
                "only the current candidate route may be confirmed",
            )
        if current.route.address != expected_address:
            raise CamUsageError(
                "roster.route_changed",
                "observed route does not equal the confirmed candidate",
            )
        route = replace(
            current.route,
            status=RouteStatus.OPERATOR_CORRELATED,
            operator_reference=_bounded_text(
                operator_reference,
                field_name="operator_reference",
                maximum=1024,
            ),
            confirmed_at=_timestamp(confirmed_at, field_name="confirmed_at"),
        )
        if _parsed_timestamp(route.confirmed_at) < _parsed_timestamp(route.observed_at):
            raise CamUsageError(
                "roster.timestamp", "route confirmation predates its observation"
            )
        updated = replace(
            current,
            status=ParticipantStatus.BOUND,
            route=route,
            invalidation_reason=None,
        )
        self.participants[current.participant_id] = updated
        return updated

    def invalidate(self, selector: str, *, reason: str) -> Participant:
        current = self._select(selector)
        if current.status == ParticipantStatus.RETIRED:
            raise CamUsageError(
                "roster.participant_retired",
                "retired participant cannot be invalidated",
            )
        route = current.route
        if route is not None:
            route = replace(
                route,
                status=RouteStatus.STALE,
                operator_reference=None,
                confirmed_at=None,
            )
        updated = replace(
            current,
            status=ParticipantStatus.STALE,
            route=route,
            invalidation_reason=_bounded_text(
                reason, field_name="invalidation_reason", maximum=512
            ),
        )
        self.participants[current.participant_id] = updated
        return updated

    def retire(self, selector: str, *, reason: str) -> Participant:
        current = self.invalidate(selector, reason=reason)
        updated = replace(current, status=ParticipantStatus.RETIRED)
        self.participants[current.participant_id] = updated
        return updated

    def require_correlated_route(self, selector: str) -> RouteObservation:
        current = self._select(selector)
        if (
            current.status != ParticipantStatus.BOUND
            or current.binding is None
            or current.route is None
            or current.route.status != RouteStatus.OPERATOR_CORRELATED
            or current.route.binding_generation != current.binding.generation
        ):
            raise CamUsageError(
                "roster.route_not_ready",
                "participant requires fresh route resolution and correlation",
            )
        return current.route

    def as_dict(self, *, redact: bool = False) -> dict[str, Any]:
        ordered = sorted(
            self.participants.values(),
            key=lambda participant: (
                participant.common_name,
                participant.participant_id,
            ),
        )
        return {
            "format": ROSTER_FORMAT,
            "project_id": self.project_id,
            "participants": [
                participant.as_dict(redact=redact) for participant in ordered
            ],
        }
