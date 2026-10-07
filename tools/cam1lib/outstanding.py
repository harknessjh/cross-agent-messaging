# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Bounded exchange-state reporting from one detached, verified snapshot.

This module performs no I/O and grants no authority. In particular, missing
intake is not nondelivery, expiry is not a work deadline, and transport state
never changes lifecycle state or the availability of a reply slot.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field
from typing import Any

from . import journal, lifecycle, state_projection
from .participants import Participant
from .protocol import REPLY_TYPES, CamUsageError, CamValidationError, parse_exact_bytes
from .state_projection import StateSnapshot

FORMAT = "CAM-OUTSTANDING/1"
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
_STATUSES = frozenset(
    {
        "received",
        "needs_human_confirmation",
        "accepted",
        "rejected",
        "started",
        "completed",
        "failed",
        "cancelled",
        "handled",
        "correlated",
    }
)


def _invalid() -> CamUsageError:
    return CamUsageError(
        "outstanding.evidence_conflict",
        "journal evidence cannot be consistently attributed; no report was produced",
    )


def _uuid(value: Any) -> str:
    if not isinstance(value, str):
        raise _invalid()
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise _invalid() from None


def _time(value: Any) -> dt.datetime:
    try:
        return lifecycle._timestamp(value, field_name="timestamp")
    except CamUsageError:
        raise _invalid() from None


def _endpoint(value: Any) -> tuple[str, str, str]:
    if not isinstance(value, dict) or value.get("vendor") not in {
        "codex",
        "claude-code",
    }:
        raise _invalid()
    if not isinstance(value.get("agent_name"), str):
        raise _invalid()
    return value["vendor"], value["agent_name"], _uuid(value.get("session_id"))


@dataclass(frozen=True)
class _Binding:
    session: str
    generation: int


@dataclass(frozen=True)
class _Parties:
    sender: str
    recipient: str
    sender_binding: _Binding
    recipient_binding: _Binding


@dataclass
class _Message:
    raw: bytes
    envelope: dict[str, Any]
    message_id: str
    sequence: int
    parties: _Parties | None = None
    registered_sequence: int | None = None
    intakes: list[dict[str, Any]] = field(default_factory=list)
    attempts: list[_Attempt] = field(default_factory=list)


@dataclass
class _Attempt:
    record: dict[str, Any]
    message: _Message
    outcomes: list[dict[str, Any]] = field(default_factory=list)


