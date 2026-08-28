# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Journal-backed participant and lifecycle state.

The append-only project journal is the only source of truth. The current JSON
document written by this module is a disposable projection: callers may delete
it and rebuild it from the journal at any time.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from .journal import (
    _verified_records_for_transaction,
    append_record,
    decode_exact_message,
)
from .lifecycle import ROOT_TYPES, LifecycleEntry, LifecycleProjection, LifecycleState
from .participants import Participant, ParticipantRoster
from .project import (
    ProjectBinding,
    ProjectError,
    ProjectTransaction,
    _transaction_cache,
    project_transaction,
    replace_private_json,
    require_project_transaction,
)
from .protocol import (
    REPLY_TYPES,
    CamUsageError,
    CamValidationError,
    ValidationPolicy,
    parse_exact_bytes,
)
from .validation import validate_exact_bytes

STATE_PROJECTION_NAME = "state-current.json"

PARTICIPANT_ADDED = "state.participant.added"
PARTICIPANT_BOUND = "state.participant.bound"
PARTICIPANT_ROUTE_OBSERVED = "state.participant.route_observed"
PARTICIPANT_ROUTE_CONFIRMED = "state.participant.route_confirmed"
PARTICIPANT_INVALIDATED = "state.participant.invalidated"
PARTICIPANT_RETIRED = "state.participant.retired"
LIFECYCLE_ROOT_REGISTERED = "state.lifecycle.root_registered"
LIFECYCLE_REPLY_APPLIED = "state.lifecycle.reply_applied"
LIFECYCLE_EXPIRED_UNCONFIRMED = "state.lifecycle.expired_unconfirmed"

STATE_EVENT_TYPES = frozenset(
    {
        PARTICIPANT_ADDED,
        PARTICIPANT_BOUND,
        PARTICIPANT_ROUTE_OBSERVED,
        PARTICIPANT_ROUTE_CONFIRMED,
        PARTICIPANT_INVALIDATED,
        PARTICIPANT_RETIRED,
        LIFECYCLE_ROOT_REGISTERED,
        LIFECYCLE_REPLY_APPLIED,
        LIFECYCLE_EXPIRED_UNCONFIRMED,
    }
)

_HISTORICAL_VALIDATION_POLICY = ValidationPolicy(allow_expired=True)
_STATE_CACHE_KEY = "cam1.state.snapshot"


class StateError(ProjectError):
    """Fail-closed journal projection or domain-event failure."""


class ProjectionRefreshError(StateError):
    """A journal event committed but its disposable cache refresh failed."""

    def __init__(self, *, record_id: str, sequence: int):
        self.record_id = record_id
        self.sequence = sequence
        super().__init__(
            "state.projection_refresh",
            f"journal event committed at sequence {sequence}; rebuild projections and do not retry",
        )


@dataclass(slots=True)
class StateSnapshot:
    """Rebuilt in-memory state plus its source-journal position."""

    roster: ParticipantRoster
    lifecycle: LifecycleProjection
    journal_sequence: int = 0
    journal_record_sha256: str | None = None
    _message_bytes: dict[str, bytes] = field(default_factory=dict, repr=False)
    _nonce_owners: dict[str, str] = field(default_factory=dict, repr=False)
    _nonce_echoes: dict[str, str] = field(default_factory=dict, repr=False)

    def projection_document(self) -> dict[str, Any]:
        return {
            "format": "CAM-STATE/1",
            "project_id": self.roster.project_id,
            "journal_position": self._journal_position(),
            "participants": self.roster.as_dict()["participants"],
            "lifecycle": self.lifecycle.as_dict()["entries"],
        }

    def _journal_position(self) -> dict[str, Any]:
        return {
            "sequence": self.journal_sequence,
            "record_sha256": self.journal_record_sha256,
        }


@dataclass(frozen=True, slots=True)
class LifecyclePlan:
    """Prospectively validated lifecycle event for one held transaction."""

    project_id: str
    event_type: str
    attributes: Mapping[str, Any]
    exact_message: bytes
    recorded_at: dt.datetime
    preview: LifecycleEntry
    duplicate: bool
    freshness_deadline: str


def require_plan_freshness(
    plan: LifecyclePlan,
    *,
    now: dt.datetime | None = None,
) -> None:
    """Fail if a prepared observation crossed its final freshness deadline."""

    _, observed_at = _event_time(now)
    if _validation_time(observed_at) >= _validation_time(plan.freshness_deadline):
        raise CamUsageError(
            "state.observation_expired",
            "message freshness deadline passed during local validation",
        )


def state_projection_path(project: ProjectBinding) -> Path:
    return project.project_dir / STATE_PROJECTION_NAME


def _state_error(code: str, detail: str) -> StateError:
    return StateError(code, detail)


def _canonical_uuid(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise CamUsageError(
            "state.identifier",
            f"{field_name} must be a UUID string",
        )
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError):
        raise CamUsageError(
            "state.identifier",
            f"{field_name} must be a valid UUID",
        ) from None


def _uuid_values_equal(left: Any, right: Any) -> bool:
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    try:
        return uuid.UUID(left) == uuid.UUID(right)
    except (ValueError, AttributeError):
        return False


def _attributes(
    supplied: Mapping[str, Any],
    *,
    required: frozenset[str],
) -> dict[str, Any]:
    values = dict(supplied)
    if set(values) != required:
        raise _state_error(
            "state.event_attributes",
            "state event attributes do not match the event contract",
        )
    return values


def _required_text(values: Mapping[str, Any], name: str) -> str:
    value = values.get(name)
    if not isinstance(value, str) or not value:
        raise _state_error(
            "state.event_attributes",
            f"state event attribute {name} must be a non-empty string",
        )
    return value


