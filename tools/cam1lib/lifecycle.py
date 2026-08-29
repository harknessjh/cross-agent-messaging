# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Stateful CAM/1 request-lifecycle projection.

The wire validator deliberately checks only one root/candidate pair.  This
module adds the history-dependent checks needed by the audited local profile
without changing the CAM/1 envelope or treating retained state as authority.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

from .protocol import CamUsageError

LIFECYCLE_FORMAT = "CAM-LIFECYCLE/1"
ROOT_TYPES = frozenset({"hello", "challenge", "request", "cancel"})
TERMINAL_STATES = frozenset(
    {
        "handled",
        "correlated",
        "completed",
        "rejected",
        "failed",
        "cancelled",
        "late_rejected",
    }
)


class LifecycleState(StrEnum):
    """Local projection states; these are not CAM/1 wire receipt values."""

    PENDING = "pending"
    HELD = "held"
    RECEIVED = "received"
    ACCEPTED = "accepted"
    STARTED = "started"
    HANDLED = "handled"
    CORRELATED = "correlated"
    COMPLETED = "completed"
    REJECTED = "rejected"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED_UNCONFIRMED = "expired_unconfirmed"
    LATE_REJECTED = "late_rejected"


@dataclass(frozen=True, slots=True)
class LifecycleEntry:
    """Current state for one root envelope."""

    root_message_id: str
    root_type: str
    idempotency_key: str
    semantic_request_sha256: str
    root_sent_at: str
    root_expires_at: str
    observed_at: str
    state: LifecycleState = LifecycleState.PENDING
    last_message_id: str | None = None
    last_reply_sent_at: str | None = None
    last_reply_observed_at: str | None = None
    reply_message_ids: tuple[str, ...] = ()
    terminal: bool = False
    renewal_of: str | None = None
    cancels_root_id: str | None = None
    cancelled_by_root_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "root_message_id": self.root_message_id,
            "root_type": self.root_type,
            "idempotency_key": self.idempotency_key,
            "semantic_request_sha256": self.semantic_request_sha256,
            "root_sent_at": self.root_sent_at,
            "root_expires_at": self.root_expires_at,
            "observed_at": self.observed_at,
            "state": self.state.value,
            "last_message_id": self.last_message_id,
            "last_reply_sent_at": self.last_reply_sent_at,
            "last_reply_observed_at": self.last_reply_observed_at,
            "reply_message_ids": list(self.reply_message_ids),
            "terminal": self.terminal,
            "renewal_of": self.renewal_of,
            "cancels_root_id": self.cancels_root_id,
            "cancelled_by_root_id": self.cancelled_by_root_id,
        }


def _canonical_uuid(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise CamUsageError(
            "lifecycle.identifier",
            f"{field_name} must be a UUID string",
        )
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError):
        raise CamUsageError(
            "lifecycle.identifier",
            f"{field_name} must be a valid UUID",
        ) from None


def _timestamp(value: Any, *, field_name: str) -> dt.datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise CamUsageError(
            "lifecycle.timestamp",
            f"{field_name} must be a UTC timestamp",
        )
    try:
        parsed = dt.datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise CamUsageError(
            "lifecycle.timestamp",
            f"{field_name} must be a valid UTC timestamp",
        ) from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CamUsageError(
            "lifecycle.timestamp",
            f"{field_name} must be timezone-aware",
        )
    return parsed.astimezone(dt.UTC)


def _timestamp_text(value: Any, *, field_name: str) -> str:
    parsed = _timestamp(value, field_name=field_name)
    if parsed.microsecond:
        return parsed.isoformat(timespec="microseconds").replace("+00:00", "Z")
    return parsed.isoformat(timespec="seconds").replace("+00:00", "Z")