class _Index:
    def __init__(self, snapshot: StateSnapshot, records: tuple[dict[str, Any], ...]):
        self.snapshot = snapshot
        self.messages: dict[str, _Message] = {}
        self.parsed: dict[bytes, dict[str, Any]] = {}
        self.bindings: dict[str, _Binding] = {}
        self.observations: dict[str, dict[str, Any]] = {}
        self.validations: dict[str, tuple[_Message, dict[str, Any]]] = {}
        self.attempts: dict[str, _Attempt] = {}
        self.reply_attempts: dict[str, list[_Attempt]] = {}
        self.unusable_evidence_count = 0
        self.successors = {
            entry.renewal_of: entry.root_message_id
            for entry in snapshot.lifecycle.entries.values()
            if entry.renewal_of is not None
        }
        for record in records:
            self._record(record)
        for message in self.messages.values():
            if message.envelope["type"] not in REPLY_TYPES:
                continue
            root = self.messages.get(_uuid(message.envelope["in_reply_to"]))
            if root is None or any(
                _endpoint(message.envelope[left]) != _endpoint(root.envelope[right])
                for left, right in (
                    ("claimed_sender", "recipient"),
                    ("recipient", "claimed_sender"),
                )
            ):
                raise _invalid()

    def _message(self, raw: bytes | None, sequence: int) -> _Message:
        if raw is None:
            raise _invalid()
        envelope = self.parsed.get(raw)
        if envelope is None:
            try:
                envelope = parse_exact_bytes(raw)
            except (CamUsageError, CamValidationError):
                raise _invalid() from None
            if envelope.get("protocol") != "CAM/1" or envelope.get("type") not in (
                lifecycle.ROOT_TYPES | REPLY_TYPES
            ):
                raise _invalid()
            _endpoint(envelope.get("claimed_sender"))
            _endpoint(envelope.get("recipient"))
            _time(envelope.get("sent_at"))
            _time(envelope.get("expires_at"))
            if envelope["type"] in REPLY_TYPES:
                root_id = _uuid(envelope.get("in_reply_to"))
                receipt = envelope.get("receipt")
                if envelope["type"] != "verify" and (
                    not isinstance(receipt, dict)
                    or receipt.get("status") not in _STATUSES
                    or _uuid(receipt.get("for_message_id")) != root_id
                ):
                    raise _invalid()
            self.parsed[raw] = envelope
        message_id = _uuid(envelope.get("message_id"))
        existing = self.messages.get(message_id)
        if existing is not None:
            if existing.raw != raw:
                raise _invalid()
            return existing
        message = _Message(raw, envelope, message_id, sequence)
        self.messages[message_id] = message
        return message

    def _attribute(self, message: _Message, attrs: dict[str, Any]) -> _Parties | None:
        sender = attrs.get("sender_participant_id")
        recipient = attrs.get("recipient_participant_id")
        if sender is None or recipient is None:
            self.unusable_evidence_count += 1
            return None
        sender, recipient = _uuid(sender), _uuid(recipient)
        for participant_id, wire_key in (
            (sender, "claimed_sender"),
            (recipient, "recipient"),
        ):
            participant = self.snapshot.roster.participants.get(participant_id)
            binding = self.bindings.get(participant_id)
            if (
                participant is None
                or binding is None
                or _endpoint(message.envelope[wire_key])
                != (participant.vendor, participant.common_name, binding.session)
            ):
                raise _invalid()
        parties = _Parties(
            sender, recipient, self.bindings[sender], self.bindings[recipient]
        )
        if message.parties is not None and (
            message.parties.sender != sender or message.parties.recipient != recipient
        ):
            raise _invalid()
        if message.parties is None:
            message.parties = parties
        return parties

    def _bind(self, participant_id: str, session: str) -> None:
        previous = self.bindings.get(participant_id)
        self.bindings[participant_id] = _Binding(
            _uuid(session), 1 if previous is None else previous.generation + 1
        )

    def _record(self, record: dict[str, Any]) -> None:
        kind, attrs = record["event_type"], record["attributes"]
        sequence = record["sequence"]
        if kind == state_projection.PARTICIPANT_BOUND:
            self._bind(attrs["participant_id"], attrs["session_id"])
        elif kind == state_projection.PARTICIPANT_ENROLLMENT_CONFIRMED:
            proposal = self.snapshot.enrollment.proposals[attrs["proposal_id"]]
            self._bind(proposal.participant_id, proposal.session_id)
        elif kind in {
            state_projection.LIFECYCLE_ROOT_REGISTERED,
            state_projection.LIFECYCLE_REPLY_APPLIED,
        }:
            message = self._message(journal.decode_exact_message(record), sequence)
            if message.registered_sequence is None:
                message.registered_sequence = sequence
        elif kind == "message.inbound.observed":
            self.observations[record["record_id"]] = record
        elif kind in {"message.inbound.validated", "message.inbound.duplicate"}:
            self._intake(record)
        elif kind == "message.outbound.intent":
            message = self._message(journal.decode_exact_message(record), sequence)
            if _uuid(attrs.get("message_id")) != message.message_id:
                raise _invalid()
            if self._attribute(message, attrs) is None:
                return
            if (
                attrs.get("recipient_session_id") is not None
                and _uuid(attrs["recipient_session_id"])
                != _endpoint(message.envelope["recipient"])[2]
            ):
                raise _invalid()
            attempt = _Attempt(record, message)
            self.attempts[record["record_id"]] = attempt
            message.attempts.append(attempt)
            if message.envelope["type"] in REPLY_TYPES:
                root_id = _uuid(message.envelope["in_reply_to"])
                self.reply_attempts.setdefault(root_id, []).append(attempt)
        elif kind in {"transport.accepted", "transport.not_accepted"}:
            attempt = self.attempts.get(attrs.get("intent_record_id"))
            if attempt is None:
                self.unusable_evidence_count += 1
            else:
                attempt.outcomes.append(record)

    def _intake(self, record: dict[str, Any]) -> None:
        attrs = record["attributes"]
        observed = self.observations.get(attrs.get("observed_record_id"))
        if observed is None:
            self.unusable_evidence_count += 1
            return
        raw = journal.decode_exact_message(observed)
        if raw is None:
            self.unusable_evidence_count += 1
            return
        message = self._message(raw, observed["sequence"])
        if _uuid(attrs.get("message_id")) != message.message_id:
            raise _invalid()
        duplicate = record["event_type"] == "message.inbound.duplicate"
        if duplicate:
            prior = self.validations.get(attrs.get("prior_validated_record_id"))
            if prior is None:
                self.unusable_evidence_count += 1
                return
            prior_message, proof = prior
            if (
                prior_message is not message
                or proof["sequence"] >= observed["sequence"]
            ):
                raise _invalid()
            assessment, committed = proof["assessment"], proof["lifecycle_committed"]
        else:
            assessment = attrs.get("assessment")
            committed = attrs.get("lifecycle_committed")
            if assessment == "validated" and committed is None:
                committed = True  # pre-causal validated record shape
            if (assessment, committed) not in {
                ("validated", True),
                ("held_for_clarification", False),
            }:
                self.unusable_evidence_count += 1
                return
        if committed and message.registered_sequence is None:
            self.unusable_evidence_count += 1
            return
        if self._attribute(message, attrs) is None:
            return
        proof = {
            "sequence": record["sequence"],
            "assessment": assessment,
            "lifecycle_committed": committed,
            "duplicate": duplicate,
        }
        message.intakes.append(proof)
        if not duplicate:
            self.validations[record["record_id"]] = (message, proof)