def _optional_text(values: Mapping[str, Any], name: str) -> str | None:
    value = values.get(name)
    if value is not None and not isinstance(value, str):
        raise _state_error(
            "state.event_attributes",
            f"state event attribute {name} must be a string or null",
        )
    return cast(str | None, value)


def _require_no_message(exact_message: bytes | None) -> None:
    if exact_message is not None:
        raise _state_error(
            "state.event_message",
            "participant and marker events must not contain message bytes",
        )


def _require_message(exact_message: bytes | None) -> bytes:
    if exact_message is None:
        raise _state_error(
            "state.event_message",
            "lifecycle message event is missing exact message bytes",
        )
    return exact_message


def _validation_time(value: str) -> dt.datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise CamUsageError("state.timestamp", "observed_at must be a UTC timestamp")
    try:
        parsed = dt.datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise CamUsageError(
            "state.timestamp", "observed_at must be a valid UTC timestamp"
        ) from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CamUsageError("state.timestamp", "observed_at must be timezone-aware")
    return parsed.astimezone(dt.UTC)


def _current_utc_time() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _event_time(value: dt.datetime | None) -> tuple[dt.datetime, str]:
    observed = value or _current_utc_time()
    if observed.tzinfo is None or observed.utcoffset() is None:
        raise CamUsageError(
            "state.timestamp",
            "state event time must be timezone-aware",
        )
    normalized = observed.astimezone(dt.UTC)
    timespec = "microseconds" if normalized.microsecond else "seconds"
    return normalized, normalized.isoformat(timespec=timespec).replace("+00:00", "Z")


def _validate_message(
    raw: bytes,
    *,
    observed_at: str,
    against_raw: bytes | None = None,
    allow_expired: bool = False,
) -> dict[str, Any]:
    result = validate_exact_bytes(
        raw,
        against_raw=against_raw,
        now=_validation_time(observed_at),
        policy=(_HISTORICAL_VALIDATION_POLICY if allow_expired else ValidationPolicy()),
    )
    return result.envelope


def _remember_message(snapshot: StateSnapshot, message_id: str, raw: bytes) -> None:
    prior = snapshot._message_bytes.get(message_id)
    if prior is not None and prior != raw:
        raise CamUsageError(
            "state.message_conflict",
            "message ID was reused with different exact bytes",
        )
    snapshot._message_bytes[message_id] = raw


def _record_nonce(
    snapshot: StateSnapshot,
    envelope: Mapping[str, Any],
    *,
    root: bool,
) -> None:
    nonce = envelope.get("nonce")
    if nonce is None:
        return
    if not isinstance(nonce, str):
        raise CamUsageError("state.nonce", "message nonce is invalid")
    message_id = _canonical_uuid(envelope.get("message_id"), field_name="message_id")
    if root:
        owner = snapshot._nonce_owners.get(nonce)
        if owner is not None and owner != message_id:
            raise CamUsageError(
                "state.nonce_reuse", "nonce is already owned by another root"
            )
        snapshot._nonce_owners[nonce] = message_id
        return
    root_id = _canonical_uuid(envelope.get("in_reply_to"), field_name="in_reply_to")
    if snapshot._nonce_owners.get(nonce) != root_id:
        raise CamUsageError(
            "state.nonce_reuse", "reply nonce is not owned by its correlated root"
        )
    prior_echo = snapshot._nonce_echoes.get(root_id)
    if prior_echo is not None and prior_echo != message_id:
        raise CamUsageError(
            "state.nonce_reuse", "root nonce was already echoed by another reply"
        )
    snapshot._nonce_echoes[root_id] = message_id


def _endpoint_identity(endpoint: Any) -> tuple[Any, ...]:
    if not isinstance(endpoint, dict):
        raise CamUsageError("lifecycle.cancel_identity", "cancel endpoint is invalid")
    vendor = endpoint.get("vendor")
    host_id = endpoint.get("host_id")
    session_id = endpoint.get("session_id")
    if isinstance(session_id, str) and session_id:
        try:
            session_identity = str(uuid.UUID(session_id))
        except ValueError:
            session_identity = session_id
        return (vendor, host_id, "session", session_identity)
    return (vendor, host_id, "name", endpoint.get("agent_name"))


def _normalized_scope(action: Any) -> dict[str, tuple[str, ...]]:
    if not isinstance(action, dict) or not isinstance(action.get("scope"), dict):
        raise CamUsageError("lifecycle.cancel_scope", "cancel scope is invalid")
    scope = action["scope"]
    normalized: dict[str, tuple[str, ...]] = {}
    for name, values in scope.items():
        if (
            not isinstance(name, str)
            or not isinstance(values, list)
            or not all(isinstance(value, str) for value in values)
        ):
            raise CamUsageError("lifecycle.cancel_scope", "cancel scope is invalid")
        normalized[name] = tuple(sorted(values))
    return normalized


def _require_cancel_identity(
    cancel: Mapping[str, Any], request: Mapping[str, Any]
) -> None:
    sender_matches = _endpoint_identity(cancel.get("claimed_sender")) == (
        _endpoint_identity(request.get("claimed_sender"))
    )
    recipient_matches = _endpoint_identity(cancel.get("recipient")) == (
        _endpoint_identity(request.get("recipient"))
    )
    if not sender_matches or not recipient_matches:
        raise CamUsageError(
            "lifecycle.cancel_identity",
            "cancel sender or recipient does not match the preserved request",
        )