def _message_digest(envelope: dict[str, Any]) -> str:
    canonical = json.dumps(
        envelope,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _semantic_request_digest(envelope: dict[str, Any]) -> str:
    """Hash request meaning while excluding only refreshable wire metadata."""

    semantic = {
        key: value
        for key, value in envelope.items()
        if key not in {"message_id", "nonce", "sent_at", "expires_at", "authorization"}
    }
    action = semantic.get("action")
    if not isinstance(action, dict):
        raise CamUsageError(
            "lifecycle.action",
            "lifecycle root must contain an action object",
        )
    normalized_action = dict(action)
    scope = action.get("scope")
    if isinstance(scope, dict):
        normalized_scope: dict[str, Any] = {}
        for name, value in scope.items():
            normalized_scope[name] = (
                sorted(value)
                if isinstance(value, list)
                and all(isinstance(item, str) for item in value)
                else value
            )
        normalized_action["scope"] = normalized_scope
    semantic["action"] = normalized_action
    evidence = semantic.get("evidence")
    if isinstance(evidence, list):
        semantic["evidence"] = sorted(
            evidence,
            key=lambda item: json.dumps(
                item,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
        )
    canonical = json.dumps(
        semantic,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def semantic_operation_digest(envelope: dict[str, Any]) -> str:
    """Return the stable semantic digest used for idempotent root operations."""

    return _semantic_request_digest(envelope)


def _reply_shape(envelope: dict[str, Any]) -> tuple[str, str | None]:
    message_type = envelope.get("type")
    if not isinstance(message_type, str):
        raise CamUsageError("lifecycle.type", "message type must be a string")
    receipt = envelope.get("receipt")
    status: str | None = None
    if isinstance(receipt, dict):
        value = receipt.get("status")
        if isinstance(value, str):
            status = value
    return message_type, status


def _request_transition(
    state: LifecycleState,
    message_type: str,
    status: str | None,
) -> LifecycleState | None:
    shape = (message_type, status)
    initial = {
        ("ack", "received"): LifecycleState.RECEIVED,
        ("ack", "needs_human_confirmation"): LifecycleState.HELD,
        ("ack", "accepted"): LifecycleState.ACCEPTED,
        ("ack", "rejected"): LifecycleState.REJECTED,
        ("error", "failed"): LifecycleState.FAILED,
    }
    after_receipt = {
        ("status", "accepted"): LifecycleState.ACCEPTED,
        ("error", "failed"): LifecycleState.FAILED,
    }
    after_hold = {
        ("ack", "accepted"): LifecycleState.ACCEPTED,
        ("ack", "rejected"): LifecycleState.REJECTED,
        ("error", "failed"): LifecycleState.FAILED,
    }
    after_acceptance = {
        ("status", "started"): LifecycleState.STARTED,
        ("result", "completed"): LifecycleState.COMPLETED,
        ("error", "failed"): LifecycleState.FAILED,
    }
    after_started = {
        ("result", "completed"): LifecycleState.COMPLETED,
        ("error", "failed"): LifecycleState.FAILED,
    }
    if state == LifecycleState.PENDING:
        return initial.get(shape)
    if state == LifecycleState.RECEIVED:
        return after_receipt.get(shape)
    if state == LifecycleState.HELD:
        return after_hold.get(shape)
    if state == LifecycleState.ACCEPTED:
        return after_acceptance.get(shape)
    if state == LifecycleState.STARTED:
        return after_started.get(shape)
    return None


def _hello_transition(
    state: LifecycleState,
    message_type: str,
    status: str | None,
) -> LifecycleState | None:
    if message_type != "ack":
        return None
    if state == LifecycleState.PENDING:
        return {
            "needs_human_confirmation": LifecycleState.HELD,
            "received": LifecycleState.HANDLED,
            "rejected": LifecycleState.REJECTED,
        }.get(status)
    if state == LifecycleState.HELD:
        return {
            "received": LifecycleState.HANDLED,
            "rejected": LifecycleState.REJECTED,
        }.get(status)
    return None


def _challenge_transition(
    state: LifecycleState,
    message_type: str,
    status: str | None,
) -> LifecycleState | None:
    if message_type == "verify" and status is None:
        if state in {LifecycleState.PENDING, LifecycleState.HELD}:
            return LifecycleState.CORRELATED
        return None
    if message_type == "ack" and state == LifecycleState.PENDING:
        return {
            "needs_human_confirmation": LifecycleState.HELD,
            "rejected": LifecycleState.REJECTED,
        }.get(status)
    if message_type == "ack" and state == LifecycleState.HELD:
        return LifecycleState.REJECTED if status == "rejected" else None
    return None


def _cancel_transition(
    state: LifecycleState,
    message_type: str,
    status: str | None,
) -> LifecycleState | None:
    if state == LifecycleState.PENDING:
        return {
            ("ack", "received"): LifecycleState.RECEIVED,
            ("ack", "accepted"): LifecycleState.CANCELLED,
            ("ack", "rejected"): LifecycleState.REJECTED,
            ("error", "failed"): LifecycleState.FAILED,
        }.get((message_type, status))
    if state == LifecycleState.RECEIVED:
        return {
            ("status", "accepted"): LifecycleState.CANCELLED,
            ("error", "failed"): LifecycleState.FAILED,
        }.get((message_type, status))
    return None


def next_state(
    root_type: str,
    current: LifecycleState,
    candidate: dict[str, Any],
) -> LifecycleState:
    """Return the legal next state or fail closed on a stateful violation."""

    if current == LifecycleState.EXPIRED_UNCONFIRMED:
        raise CamUsageError(
            "lifecycle.expired",
            "an expired-unconfirmed lifecycle cannot be advanced",
        )
    if current.value in TERMINAL_STATES:
        raise CamUsageError(
            "lifecycle.terminal",
            "a terminal lifecycle cannot be advanced",
        )
    message_type, status = _reply_shape(candidate)
    resolver = {
        "hello": _hello_transition,
        "challenge": _challenge_transition,
        "request": _request_transition,
        "cancel": _cancel_transition,
    }.get(root_type)
    if resolver is None:
        raise CamUsageError(
            "lifecycle.root_type",
            "message is not a supported lifecycle root",
        )
    resolved = resolver(current, message_type, status)
    if resolved is None:
        raise CamUsageError(
            "lifecycle.transition",
            "candidate is not legal at the current lifecycle state",
        )
    return resolved


def _root_times(
    envelope: dict[str, Any], observed_at: str
) -> tuple[str, str, str, LifecycleState]:
    sent_at = _timestamp_text(envelope.get("sent_at"), field_name="sent_at")
    expires_at = _timestamp_text(envelope.get("expires_at"), field_name="expires_at")
    observed = _timestamp_text(observed_at, field_name="observed_at")
    observed_time = _timestamp(observed, field_name="observed_at")
    if observed_time < _timestamp(sent_at, field_name="sent_at"):
        raise CamUsageError(
            "lifecycle.observed_before_sent",
            "root was observed before its claimed send time",
        )
    state = (
        LifecycleState.EXPIRED_UNCONFIRMED
        if observed_time >= _timestamp(expires_at, field_name="expires_at")
        else LifecycleState.PENDING
    )
    return sent_at, expires_at, observed, state


def _stateful_reply_state(
    current: LifecycleEntry,
    envelope: dict[str, Any],
    *,
    observed_at: str,
) -> LifecycleState:
    reply_sent_at = _timestamp(envelope.get("sent_at"), field_name="sent_at")
    if reply_sent_at < _timestamp(current.root_sent_at, field_name="root_sent_at"):
        raise CamUsageError(
            "lifecycle.reply_predates_root",
            "reply send time predates its root request",
        )
    message_type, status = _reply_shape(envelope)
    root_expired = _timestamp(observed_at, field_name="observed_at") >= _timestamp(
        current.root_expires_at, field_name="root_expires_at"
    )
    late_rejection = message_type == "ack" and status == "rejected" and root_expired
    unconfirmed = current.state in {
        LifecycleState.PENDING,
        LifecycleState.HELD,
        LifecycleState.EXPIRED_UNCONFIRMED,
    }
    if unconfirmed and root_expired and not late_rejection:
        raise CamUsageError(
            "lifecycle.root_expired_before_reply",
            "unconfirmed root expired before the reply was observed",
        )
    if unconfirmed and late_rejection:
        return LifecycleState.LATE_REJECTED
    return next_state(current.root_type, current.state, envelope)


def _reply_transition(
    current: LifecycleEntry,
    envelope: dict[str, Any],
    *,
    observed_at: str | None,
) -> tuple[str, str, LifecycleState]:
    """Validate one reply observation and return its normalized transition."""

    reply_sent_at = _timestamp_text(envelope.get("sent_at"), field_name="sent_at")
    observed = _timestamp_text(
        observed_at if observed_at is not None else reply_sent_at,
        field_name="observed_at",
    )
    if _timestamp(observed, field_name="observed_at") < _timestamp(
        reply_sent_at,
        field_name="sent_at",
    ):
        raise CamUsageError(
            "lifecycle.reply_observed_before_sent",
            "reply was observed before its claimed send time",
        )
    if current.last_reply_sent_at is not None and _timestamp(
        reply_sent_at,
        field_name="sent_at",
    ) < _timestamp(current.last_reply_sent_at, field_name="last_reply_sent_at"):
        raise CamUsageError(
            "lifecycle.reply_chronology",
            "reply send time regresses behind the prior lifecycle reply",
        )
    if current.last_reply_observed_at is not None and _timestamp(
        observed,
        field_name="observed_at",
    ) < _timestamp(
        current.last_reply_observed_at,
        field_name="last_reply_observed_at",
    ):
        raise CamUsageError(
            "lifecycle.reply_chronology",
            "reply observation time regresses behind the prior lifecycle reply",
        )
    return (
        reply_sent_at,
        observed,
        _stateful_reply_state(current, envelope, observed_at=observed),
    )


@dataclass(slots=True)
class LifecycleProjection:
    """Internal in-memory projection rebuilt from validated journal events.

    Untrusted callers should use :class:`cam1lib.state.StateStore`, which
    validates preserved exact bytes and full journal history before invoking
    these mutation methods.
    """

    project_id: str
    entries: dict[str, LifecycleEntry] = field(default_factory=dict)
    processed_messages: dict[str, str] = field(default_factory=dict)
    _reply_prior_entries: dict[str, LifecycleEntry] = field(
        default_factory=dict,
        repr=False,
    )

    def __post_init__(self) -> None:
        self.project_id = _canonical_uuid(self.project_id, field_name="project_id")

    def _semantic_operations(
        self,
        idempotency_key: str,
        *,
        excluding: str,
    ) -> tuple[LifecycleEntry, ...]:
        return tuple(
            entry
            for entry in self.entries.values()
            if entry.idempotency_key == idempotency_key
            and entry.root_message_id != excluding
        )

    def reply_observation_basis(
        self,
        message_id: str,
        root_message_id: str,
    ) -> LifecycleEntry:
        """Return the root state against which a reply was first evaluated."""

        canonical_message = _canonical_uuid(message_id, field_name="message_id")
        canonical_root = _canonical_uuid(
            root_message_id,
            field_name="root_message_id",
        )
        current = self.entries.get(canonical_root)
        if current is None:
            raise CamUsageError(
                "lifecycle.root_missing",
                "reply root is not present in lifecycle state",
            )
        return self._reply_prior_entries.get(canonical_message, current)

    def _resolve_renewal(
        self,
        prior_operations: tuple[LifecycleEntry, ...],
        renewal_of: str | None,
        *,
        semantic_request_sha256: str,
        renewal_sent_at: str,
        renewal_observed_at: str,
    ) -> str | None:
        if not prior_operations:
            if renewal_of is not None:
                raise CamUsageError(
                    "lifecycle.renewal_root",
                    "renewal root is not present for this semantic operation",
                )
            return None
        if renewal_of is None:
            raise CamUsageError(
                "lifecycle.idempotency_conflict",
                "existing semantic idempotency key requires an explicit renewal root",
            )
        canonical = _canonical_uuid(renewal_of, field_name="renewal_of")
        prior = next(
            (entry for entry in prior_operations if entry.root_message_id == canonical),
            None,
        )
        if prior is None:
            raise CamUsageError(
                "lifecycle.renewal_root",
                "renewal does not reference the prior semantic operation",
            )
        renewal_observed = _timestamp(
            renewal_observed_at,
            field_name="observed_at",
        )
        predecessor_cancels = tuple(
            entry
            for entry in self.entries.values()
            if entry.cancels_root_id == canonical
        )
        for cancel in predecessor_cancels:
            if (
                cancel.state == LifecycleState.PENDING
                and renewal_observed
                >= _timestamp(
                    cancel.root_expires_at,
                    field_name="root_expires_at",
                )
            ):
                self.mark_expired_unconfirmed(
                    cancel.root_message_id,
                    observed_at=renewal_observed_at,
                )
        if any(
            self.entries[cancel.root_message_id].state
            in {LifecycleState.PENDING, LifecycleState.RECEIVED}
            for cancel in predecessor_cancels
        ):
            raise CamUsageError(
                "lifecycle.renewal_cancel_unresolved",
                "renewal is blocked while a cancel for the prior operation is unresolved",
            )
        if prior.state in {LifecycleState.PENDING, LifecycleState.HELD} and _timestamp(
            renewal_observed_at,
            field_name="observed_at",
        ) >= _timestamp(prior.root_expires_at, field_name="root_expires_at"):
            prior = self.mark_expired_unconfirmed(
                prior.root_message_id,
                observed_at=renewal_observed_at,
            )
        if any(entry.renewal_of == canonical for entry in prior_operations):
            raise CamUsageError(
                "lifecycle.renewal_superseded",
                "renewal root has already been superseded",
            )
        if prior.state not in {
            LifecycleState.EXPIRED_UNCONFIRMED,
            LifecycleState.LATE_REJECTED,
        }:
            raise CamUsageError(
                "lifecycle.renewal_state",
                "only an expired-unconfirmed or late-rejected operation may be renewed",
            )
        if prior.semantic_request_sha256 != semantic_request_sha256:
            raise CamUsageError(
                "lifecycle.renewal_semantics",
                "renewal changes the prior request's semantic content",
            )
        prior_boundary = max(
            _timestamp(prior.root_expires_at, field_name="root_expires_at"),
            _timestamp(prior.observed_at, field_name="observed_at"),
            *(
                [
                    _timestamp(
                        prior.last_reply_observed_at,
                        field_name="last_reply_observed_at",
                    )
                ]
                if prior.last_reply_observed_at is not None
                else []
            ),
        )
        if _timestamp(renewal_sent_at, field_name="sent_at") < _timestamp(
            prior.root_expires_at,
            field_name="root_expires_at",
        ):
            raise CamUsageError(
                "lifecycle.renewal_chronology",
                "renewal claims to have been sent before the prior root expired",
            )
        if _timestamp(renewal_observed_at, field_name="observed_at") < prior_boundary:
            raise CamUsageError(
                "lifecycle.renewal_chronology",
                "renewal was observed before the prior operation closed",
            )
        return canonical

    def _cancel_target(
        self,
        envelope: dict[str, Any],
        message_type: str,
        *,
        observed_at: str,
    ) -> str | None:
        if message_type != "cancel":
            return None
        target_id = _canonical_uuid(
            envelope.get("in_reply_to"), field_name="in_reply_to"
        )
        target = self.entries.get(target_id)
        if target is None or target.root_type != "request":
            raise CamUsageError(
                "lifecycle.cancel_target",
                "cancel target must be a registered request root",
            )
        if target.terminal or target.state == LifecycleState.EXPIRED_UNCONFIRMED:
            raise CamUsageError(
                "lifecycle.cancel_target_terminal",
                "cancel target is no longer active",
            )
        if target.state in {
            LifecycleState.PENDING,
            LifecycleState.HELD,
        } and _timestamp(observed_at, field_name="observed_at") >= _timestamp(
            target.root_expires_at, field_name="root_expires_at"
        ):
            raise CamUsageError(
                "lifecycle.cancel_target_expired",
                "unconfirmed cancel target expired before cancellation",
            )
        if any(
            entry.cancels_root_id == target_id
            and entry.state in {LifecycleState.PENDING, LifecycleState.RECEIVED}
            for entry in self.entries.values()
        ):
            raise CamUsageError(
                "lifecycle.cancel_conflict",
                "request already has an active cancel lifecycle",
            )
        return target_id

    def register_root(
        self,
        envelope: dict[str, Any],
        *,
        observed_at: str,
        renewal_of: str | None = None,
    ) -> LifecycleEntry:
        message_type = envelope.get("type")
        if message_type not in ROOT_TYPES:
            raise CamUsageError(
                "lifecycle.root_type",
                "only hello, challenge, request, or cancel can start a lifecycle",
            )
        message_id = _canonical_uuid(
            envelope.get("message_id"), field_name="message_id"
        )
        digest = _message_digest(envelope)
        sent_at, expires_at, observed, initial_state = _root_times(
            envelope,
            observed_at,
        )
        prior_digest = self.processed_messages.get(message_id)
        if prior_digest is not None:
            if prior_digest == digest:
                if initial_state == LifecycleState.EXPIRED_UNCONFIRMED:
                    raise CamUsageError(
                        "lifecycle.duplicate_expired",
                        "expired root cannot be retransmitted",
                    )
                existing = self.entries[message_id]
                canonical_renewal = (
                    _canonical_uuid(renewal_of, field_name="renewal_of")
                    if renewal_of is not None
                    else None
                )
                if (
                    canonical_renewal is not None
                    and existing.renewal_of != canonical_renewal
                ):
                    raise CamUsageError(
                        "lifecycle.renewal_conflict",
                        "duplicate root supplied different renewal metadata",
                    )
                return existing
            raise CamUsageError(
                "lifecycle.message_conflict",
                "message ID was reused with different content",
            )
        action = envelope.get("action")
        idempotency_key = _canonical_uuid(
            action.get("idempotency_key") if isinstance(action, dict) else None,
            field_name="action.idempotency_key",
        )
        semantic_request_sha256 = _semantic_request_digest(envelope)
        prior_operations = self._semantic_operations(
            idempotency_key,
            excluding=message_id,
        )
        canonical_renewal = self._resolve_renewal(
            prior_operations,
            renewal_of,
            semantic_request_sha256=semantic_request_sha256,
            renewal_sent_at=sent_at,
            renewal_observed_at=observed,
        )
        cancels_root_id = self._cancel_target(
            envelope,
            message_type,
            observed_at=observed,
        )
        entry = LifecycleEntry(
            root_message_id=message_id,
            root_type=message_type,
            idempotency_key=idempotency_key,
            semantic_request_sha256=semantic_request_sha256,
            root_sent_at=sent_at,
            root_expires_at=expires_at,
            observed_at=observed,
            state=initial_state,
            renewal_of=canonical_renewal,
            cancels_root_id=cancels_root_id,
        )
        self.entries[message_id] = entry
        self.processed_messages[message_id] = digest
        return entry

    def apply_reply(
        self,
        envelope: dict[str, Any],
        *,
        observed_at: str | None = None,
    ) -> LifecycleEntry:
        message_id = _canonical_uuid(
            envelope.get("message_id"), field_name="message_id"
        )
        digest = _message_digest(envelope)
        prior_digest = self.processed_messages.get(message_id)
        if prior_digest is not None:
            if prior_digest != digest:
                raise CamUsageError(
                    "lifecycle.message_conflict",
                    "message ID was reused with different content",
                )
            root_id = _canonical_uuid(
                envelope.get("in_reply_to"), field_name="in_reply_to"
            )
            prior_entry = self._reply_prior_entries.get(message_id)
            if prior_entry is None:
                raise CamUsageError(
                    "lifecycle.duplicate_history",
                    "duplicate reply has no preserved pre-transition state",
                )
            _reply_transition(
                prior_entry,
                envelope,
                observed_at=observed_at,
            )
            return self.entries[root_id]

        root_id = _canonical_uuid(envelope.get("in_reply_to"), field_name="in_reply_to")
        current = self.entries.get(root_id)
        if current is None:
            raise CamUsageError(
                "lifecycle.root_missing",
                "reply root is not present in lifecycle state",
            )
        reply_sent_at, observed, state = _reply_transition(
            current,
            envelope,
            observed_at=observed_at,
        )
        updated = replace(
            current,
            state=state,
            last_message_id=message_id,
            last_reply_sent_at=reply_sent_at,
            last_reply_observed_at=observed,
            reply_message_ids=(*current.reply_message_ids, message_id),
            terminal=state.value in TERMINAL_STATES,
        )
        cancelled_target = (
            self._cancelled_target(updated)
            if updated.root_type == "cancel" and state == LifecycleState.CANCELLED
            else None
        )
        self.entries[root_id] = updated
        if cancelled_target is not None:
            self.entries[cancelled_target.root_message_id] = cancelled_target
        self._reply_prior_entries[message_id] = current
        self.processed_messages[message_id] = digest
        return updated

    def _cancelled_target(self, cancel: LifecycleEntry) -> LifecycleEntry:
        target_id = cancel.cancels_root_id
        target = self.entries.get(target_id) if target_id is not None else None
        if target is None:
            raise CamUsageError(
                "lifecycle.cancel_target",
                "accepted cancel has no registered request target",
            )
        if target.terminal:
            raise CamUsageError(
                "lifecycle.cancel_target_terminal",
                "accepted cancel cannot replace a terminal request outcome",
            )
        return replace(
            target,
            state=LifecycleState.CANCELLED,
            terminal=True,
            cancelled_by_root_id=cancel.root_message_id,
        )

    def mark_expired_unconfirmed(
        self,
        root_message_id: str,
        *,
        observed_at: str,
    ) -> LifecycleEntry:
        root_id = _canonical_uuid(root_message_id, field_name="root_message_id")
        current = self.entries.get(root_id)
        if current is None:
            raise CamUsageError(
                "lifecycle.root_missing",
                "lifecycle root is not present",
            )
        if current.terminal:
            raise CamUsageError(
                "lifecycle.terminal",
                "a terminal lifecycle cannot be marked expired-unconfirmed",
            )
        if current.state not in {
            LifecycleState.PENDING,
            LifecycleState.HELD,
            LifecycleState.EXPIRED_UNCONFIRMED,
        }:
            raise CamUsageError(
                "lifecycle.already_confirmed",
                "a confirmed lifecycle remains active beyond root expiry",
            )
        observed = _timestamp_text(observed_at, field_name="observed_at")
        if _timestamp(observed, field_name="observed_at") < _timestamp(
            current.root_expires_at, field_name="root_expires_at"
        ):
            raise CamUsageError(
                "lifecycle.not_expired",
                "lifecycle cannot be marked expired before root expiry",
            )
        updated = replace(
            current,
            state=LifecycleState.EXPIRED_UNCONFIRMED,
            terminal=False,
            observed_at=observed,
        )
        self.entries[root_id] = updated
        return updated

    def as_dict(self) -> dict[str, Any]:
        return {
            "format": LIFECYCLE_FORMAT,
            "project_id": self.project_id,
            "entries": [self.entries[key].as_dict() for key in sorted(self.entries)],
        }