def _outcome(attempt: _Attempt) -> tuple[str, bool]:
    if not attempt.outcomes:
        return "orphaned", False
    if len(attempt.outcomes) != 1:
        return "unknown", True
    outcome = attempt.outcomes[0]
    attrs = outcome["attributes"]
    parties = attempt.message.parties
    assert parties is not None
    if (
        not isinstance(attrs.get("message_id"), str)
        or attrs["message_id"].lower() != attempt.message.message_id
        or attrs.get("participant_id") != parties.recipient
        or outcome["sequence"] <= attempt.record["sequence"]
    ):
        return "unknown", True
    if outcome["event_type"] == "transport.accepted":
        return "accepted", False
    if attrs.get("delivery_state") == "not_attempted":
        return "not_attempted", False
    return "unknown", False


def _transport(attempts: list[_Attempt]) -> dict[str, Any]:
    outcomes = [_outcome(attempt) for attempt in attempts]
    return {
        "status": outcomes[-1][0] if outcomes else "no_recorded_evidence",
        "attempt_count": len(attempts),
        "unresolved_attempt_count": sum(
            value in {"unknown", "orphaned"} for value, _ in outcomes
        ),
        "conflicting_outcome_count": sum(conflict for _, conflict in outcomes),
        "latest_intent_sequence": attempts[-1].record["sequence"] if attempts else None,
    }


def _intake(message: _Message) -> dict[str, Any]:
    proofs = message.intakes
    latest = proofs[-1] if proofs else None
    return {
        "status": latest["assessment"] if latest else "no_recorded_evidence",
        "lifecycle_committed": latest["lifecycle_committed"] if latest else None,
        "first_sequence": proofs[0]["sequence"] if proofs else None,
        "last_sequence": latest["sequence"] if latest else None,
        "record_count": len(proofs),
        "duplicate_count": sum(proof["duplicate"] for proof in proofs),
    }


def _participant(value: Participant) -> dict[str, Any]:
    return {
        "participant_id": value.participant_id,
        "common_name": value.common_name,
        "vendor": value.vendor,
        "status": value.status.value,
    }


def _reply(message: _Message) -> dict[str, Any]:
    return {
        "message_id": message.message_id,
        "type": message.envelope["type"],
        "receipt_status": (message.envelope.get("receipt") or {}).get("status"),
        "committed_sequence": message.registered_sequence,
    }