def _require_cancel_action(
    cancel: Mapping[str, Any], request: Mapping[str, Any]
) -> None:
    cancel_action = cancel.get("action")
    request_action = request.get("action")
    if not isinstance(cancel_action, dict) or not isinstance(request_action, dict):
        raise CamUsageError("lifecycle.cancel_action", "cancel action is invalid")
    changed = (
        cancel_action.get("operation") != "cancel"
        or cancel_action.get("risk_class") != request_action.get("risk_class")
        or _normalized_scope(cancel_action) != _normalized_scope(request_action)
        or cancel.get("constraints") != request.get("constraints")
    )
    if changed:
        raise CamUsageError(
            "lifecycle.cancel_action",
            "cancel changes the request risk, scope, or constraints",
        )


def _validate_cancel_against_request(
    cancel: Mapping[str, Any],
    request: Mapping[str, Any],
) -> None:
    if request.get("type") != "request":
        raise CamUsageError(
            "lifecycle.cancel_target",
            "cancel must target a request root",
        )
    try:
        target_id = str(uuid.UUID(cast(str, cancel.get("in_reply_to"))))
        request_id = str(uuid.UUID(cast(str, request.get("message_id"))))
    except (ValueError, TypeError, AttributeError):
        raise CamUsageError(
            "lifecycle.cancel_target",
            "cancel target identifier is invalid",
        ) from None
    if target_id != request_id:
        raise CamUsageError(
            "lifecycle.cancel_target",
            "cancel does not reference the preserved request",
        )
    _require_cancel_identity(cancel, request)
    _require_cancel_action(cancel, request)
    authorization = cancel.get("authorization")
    if not isinstance(authorization, dict) or authorization.get("basis") != (
        "operator_confirmation"
    ):
        raise CamUsageError(
            "lifecycle.cancel_authorization",
            "cancel requires fresh operator confirmation",
        )
    cancel_sent_at = cast(str, cancel.get("sent_at"))
    request_sent_at = cast(str, request.get("sent_at"))
    if _validation_time(cancel_sent_at) < _validation_time(request_sent_at):
        raise CamUsageError(
            "lifecycle.cancel_chronology",
            "cancel predates the preserved request",
        )


def _participant_add(
    snapshot: StateSnapshot, attributes: Mapping[str, Any], exact_message: bytes | None
) -> Participant:
    _require_no_message(exact_message)
    values = _attributes(
        attributes,
        required=frozenset(
            {"participant_id", "common_name", "display_name", "role", "vendor"}
        ),
    )
    return snapshot.roster.add(
        participant_id=_required_text(values, "participant_id"),
        common_name=_required_text(values, "common_name"),
        display_name=_required_text(values, "display_name"),
        role=_required_text(values, "role"),
        vendor=_required_text(values, "vendor"),
    )


def _participant_bind(
    snapshot: StateSnapshot, attributes: Mapping[str, Any], exact_message: bytes | None
) -> Participant:
    _require_no_message(exact_message)
    values = _attributes(
        attributes,
        required=frozenset(
            {
                "participant_id",
                "session_id",
                "session_label",
                "session_kind",
                "operator_reference",
                "bound_at",
            }
        ),
    )
    return snapshot.roster.bind(
        _required_text(values, "participant_id"),
        session_id=_required_text(values, "session_id"),
        session_label=_required_text(values, "session_label"),
        session_kind=_optional_text(values, "session_kind"),
        operator_reference=_required_text(values, "operator_reference"),
        bound_at=_required_text(values, "bound_at"),
    )


def _participant_route_observed(
    snapshot: StateSnapshot, attributes: Mapping[str, Any], exact_message: bytes | None
) -> Participant:
    _require_no_message(exact_message)
    legacy_fields = frozenset(
        {
            "participant_id",
            "transport",
            "address",
            "source",
            "observed_at",
            "agent_view_id",
            "list_agents_name",
            "list_agents_ref",
            "product_state",
        }
    )
    evidence_fields = frozenset(
        {
            "agent_view_kind",
            "agent_view_started_at_ms",
            "session_git_top_level",
            "session_git_common_dir",
        }
    )
    if set(attributes) == legacy_fields:
        values = dict(attributes)
        values.update(dict.fromkeys(evidence_fields))
    else:
        values = _attributes(attributes, required=legacy_fields | evidence_fields)
    return snapshot.roster.observe_route(
        _required_text(values, "participant_id"),
        transport=_required_text(values, "transport"),
        address=_required_text(values, "address"),
        source=_required_text(values, "source"),
        observed_at=_required_text(values, "observed_at"),
        agent_view_id=_optional_text(values, "agent_view_id"),
        list_agents_name=_optional_text(values, "list_agents_name"),
        list_agents_ref=_optional_text(values, "list_agents_ref"),
        product_state=_optional_text(values, "product_state"),
        agent_view_kind=_optional_text(values, "agent_view_kind"),
        agent_view_started_at_ms=values.get("agent_view_started_at_ms"),
        session_git_top_level=_optional_text(values, "session_git_top_level"),
        session_git_common_dir=_optional_text(values, "session_git_common_dir"),
    )


def _participant_route_confirmed(
    snapshot: StateSnapshot, attributes: Mapping[str, Any], exact_message: bytes | None
) -> Participant:
    _require_no_message(exact_message)
    values = _attributes(
        attributes,
        required=frozenset(
            {"participant_id", "expected_address", "operator_reference", "confirmed_at"}
        ),
    )
    return snapshot.roster.confirm_route(
        _required_text(values, "participant_id"),
        expected_address=_required_text(values, "expected_address"),
        operator_reference=_required_text(values, "operator_reference"),
        confirmed_at=_required_text(values, "confirmed_at"),
    )


def _participant_invalidated(
    snapshot: StateSnapshot, attributes: Mapping[str, Any], exact_message: bytes | None
) -> Participant:
    _require_no_message(exact_message)
    values = _attributes(
        attributes,
        required=frozenset({"participant_id", "reason"}),
    )
    return snapshot.roster.invalidate(
        _required_text(values, "participant_id"),
        reason=_required_text(values, "reason"),
    )


def _participant_retired(
    snapshot: StateSnapshot, attributes: Mapping[str, Any], exact_message: bytes | None
) -> Participant:
    _require_no_message(exact_message)
    values = _attributes(
        attributes,
        required=frozenset({"participant_id", "reason"}),
    )
    return snapshot.roster.retire(
        _required_text(values, "participant_id"),
        reason=_required_text(values, "reason"),
    )


def _lifecycle_root_registered(
    snapshot: StateSnapshot, attributes: Mapping[str, Any], exact_message: bytes | None
) -> LifecycleEntry:
    values = _attributes(
        attributes,
        required=frozenset(
            {"root_message_id", "root_type", "renewal_of", "observed_at"}
        ),
    )
    raw = _require_message(exact_message)
    observed_at = _required_text(values, "observed_at")
    envelope = _validate_message(
        raw,
        observed_at=observed_at,
        allow_expired=True,
    )
    if envelope.get("type") == "cancel":
        target_id = _canonical_uuid(
            envelope.get("in_reply_to"), field_name="in_reply_to"
        )
        target_raw = snapshot._message_bytes.get(target_id)
        if target_raw is None:
            raise _state_error(
                "state.event_order",
                "cancel precedes its preserved request root",
            )
        request = _validate_message(
            target_raw,
            observed_at=observed_at,
            allow_expired=True,
        )
        _validate_cancel_against_request(envelope, request)
    _record_nonce(snapshot, envelope, root=True)
    renewal_of = _optional_text(values, "renewal_of")
    canonical_renewal = (
        _canonical_uuid(renewal_of, field_name="renewal_of")
        if renewal_of is not None
        else None
    )
    entry = snapshot.lifecycle.register_root(
        envelope,
        observed_at=observed_at,
        renewal_of=canonical_renewal,
    )
    event_root_id = _canonical_uuid(
        _required_text(values, "root_message_id"),
        field_name="root_message_id",
    )
    if (
        entry.root_message_id != event_root_id
        or entry.root_type != _required_text(values, "root_type")
        or (canonical_renewal is not None and entry.renewal_of != canonical_renewal)
    ):
        raise _state_error(
            "state.event_correlation",
            "lifecycle root metadata does not match the exact message",
        )
    _remember_message(snapshot, entry.root_message_id, raw)
    return entry


def _lifecycle_reply_applied(
    snapshot: StateSnapshot, attributes: Mapping[str, Any], exact_message: bytes | None
) -> LifecycleEntry:
    values = _attributes(
        attributes,
        required=frozenset(
            {"message_id", "root_message_id", "message_type", "observed_at"}
        ),
    )
    raw = _require_message(exact_message)
    parsed = parse_exact_bytes(raw)
    root_id = _canonical_uuid(
        _required_text(values, "root_message_id"),
        field_name="root_message_id",
    )
    root_raw = snapshot._message_bytes.get(root_id)
    if root_raw is None:
        raise _state_error(
            "state.event_order",
            "lifecycle reply precedes its root message",
        )
    envelope = _validate_message(
        raw,
        against_raw=root_raw,
        observed_at=_required_text(values, "observed_at"),
    )
    message_id = _canonical_uuid(
        _required_text(values, "message_id"),
        field_name="message_id",
    )
    _record_nonce(snapshot, envelope, root=False)
    _remember_message(snapshot, message_id, raw)
    entry = snapshot.lifecycle.apply_reply(
        envelope,
        observed_at=_required_text(values, "observed_at"),
    )
    if (
        _canonical_uuid(envelope.get("message_id"), field_name="message_id")
        != message_id
        or _canonical_uuid(envelope.get("in_reply_to"), field_name="in_reply_to")
        != root_id
        or envelope.get("type") != _required_text(values, "message_type")
        or parsed != envelope
    ):
        raise _state_error(
            "state.event_correlation",
            "lifecycle reply metadata does not match the exact message",
        )
    return entry


def _lifecycle_expired_unconfirmed(
    snapshot: StateSnapshot, attributes: Mapping[str, Any], exact_message: bytes | None
) -> LifecycleEntry:
    _require_no_message(exact_message)
    values = _attributes(
        attributes,
        required=frozenset({"root_message_id", "observed_at"}),
    )
    return snapshot.lifecycle.mark_expired_unconfirmed(
        _required_text(values, "root_message_id"),
        observed_at=_required_text(values, "observed_at"),
    )


_EVENT_APPLIERS = {
    PARTICIPANT_ADDED: _participant_add,
    PARTICIPANT_BOUND: _participant_bind,
    PARTICIPANT_ROUTE_OBSERVED: _participant_route_observed,
    PARTICIPANT_ROUTE_CONFIRMED: _participant_route_confirmed,
    PARTICIPANT_INVALIDATED: _participant_invalidated,
    PARTICIPANT_RETIRED: _participant_retired,
    LIFECYCLE_ROOT_REGISTERED: _lifecycle_root_registered,
    LIFECYCLE_REPLY_APPLIED: _lifecycle_reply_applied,
    LIFECYCLE_EXPIRED_UNCONFIRMED: _lifecycle_expired_unconfirmed,
}


def _apply_event(
    snapshot: StateSnapshot,
    *,
    event_type: str,
    attributes: Mapping[str, Any],
    exact_message: bytes | None,
) -> Participant | LifecycleEntry:
    applier = _EVENT_APPLIERS.get(event_type)
    if applier is None:
        raise _state_error("state.event_type", "state event type is unsupported")
    return applier(snapshot, attributes, exact_message)