def _row(
    index: _Index, root: _Message, selected: Participant, now: dt.datetime
) -> dict[str, Any]:
    parties = root.parties
    assert parties is not None
    outgoing = parties.sender == selected.participant_id
    local = parties.sender_binding if outgoing else parties.recipient_binding
    peer_id = parties.recipient if outgoing else parties.sender
    entry = index.snapshot.lifecycle.entries.get(root.message_id)
    recorded = entry.state.value if entry else None
    deadline_passed = now >= _time(root.envelope["expires_at"])
    unconfirmed = recorded in {None, "pending", "held", "expired_unconfirmed"}
    effective = "expired_unconfirmed" if deadline_passed and unconfirmed else recorded
    attempts = index.reply_attempts.get(root.message_id, [])
    last = (
        index.messages.get(entry.last_message_id)
        if entry and entry.last_message_id
        else None
    )
    latest_attempt = _reply(attempts[-1].message) if attempts else None
    if latest_attempt is not None:
        latest_attempt["transport"] = _transport(attempts)
    return {
        "root_message_id": root.message_id,
        "root_type": root.envelope["type"],
        "first_evidence_sequence": root.registered_sequence or root.sequence,
        "counterpart": _participant(index.snapshot.roster.participants[peer_id]),
        "local_binding": {
            "generation": local.generation,
            "session_relation": "current_session"
            if selected.binding and local.session == selected.binding.session_id
            else "earlier_session",
            "generation_relation": "current_generation"
            if selected.binding and local.generation == selected.binding.generation
            else "earlier_generation",
        },
        "lifecycle_recorded": entry is not None,
        "recorded_state": recorded,
        "effective_state": effective,
        "sent_at": root.envelope["sent_at"],
        "expires_at": root.envelope["expires_at"],
        "expiry_annotation": (
            "unconfirmed_deadline_passed"
            if unconfirmed
            else "initial_deadline_passed_not_work_deadline"
        )
        if deadline_passed
        else "initial_deadline_not_passed",
        "renewal_of": entry.renewal_of if entry else None,
        "superseded_by": index.successors.get(root.message_id),
        "cancels_root_id": entry.cancels_root_id if entry else None,
        "cancelled_by_root_id": entry.cancelled_by_root_id if entry else None,
        "root_transport": _transport(root.attempts),
        "journaled_intake_evidence": _intake(root),
        "last_committed_reply": _reply(last) if last else None,
        "latest_reply_attempt": latest_attempt,
    }


def render(
    snapshot: StateSnapshot,
    records: tuple[dict[str, Any], ...],
    *,
    project_name: str,
    participant: str,
    as_of: dt.datetime,
    limit: int = DEFAULT_LIMIT,
    include_attention: bool = False,
) -> dict[str, Any]:
    """Render caller-owned snapshot values after their transaction is released."""
    if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
        raise CamUsageError("outstanding.limit", "limit must be between 1 and 200")
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise CamUsageError("outstanding.time", "as_of must be timezone-aware")
    as_of = as_of.astimezone(dt.UTC)
    if snapshot.journal_sequence != len(records) or snapshot.journal_record_sha256 != (
        records[-1]["record_sha256"] if records else None
    ):
        raise CamUsageError(
            "outstanding.snapshot_mismatch",
            "snapshot and records have different verified positions",
        )
    selected = state_projection._select_participant(snapshot.roster, participant)
    index = _Index(snapshot, records)
    sections: dict[str, list[_Message]] = {
        "outgoing_nonterminal": [],
        "incoming_accepted": [],
        "attention": [],
    }
    unattributed = 0
    for root in index.messages.values():
        if root.envelope["type"] not in lifecycle.ROOT_TYPES:
            continue
        entry = snapshot.lifecycle.entries.get(root.message_id)
        if root.parties is None:
            unattributed += entry is not None
            continue
        if entry is not None and entry.terminal:
            continue
        if root.parties.sender == selected.participant_id:
            sections["outgoing_nonterminal"].append(root)
        if (
            root.parties.recipient != selected.participant_id
            or root.envelope["type"] != "request"
        ):
            continue
        if entry is not None and entry.state in {
            lifecycle.LifecycleState.ACCEPTED,
            lifecycle.LifecycleState.STARTED,
        }:
            sections["incoming_accepted"].append(root)
        elif root.intakes and include_attention:
            sections["attention"].append(root)
    rendered: dict[str, Any] = {}
    for name, roots in sections.items():
        enabled = name != "attention" or include_attention
        roots.sort(
            key=lambda root: (
                root.registered_sequence or root.sequence,
                root.message_id,
            )
        )
        shown = roots[:limit]
        rendered[name] = {
            "enabled": enabled,
            "total": len(roots) if enabled else None,
            "shown": len(shown),
            "omitted": len(roots) - len(shown) if enabled else None,
            "items": [_row(index, root, selected, as_of) for root in shown],
        }
    identity = _participant(selected)
    identity["binding_generation"] = (
        selected.binding.generation if selected.binding else None
    )
    return {
        "ok": True,
        "format": FORMAT,
        "project": {
            "project_id": snapshot.roster.project_id,
            "display_name": project_name,
        },
        "participant": identity,
        "as_of": as_of.isoformat().replace("+00:00", "Z"),
        "journal_position": {
            "sequence": snapshot.journal_sequence,
            "record_sha256": snapshot.journal_record_sha256,
        },
        "limit_per_section": limit,
        "sections": rendered,
        "coverage": {
            "unattributed_lifecycle_roots": unattributed,
            "unusable_audit_records": index.unusable_evidence_count,
            "terminal_intake_tracking": False,
        },
    }