def _empty_snapshot(project: ProjectBinding) -> StateSnapshot:
    return StateSnapshot(
        roster=ParticipantRoster(project.project_id),
        lifecycle=LifecycleProjection(project.project_id),
    )


def _select_participant(roster: ParticipantRoster, selector: str) -> Participant:
    for participant in roster.participants.values():
        if selector in {participant.participant_id, participant.common_name}:
            return participant
    raise CamUsageError("roster.participant_unknown", "participant is not known")


def _transaction_snapshot_locked(
    project: ProjectBinding,
    transaction: ProjectTransaction,
) -> StateSnapshot:
    """Return the private canonical snapshot, replaying only an appended suffix."""

    require_project_transaction(project, transaction)
    records = _verified_records_for_transaction(project, transaction)
    caches = _transaction_cache(project, transaction)
    cached = caches.get(_STATE_CACHE_KEY)
    if cached is None:
        snapshot = _empty_snapshot(project)
    elif isinstance(cached, StateSnapshot):
        snapshot = cached
    else:
        raise _state_error("state.cache", "state transaction cache is invalid")

    start = snapshot.journal_sequence
    if start < 0 or start > len(records):
        raise _state_error("state.cache", "state cache journal position is invalid")
    if start and snapshot.journal_record_sha256 != records[start - 1]["record_sha256"]:
        raise _state_error("state.cache", "state cache journal digest is invalid")

    if cached is not None and start < len(records):
        # Apply an unprojected journal suffix to a working copy so a malformed
        # state event cannot partially corrupt the last known-good snapshot.
        snapshot = deepcopy(snapshot)
    for record in records[start:]:
        snapshot.journal_sequence = cast(int, record["sequence"])
        snapshot.journal_record_sha256 = cast(str, record["record_sha256"])
        event_type = cast(str, record["event_type"])
        if event_type not in STATE_EVENT_TYPES:
            if event_type.startswith("state."):
                raise _state_error(
                    "state.event_type",
                    f"unsupported state event at journal sequence {record['sequence']}",
                )
            continue
        attributes = record.get("attributes")
        if not isinstance(attributes, dict):
            raise _state_error(
                "state.event_attributes",
                f"invalid state event at journal sequence {record['sequence']}",
            )
        try:
            _apply_event(
                snapshot,
                event_type=event_type,
                attributes=attributes,
                exact_message=decode_exact_message(record),
            )
        except (CamUsageError, CamValidationError, StateError) as error:
            code = getattr(error, "code", error.__class__.__name__)
            raise _state_error(
                "state.event_invalid",
                f"invalid {event_type} at journal sequence {record['sequence']} ({code})",
            ) from None
    caches[_STATE_CACHE_KEY] = snapshot
    return snapshot


def _replay_locked(
    project: ProjectBinding,
    transaction: ProjectTransaction,
) -> StateSnapshot:
    """Return an isolated snapshot backed by one private transaction projection."""

    return deepcopy(_transaction_snapshot_locked(project, transaction))


def _cache_snapshot(
    project: ProjectBinding,
    transaction: ProjectTransaction,
    snapshot: StateSnapshot,
) -> None:
    """Transfer an unexposed committed snapshot into the transaction cache."""

    _transaction_cache(project, transaction)[_STATE_CACHE_KEY] = snapshot


def _write_projections(project: ProjectBinding, snapshot: StateSnapshot) -> None:
    replace_private_json(
        state_projection_path(project),
        snapshot.projection_document(),
    )


@contextmanager
def _transaction(
    project: ProjectBinding,
    supplied: ProjectTransaction | None,
) -> Iterator[ProjectTransaction]:
    if supplied is not None:
        require_project_transaction(project, supplied)
        yield supplied
        return
    with project_transaction(project) as acquired:
        yield acquired


class StateStore:
    """Mutate journal-backed projections under one project-wide transaction."""

    def __init__(self, project: ProjectBinding):
        self.project = project

    def rebuild(
        self, *, transaction: ProjectTransaction | None = None
    ) -> StateSnapshot:
        with _transaction(self.project, transaction) as active:
            snapshot = _replay_locked(self.project, active)
            _write_projections(self.project, snapshot)
            return snapshot

    def snapshot(
        self, *, transaction: ProjectTransaction | None = None
    ) -> StateSnapshot:
        """Replay current canonical state without refreshing disposable files."""

        with _transaction(self.project, transaction) as active:
            return _replay_locked(self.project, active)

    def preserved_message(
        self,
        message_id: str,
        *,
        transaction: ProjectTransaction | None = None,
    ) -> bytes | None:
        """Return the exact journaled bytes for one lifecycle message, if known."""

        canonical = _canonical_uuid(message_id, field_name="message_id")
        with _transaction(self.project, transaction) as active:
            return _transaction_snapshot_locked(
                self.project, active
            )._message_bytes.get(canonical)

    def _mutate(
        self,
        *,
        event_type: str,
        attributes: Mapping[str, Any],
        exact_message: bytes | None = None,
        now: dt.datetime | None = None,
        transaction: ProjectTransaction | None = None,
    ) -> Participant | LifecycleEntry:
        with _transaction(self.project, transaction) as active:
            snapshot = _replay_locked(self.project, active)
            return self._commit_locked(
                snapshot,
                active,
                event_type=event_type,
                attributes=attributes,
                exact_message=exact_message,
                now=now,
            )

    def _commit_locked(
        self,
        snapshot: StateSnapshot,
        transaction: ProjectTransaction,
        *,
        event_type: str,
        attributes: Mapping[str, Any],
        exact_message: bytes | None,
        now: dt.datetime | None,
    ) -> Participant | LifecycleEntry:
        result = _apply_event(
            snapshot,
            event_type=event_type,
            attributes=attributes,
            exact_message=exact_message,
        )
        record = append_record(
            self.project,
            event_type=event_type,
            exact_message=exact_message,
            attributes=attributes,
            now=now,
            transaction=transaction,
        )
        snapshot.journal_sequence = cast(int, record["sequence"])
        snapshot.journal_record_sha256 = cast(str, record["record_sha256"])
        _cache_snapshot(self.project, transaction, snapshot)
        try:
            _write_projections(self.project, snapshot)
        except ProjectError:
            raise ProjectionRefreshError(
                record_id=cast(str, record["record_id"]),
                sequence=cast(int, record["sequence"]),
            ) from None
        return result

    def participant_add(
        self,
        *,
        common_name: str,
        display_name: str,
        role: str,
        vendor: str,
        participant_id: str | None = None,
        now: dt.datetime | None = None,
        transaction: ProjectTransaction | None = None,
    ) -> Participant:
        identifier = participant_id or str(uuid.uuid4())
        result = self._mutate(
            event_type=PARTICIPANT_ADDED,
            attributes={
                "participant_id": identifier,
                "common_name": common_name,
                "display_name": display_name,
                "role": role,
                "vendor": vendor,
            },
            now=now,
            transaction=transaction,
        )
        return cast(Participant, result)

    def participant_bind(
        self,
        selector: str,
        *,
        session_id: str,
        session_label: str,
        session_kind: str | None,
        operator_reference: str,
        bound_at: str,
        now: dt.datetime | None = None,
        transaction: ProjectTransaction | None = None,
    ) -> Participant:
        with _transaction(self.project, transaction) as active:
            snapshot = _replay_locked(self.project, active)
            current = _select_participant(snapshot.roster, selector)
            return cast(
                Participant,
                self._commit_locked(
                    snapshot,
                    active,
                    event_type=PARTICIPANT_BOUND,
                    attributes={
                        "participant_id": current.participant_id,
                        "session_id": session_id,
                        "session_label": session_label,
                        "session_kind": session_kind,
                        "operator_reference": operator_reference,
                        "bound_at": bound_at,
                    },
                    exact_message=None,
                    now=now,
                ),
            )

    def participant_observe_route(
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
        now: dt.datetime | None = None,
        transaction: ProjectTransaction | None = None,
    ) -> Participant:
        with _transaction(self.project, transaction) as active:
            snapshot = _replay_locked(self.project, active)
            current = _select_participant(snapshot.roster, selector)
            return cast(
                Participant,
                self._commit_locked(
                    snapshot,
                    active,
                    event_type=PARTICIPANT_ROUTE_OBSERVED,
                    attributes={
                        "participant_id": current.participant_id,
                        "transport": transport,
                        "address": address,
                        "source": source,
                        "observed_at": observed_at,
                        "agent_view_id": agent_view_id,
                        "list_agents_name": list_agents_name,
                        "list_agents_ref": list_agents_ref,
                        "product_state": product_state,
                        "agent_view_kind": agent_view_kind,
                        "agent_view_started_at_ms": agent_view_started_at_ms,
                        "session_git_top_level": session_git_top_level,
                        "session_git_common_dir": session_git_common_dir,
                    },
                    exact_message=None,
                    now=now,
                ),
            )

    def participant_confirm_route(
        self,
        selector: str,
        *,
        expected_address: str,
        operator_reference: str,
        confirmed_at: str,
        now: dt.datetime | None = None,
        transaction: ProjectTransaction | None = None,
    ) -> Participant:
        with _transaction(self.project, transaction) as active:
            snapshot = _replay_locked(self.project, active)
            current = _select_participant(snapshot.roster, selector)
            return cast(
                Participant,
                self._commit_locked(
                    snapshot,
                    active,
                    event_type=PARTICIPANT_ROUTE_CONFIRMED,
                    attributes={
                        "participant_id": current.participant_id,
                        "expected_address": expected_address,
                        "operator_reference": operator_reference,
                        "confirmed_at": confirmed_at,
                    },
                    exact_message=None,
                    now=now,
                ),
            )

    def participant_invalidate(
        self,
        selector: str,
        *,
        reason: str,
        now: dt.datetime | None = None,
        transaction: ProjectTransaction | None = None,
    ) -> Participant:
        return self._participant_status_event(
            selector,
            event_type=PARTICIPANT_INVALIDATED,
            reason=reason,
            now=now,
            transaction=transaction,
        )

    def participant_retire(
        self,
        selector: str,
        *,
        reason: str,
        now: dt.datetime | None = None,
        transaction: ProjectTransaction | None = None,
    ) -> Participant:
        return self._participant_status_event(
            selector,
            event_type=PARTICIPANT_RETIRED,
            reason=reason,
            now=now,
            transaction=transaction,
        )

    def _participant_status_event(
        self,
        selector: str,
        *,
        event_type: str,
        reason: str,
        now: dt.datetime | None,
        transaction: ProjectTransaction | None,
    ) -> Participant:
        with _transaction(self.project, transaction) as active:
            snapshot = _replay_locked(self.project, active)
            current = _select_participant(snapshot.roster, selector)
            return cast(
                Participant,
                self._commit_locked(
                    snapshot,
                    active,
                    event_type=event_type,
                    attributes={
                        "participant_id": current.participant_id,
                        "reason": reason,
                    },
                    exact_message=None,
                    now=now,
                ),
            )

    def lifecycle_root(
        self,
        exact_message: bytes,
        *,
        renewal_of: str | None = None,
        now: dt.datetime | None = None,
        transaction: ProjectTransaction | None = None,
    ) -> LifecycleEntry:
        with _transaction(self.project, transaction) as active:
            plan = self.prepare_lifecycle(
                exact_message,
                renewal_of=renewal_of,
                now=now,
                transaction=active,
            )
            return self.commit_lifecycle(plan, transaction=active, now=now)

    def lifecycle_reply(
        self,
        exact_message: bytes,
        *,
        now: dt.datetime | None = None,
        transaction: ProjectTransaction | None = None,
    ) -> LifecycleEntry:
        with _transaction(self.project, transaction) as active:
            plan = self.prepare_lifecycle(
                exact_message,
                now=now,
                transaction=active,
            )
            if plan.event_type != LIFECYCLE_REPLY_APPLIED:
                raise CamUsageError(
                    "state.lifecycle_type",
                    "lifecycle_reply requires a reply envelope",
                )
            return self.commit_lifecycle(plan, transaction=active, now=now)

    def prepare_lifecycle(
        self,
        exact_message: bytes,
        *,
        renewal_of: str | None = None,
        preserved_against: bytes | None = None,
        require_preserved_against: bool = False,
        now: dt.datetime | None = None,
        transaction: ProjectTransaction,
    ) -> LifecyclePlan:
        """Validate one lifecycle candidate against the complete journal history."""

        require_project_transaction(self.project, transaction)
        event_now, observed_at = _event_time(now)
        envelope = parse_exact_bytes(exact_message)
        message_type = envelope.get("type")
        if message_type in ROOT_TYPES:
            event_type = LIFECYCLE_ROOT_REGISTERED
            attributes: dict[str, Any] = {
                "root_message_id": _canonical_uuid(
                    envelope.get("message_id"), field_name="message_id"
                ),
                "root_type": message_type,
                "renewal_of": (
                    _canonical_uuid(renewal_of, field_name="renewal_of")
                    if renewal_of is not None
                    else None
                ),
                "observed_at": observed_at,
            }
            correlated_root = message_type == "cancel"
        elif message_type in REPLY_TYPES:
            if renewal_of is not None:
                raise CamUsageError(
                    "state.renewal_type",
                    "renewal metadata is valid only for lifecycle roots",
                )
            event_type = LIFECYCLE_REPLY_APPLIED
            attributes = {
                "message_id": _canonical_uuid(
                    envelope.get("message_id"), field_name="message_id"
                ),
                "root_message_id": _canonical_uuid(
                    envelope.get("in_reply_to"), field_name="in_reply_to"
                ),
                "message_type": message_type,
                "observed_at": observed_at,
            }
            correlated_root = True
        else:
            raise CamUsageError(
                "state.lifecycle_type",
                "message type cannot participate in a lifecycle",
            )

        snapshot = _replay_locked(self.project, transaction)
        _validate_message(
            exact_message,
            observed_at=observed_at,
            allow_expired=message_type in ROOT_TYPES,
        )
        candidate_message_id = _canonical_uuid(
            envelope.get("message_id"), field_name="message_id"
        )
        duplicate = candidate_message_id in snapshot._message_bytes
        if correlated_root:
            root_id = _canonical_uuid(
                envelope.get("in_reply_to"), field_name="in_reply_to"
            )
            stored_root = snapshot._message_bytes.get(root_id)
            if stored_root is None:
                raise CamUsageError(
                    "state.root_missing",
                    "correlated root is not present in the project journal",
                )
            if require_preserved_against and preserved_against is None:
                raise CamUsageError(
                    "state.against_required",
                    "live correlated sends require the preserved root envelope",
                )
            if preserved_against is not None and preserved_against != stored_root:
                raise CamUsageError(
                    "state.against_mismatch",
                    "supplied root bytes do not equal the journaled root envelope",
                )
        elif preserved_against is not None:
            raise CamUsageError(
                "state.against_unexpected",
                "uncorrelated lifecycle roots do not accept preserved root bytes",
            )

        freshness_candidates = [
            _required_text(envelope, "expires_at"),
        ]
        if message_type in REPLY_TYPES:
            root_id = _canonical_uuid(
                envelope.get("in_reply_to"), field_name="in_reply_to"
            )
            prior = snapshot.lifecycle.reply_observation_basis(
                candidate_message_id,
                root_id,
            )
            receipt = envelope.get("receipt")
            status = receipt.get("status") if isinstance(receipt, dict) else None
            late_rejection = message_type == "ack" and status == "rejected"
            if (
                prior.state
                in {
                    LifecycleState.PENDING,
                    LifecycleState.HELD,
                    LifecycleState.EXPIRED_UNCONFIRMED,
                }
                and not late_rejection
            ):
                freshness_candidates.append(prior.root_expires_at)
        freshness_deadline = min(
            freshness_candidates,
            key=_validation_time,
        )

        preview = cast(
            LifecycleEntry,
            _apply_event(
                snapshot,
                event_type=event_type,
                attributes=attributes,
                exact_message=exact_message,
            ),
        )
        return LifecyclePlan(
            project_id=self.project.project_id,
            event_type=event_type,
            attributes=attributes,
            exact_message=exact_message,
            recorded_at=event_now,
            preview=preview,
            duplicate=duplicate,
            freshness_deadline=freshness_deadline,
        )

    def prepare_inbound_lifecycle(
        self,
        exact_message: bytes,
        *,
        renewal_of: str | None = None,
        now: dt.datetime | None = None,
        transaction: ProjectTransaction,
    ) -> LifecyclePlan:
        """Prepare inbound state, honoring prior accepted local reply delivery."""

        try:
            return self.prepare_lifecycle(
                exact_message,
                renewal_of=renewal_of,
                now=now,
                transaction=transaction,
            )
        except CamUsageError as error:
            if error.code != "lifecycle.root_expired_before_reply":
                raise
            accepted = self._prepare_accepted_outbound_reply(
                exact_message,
                now=now,
                transaction=transaction,
            )
            if accepted is None:
                raise
            return accepted

    def _prepare_accepted_outbound_reply(
        self,
        exact_message: bytes,
        *,
        now: dt.datetime | None,
        transaction: ProjectTransaction,
    ) -> LifecyclePlan | None:
        """Recognize a fresh callback for a reply this project already delivered."""

        require_project_transaction(self.project, transaction)
        event_now, observed_at = _event_time(now)
        envelope = parse_exact_bytes(exact_message)
        if envelope.get("type") not in REPLY_TYPES:
            return None
        message_id = _canonical_uuid(
            envelope.get("message_id"), field_name="message_id"
        )
        root_id = _canonical_uuid(envelope.get("in_reply_to"), field_name="in_reply_to")
        snapshot = _replay_locked(self.project, transaction)
        if snapshot._message_bytes.get(message_id) != exact_message:
            return None
        root_raw = snapshot._message_bytes.get(root_id)
        current = snapshot.lifecycle.entries.get(root_id)
        if root_raw is None or current is None:
            return None

        records = _verified_records_for_transaction(self.project, transaction)
        intent_ids = {
            record["record_id"]
            for record in records
            if record["event_type"] == "message.outbound.intent"
            and decode_exact_message(record) == exact_message
        }
        accepted_delivery = any(
            record["event_type"] == "transport.accepted"
            and isinstance(record.get("attributes"), dict)
            and record["attributes"].get("intent_record_id") in intent_ids
            and _uuid_values_equal(
                record["attributes"].get("message_id"),
                message_id,
            )
            and record["attributes"].get("lifecycle_state_committed") is True
            for record in records
        )
        if not accepted_delivery:
            return None

        _validate_message(
            exact_message,
            observed_at=observed_at,
            against_raw=root_raw,
        )
        return LifecyclePlan(
            project_id=self.project.project_id,
            event_type=LIFECYCLE_REPLY_APPLIED,
            attributes={
                "message_id": message_id,
                "root_message_id": root_id,
                "message_type": envelope["type"],
                "observed_at": observed_at,
            },
            exact_message=exact_message,
            recorded_at=event_now,
            preview=current,
            duplicate=True,
            freshness_deadline=_required_text(envelope, "expires_at"),
        )

    def commit_lifecycle(
        self,
        plan: LifecyclePlan,
        *,
        transaction: ProjectTransaction,
        now: dt.datetime | None = None,
        preserve_prepared_observation: bool = False,
    ) -> LifecycleEntry:
        """Commit a prepared lifecycle event while its project lock is held."""

        require_project_transaction(self.project, transaction)
        if plan.project_id != self.project.project_id:
            raise CamUsageError(
                "state.plan_project",
                "lifecycle plan belongs to another project",
            )
        if preserve_prepared_observation and now is not None:
            raise CamUsageError(
                "state.commit_time",
                "preserved observation cannot be combined with an override time",
            )
        snapshot = _replay_locked(self.project, transaction)
        if preserve_prepared_observation:
            observation_now = plan.recorded_at
            commit_now, _ = _event_time(None)
            attributes = dict(plan.attributes)
        else:
            commit_now, commit_observed_at = _event_time(now)
            observation_now = commit_now
            attributes = dict(plan.attributes)
            attributes["observed_at"] = commit_observed_at
        if plan.preview.state != LifecycleState.EXPIRED_UNCONFIRMED:
            require_plan_freshness(plan, now=observation_now)
        message_id = _canonical_uuid(
            plan.attributes.get("message_id", plan.attributes["root_message_id"]),
            field_name="message_id",
        )
        current_exact = snapshot._message_bytes.get(message_id)
        if current_exact is not None:
            if current_exact != plan.exact_message:
                raise CamUsageError(
                    "state.duplicate_changed",
                    "duplicate lifecycle message changed before commit",
                )
            root_id = _canonical_uuid(
                plan.attributes["root_message_id"], field_name="root_message_id"
            )
            current = snapshot.lifecycle.entries.get(root_id)
            if current is None:
                raise CamUsageError(
                    "state.duplicate_missing",
                    "duplicate lifecycle root is no longer present",
                )
            return current
        return cast(
            LifecycleEntry,
            self._commit_locked(
                snapshot,
                transaction,
                event_type=plan.event_type,
                attributes=attributes,
                exact_message=plan.exact_message,
                now=commit_now,
            ),
        )

    def lifecycle_expired(
        self,
        root_message_id: str,
        *,
        now: dt.datetime | None = None,
        transaction: ProjectTransaction | None = None,
    ) -> LifecycleEntry:
        event_now, observed_at = _event_time(now)
        result = self._mutate(
            event_type=LIFECYCLE_EXPIRED_UNCONFIRMED,
            attributes={
                "root_message_id": root_message_id,
                "observed_at": observed_at,
            },
            now=event_now,
            transaction=transaction,
        )
        return cast(LifecycleEntry, result)


def rebuild_state(
    project: ProjectBinding,
    *,
    transaction: ProjectTransaction | None = None,
) -> StateSnapshot:
    """Rebuild both disposable projections from the sole source of truth."""

    return StateStore(project).rebuild(transaction=transaction)


def validate_cancel_exact_bytes(
    cancel_raw: bytes,
    request_raw: bytes,
    *,
    now: dt.datetime | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate a cancel against the exact preserved request outside a project."""

    event_now, observed_at = _event_time(now)
    del event_now
    cancel = _validate_message(cancel_raw, observed_at=observed_at)
    request = _validate_message(
        request_raw,
        observed_at=observed_at,
        allow_expired=True,
    )
    if cancel.get("type") != "cancel":
        raise CamUsageError(
            "lifecycle.cancel_type",
            "candidate is not a cancel envelope",
        )
    _validate_cancel_against_request(cancel, request)
    return cancel, request
