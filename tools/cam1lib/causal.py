# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Journal-only causal ordering for same-project CAM conversations.

The causal context is deliberately absent from the CAM/1 wire envelope.  It is
derived while the project transaction is held, stored on the outbound-intent
record, and correlated by exact message bytes when the recipient ingests the
product-visible envelope.  It describes awareness only; it never grants
authority or changes the lifecycle projection.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from jsonschema import Draft202012Validator, FormatChecker

from . import journal
from .errors import ProjectError
from .protocol import REPLY_TYPES, parse_exact_bytes

CAUSAL_FORMAT = "CAM-CAUSAL/1"
CAUSAL_FEATURE_ID = "causal.ordering"
CAUSAL_FEATURE_VERSION = 1
CAUSAL_CAPABILITY = "causal.ordering/1"
MAX_CAUSAL_REFERENCES = 64
CAUSAL_JOURNAL_EVENT_TYPES = frozenset(
    {
        "message.inbound.validated",
        "message.outbound.intent",
        "state.compatibility.gate_activated",
        "transport.accepted",
        "transport.not_accepted",
    }
)

_SCHEMA_PATH = (
    Path(__file__).resolve().parents[2] / "schemas" / "cam-causal-context-1.schema.json"
)


class CausalError(ProjectError):
    """A causal journal assertion is malformed or inconsistent."""


@dataclass(frozen=True, slots=True)
class CausalContext:
    conversation_id: str
    depends_on: tuple[str, ...]
    supersedes: tuple[str, ...]
    recipient_frontier: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "format": CAUSAL_FORMAT,
            "conversation_id": self.conversation_id,
            "depends_on": list(self.depends_on),
            "supersedes": list(self.supersedes),
            "recipient_frontier": list(self.recipient_frontier),
        }


@dataclass(frozen=True, slots=True)
class CausalAssessment:
    enforced: bool
    held: bool
    conversation_id: str | None
    missing_frontier: tuple[str, ...] = ()
    required_frontier_count: int = 0
    reason_code: str | None = None
    reason_detail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "format": CAUSAL_FORMAT,
            "enforced": self.enforced,
            "assessment": "held_for_clarification" if self.held else "current",
            "conversation_id": self.conversation_id,
            "missing_frontier": list(self.missing_frontier),
            "required_frontier_count": self.required_frontier_count,
            "reason_code": self.reason_code,
            "reason_detail": self.reason_detail,
        }


@dataclass(frozen=True, slots=True)
class _Intent:
    record_id: str
    sequence: int
    raw: bytes
    message_id: str
    message_type: str
    sender_participant_id: str
    recipient_participant_id: str
    renewal_of: str | None
    context: CausalContext | None


def _load_schema() -> dict[str, Any]:
    with _SCHEMA_PATH.open("r", encoding="utf-8") as handle:
        schema = cast(dict[str, Any], json.load(handle))
    Draft202012Validator.check_schema(schema)
    return schema


_VALIDATOR = Draft202012Validator(_load_schema(), format_checker=FormatChecker())


def _canonical_uuid(value: Any, *, field: str) -> str:
    if not isinstance(value, str):
        raise CausalError("causal.identifier", f"{field} must be a UUID string")
    try:
        canonical = str(uuid.UUID(value))
    except (ValueError, AttributeError):
        raise CausalError(
            "causal.identifier", f"{field} must be a valid UUID"
        ) from None
    if canonical != value:
        raise CausalError(
            "causal.identifier", f"{field} must use canonical lowercase UUID text"
        )
    return canonical


def _uuid_array(value: Any, *, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise CausalError("causal.context", f"{field} must be an array")
    if len(value) > MAX_CAUSAL_REFERENCES:
        raise CausalError(
            "causal.frontier_too_large",
            f"{field} exceeds {MAX_CAUSAL_REFERENCES} causal references",
        )
    result = tuple(_canonical_uuid(item, field=f"{field} item") for item in value)
    if result != tuple(sorted(set(result))):
        raise CausalError(
            "causal.context", f"{field} must be sorted and contain unique UUIDs"
        )
    return result


def parse_context(value: Any) -> CausalContext:
    """Validate one isolated journal causal-context object."""

    if not isinstance(value, dict):
        raise CausalError("causal.context", "causal_context must be an object")
    for name in ("depends_on", "supersedes", "recipient_frontier"):
        candidate = value.get(name)
        if isinstance(candidate, list) and len(candidate) > MAX_CAUSAL_REFERENCES:
            raise CausalError(
                "causal.frontier_too_large",
                f"{name} exceeds {MAX_CAUSAL_REFERENCES} causal references",
            )
    errors = sorted(
        _VALIDATOR.iter_errors(value),
        key=lambda error: tuple(str(item) for item in error.absolute_path),
    )
    if errors:
        path = ".".join(str(item) for item in errors[0].absolute_path)
        location = f" at {path}" if path else ""
        raise CausalError(
            "causal.context", f"causal context failed its schema{location}"
        )
    return CausalContext(
        conversation_id=_canonical_uuid(
            value["conversation_id"], field="conversation_id"
        ),
        depends_on=_uuid_array(value["depends_on"], field="depends_on"),
        supersedes=_uuid_array(value["supersedes"], field="supersedes"),
        recipient_frontier=_uuid_array(
            value["recipient_frontier"], field="recipient_frontier"
        ),
    )


def _participant_id(attributes: Mapping[str, Any], name: str) -> str:
    value = attributes.get(name)
    if name == "recipient_participant_id" and value is None:
        value = attributes.get("participant_id")
    return _canonical_uuid(value, field=name)


def _intent_from_record(record: Mapping[str, Any]) -> _Intent:
    attributes = record.get("attributes")
    raw = journal.decode_exact_message(record)
    if not isinstance(attributes, dict) or raw is None:
        raise CausalError(
            "causal.intent_invalid", "outbound intent lacks attributes or exact bytes"
        )
    envelope = parse_exact_bytes(raw)
    message_id = _canonical_uuid(envelope.get("message_id"), field="message_id")
    if attributes.get("message_id") != message_id:
        raise CausalError(
            "causal.intent_invalid",
            "outbound intent message ID does not match its bytes",
        )
    context_value = attributes.get("causal_context")
    raw_renewal = attributes.get("renewal_of")
    return _Intent(
        record_id=_canonical_uuid(record.get("record_id"), field="record_id"),
        sequence=cast(int, record["sequence"]),
        raw=raw,
        message_id=message_id,
        message_type=cast(str, envelope.get("type")),
        sender_participant_id=_participant_id(attributes, "sender_participant_id"),
        recipient_participant_id=_participant_id(
            attributes, "recipient_participant_id"
        ),
        renewal_of=(
            _canonical_uuid(raw_renewal, field="renewal_of")
            if raw_renewal is not None
            else None
        ),
        context=(parse_context(context_value) if context_value is not None else None),
    )


def _all_intents(records: Sequence[Mapping[str, Any]]) -> tuple[_Intent, ...]:
    intent_records = tuple(
        record
        for record in records
        if record.get("event_type") == "message.outbound.intent"
    )
    return tuple(_intent_from_record(record) for record in intent_records)


def _intents_by_message(intents: Sequence[_Intent]) -> dict[str, tuple[_Intent, ...]]:
    grouped: dict[str, list[_Intent]] = {}
    for intent in intents:
        grouped.setdefault(intent.message_id, []).append(intent)
    return {key: tuple(value) for key, value in grouped.items()}


def _consistent_intent(
    grouped: Mapping[str, tuple[_Intent, ...]], message_id: str
) -> _Intent:
    matches = grouped.get(message_id, ())
    if not matches:
        raise CausalError(
            "causal.reference_missing",
            "causal reference has no project outbound intent",
        )
    first = matches[0]
    if any(
        candidate.raw != first.raw
        or candidate.sender_participant_id != first.sender_participant_id
        or candidate.recipient_participant_id != first.recipient_participant_id
        or candidate.renewal_of != first.renewal_of
        or candidate.context != first.context
        for candidate in matches[1:]
    ):
        raise CausalError(
            "causal.intent_conflict",
            "repeated outbound intents disagree about causal identity",
        )
    return first


def _intent_index(intents: Sequence[_Intent]) -> dict[str, _Intent]:
    grouped = _intents_by_message(intents)
    return {
        message_id: _consistent_intent(grouped, message_id) for message_id in grouped
    }


def _referenced_intent(index: Mapping[str, _Intent], message_id: str) -> _Intent:
    referenced = index.get(message_id)
    if referenced is None:
        raise CausalError(
            "causal.reference_missing",
            "causal reference has no project outbound intent",
        )
    return referenced


def _same_pair(left: _Intent, right: _Intent) -> bool:
    return {
        left.sender_participant_id,
        left.recipient_participant_id,
    } == {right.sender_participant_id, right.recipient_participant_id}


def _validate_reference(
    intent: _Intent,
    referenced: _Intent,
    *,
    relationship: str,
) -> None:
    if referenced.sequence >= intent.sequence:
        raise CausalError(
            "causal.reference_order", "causal references must name prior intents"
        )
    if not _same_pair(intent, referenced):
        raise CausalError(
            "causal.participant_mismatch",
            "causal references must remain inside one two-party exchange",
        )
    if (
        referenced.context is None
        or intent.context is None
        or (referenced.context.conversation_id != intent.context.conversation_id)
    ):
        raise CausalError(
            "causal.conversation_mismatch",
            "causal references must remain inside one conversation",
        )
    if relationship == "supersedes" and (
        referenced.sender_participant_id != intent.sender_participant_id
    ):
        raise CausalError(
            "causal.supersedes_sender",
            "supersedes may reference only the sender's own outbound message",
        )
    if relationship == "recipient_frontier" and (
        referenced.sender_participant_id != intent.recipient_participant_id
        or referenced.recipient_participant_id != intent.sender_participant_id
    ):
        raise CausalError(
            "causal.frontier_sender",
            "recipient frontier may reference only recipient-authored messages",
        )


def _validate_intent_contexts(intents: Sequence[_Intent]) -> dict[str, _Intent]:
    index = _intent_index(intents)
    for intent in intents:
        if intent.context is None:
            continue
        reply_anchor = (
            _canonical_uuid(
                parse_exact_bytes(intent.raw).get("in_reply_to"),
                field="in_reply_to",
            )
            if intent.message_type in REPLY_TYPES or intent.message_type == "cancel"
            else None
        )
        expected_dependencies = (reply_anchor,) if reply_anchor is not None else ()
        expected_supersedes = (
            (intent.renewal_of,) if intent.renewal_of is not None else ()
        )
        if intent.context.depends_on != expected_dependencies:
            raise CausalError(
                "causal.dependency_shape",
                "reply and cancel intents must depend exactly on in_reply_to",
            )
        if intent.context.supersedes != expected_supersedes:
            raise CausalError(
                "causal.supersedes_shape",
                "renewal intents must supersede exactly their predecessor",
            )
        if (
            reply_anchor is None
            and intent.renewal_of is None
            and (intent.context.conversation_id != intent.message_id)
        ):
            raise CausalError(
                "causal.root_conversation",
                "a new root conversation ID must equal its own message ID",
            )
        relationships = (
            ("depends_on", intent.context.depends_on),
            ("supersedes", intent.context.supersedes),
            ("recipient_frontier", intent.context.recipient_frontier),
        )
        for relationship, identifiers in relationships:
            for identifier in identifiers:
                _validate_reference(
                    intent,
                    _referenced_intent(index, identifier),
                    relationship=relationship,
                )
    return index


def _activation_sequence(records: Sequence[Mapping[str, Any]]) -> int | None:
    for record in records:
        if record.get("event_type") != "state.compatibility.gate_activated":
            continue
        attributes = record.get("attributes")
        if not isinstance(attributes, dict) or (
            attributes.get("feature_id") != CAUSAL_FEATURE_ID
        ):
            continue
        if attributes.get("feature_version") != CAUSAL_FEATURE_VERSION:
            return None
        # The compatibility projection treats an exact repeated activation as
        # idempotent. Preserve the first effective cutoff rather than moving
        # it forward to the duplicate journal record.
        return cast(int, record["sequence"])
    return None


def _validated_inbound_ids(
    records: Sequence[Mapping[str, Any]],
    *,
    sender_participant_id: str,
    recipient_participant_id: str,
) -> frozenset[str]:
    identifiers: set[str] = set()
    for record in records:
        if record.get("event_type") != "message.inbound.validated":
            continue
        attributes = record.get("attributes")
        if not isinstance(attributes, dict) or (
            attributes.get("recipient_participant_id") != sender_participant_id
            or attributes.get("sender_participant_id") != recipient_participant_id
        ):
            continue
        identifiers.add(
            _canonical_uuid(attributes.get("message_id"), field="message_id")
        )
    return frozenset(identifiers)


def _frontier(
    candidate_ids: Sequence[str], contexts: Mapping[str, CausalContext]
) -> tuple[str, ...]:
    """Return maximal candidates with one iterative traversal of their ancestry."""

    heads: list[str] = []
    considered: set[str] = set()
    covered: set[str] = set()
    for candidate in reversed(candidate_ids):
        if candidate in considered:
            continue
        considered.add(candidate)
        if candidate in covered:
            continue
        heads.append(candidate)
        if len(heads) > MAX_CAUSAL_REFERENCES:
            raise CausalError(
                "causal.frontier_too_large",
                "conversation has more than 64 concurrent causal frontier entries",
            )
        stack = [candidate]
        while stack:
            current = stack.pop()
            if current in covered:
                continue
            covered.add(current)
            context = contexts.get(current)
            if context is None:
                continue
            stack.extend(
                context.depends_on + context.supersedes + context.recipient_frontier
            )
    return tuple(sorted(heads))


def _recipient_frontier(
    records: Sequence[Mapping[str, Any]],
    intents: Sequence[_Intent],
    *,
    conversation_id: str,
    sender_participant_id: str,
    recipient_participant_id: str,
) -> tuple[str, ...]:
    validated = _validated_inbound_ids(
        records,
        sender_participant_id=sender_participant_id,
        recipient_participant_id=recipient_participant_id,
    )
    candidates = [
        intent.message_id
        for intent in intents
        if intent.message_id in validated
        and intent.sender_participant_id == recipient_participant_id
        and intent.recipient_participant_id == sender_participant_id
        and intent.context is not None
        and intent.context.conversation_id == conversation_id
    ]
    contexts = {
        intent.message_id: intent.context
        for intent in intents
        if intent.context is not None
        and intent.context.conversation_id == conversation_id
    }
    return _frontier(candidates, cast(Mapping[str, CausalContext], contexts))


def _anchor_context(
    index: Mapping[str, _Intent],
    anchor_id: str,
    *,
    sender_participant_id: str,
    recipient_participant_id: str,
    activation_sequence: int,
    supersedes: bool,
) -> CausalContext | None:
    anchor = _referenced_intent(index, anchor_id)
    if {anchor.sender_participant_id, anchor.recipient_participant_id} != {
        sender_participant_id,
        recipient_participant_id,
    }:
        raise CausalError(
            "causal.participant_mismatch",
            "conversation anchor belongs to different project participants",
        )
    if supersedes and anchor.sender_participant_id != sender_participant_id:
        raise CausalError(
            "causal.supersedes_sender",
            "a renewal may supersede only the sender's own outbound root",
        )
    if anchor.context is None:
        if _contextless_intent_is_grandfathered(
            index,
            anchor,
            activation_sequence=activation_sequence,
        ):
            return None
        raise CausalError(
            "causal.context_missing",
            "post-activation conversation anchor lacks causal context",
        )
    return anchor.context


def _contextless_intent_is_grandfathered(
    index: Mapping[str, _Intent],
    intent: _Intent,
    *,
    activation_sequence: int,
) -> bool:
    """Trace a contextless legacy conversation to its pre-activation root."""

    initial = intent
    current = intent
    visited: set[str] = set()
    while current.context is None:
        if current.sequence <= activation_sequence:
            return True
        if current.message_id in visited:
            raise CausalError(
                "causal.reference_order",
                "contextless conversation ancestry contains a cycle",
            )
        visited.add(current.message_id)

        supersedes = current.renewal_of is not None
        predecessor_id = current.renewal_of
        if predecessor_id is None and (
            current.message_type in REPLY_TYPES or current.message_type == "cancel"
        ):
            predecessor_id = _canonical_uuid(
                parse_exact_bytes(current.raw).get("in_reply_to"),
                field="in_reply_to",
            )
        if predecessor_id is None:
            return False
        predecessor = _referenced_intent(index, predecessor_id)
        if predecessor.sequence >= current.sequence:
            raise CausalError(
                "causal.reference_order",
                "contextless conversation ancestry must name prior intents",
            )
        if not _same_pair(initial, predecessor):
            raise CausalError(
                "causal.participant_mismatch",
                "contextless conversation ancestry changed project participants",
            )
        if supersedes and (
            predecessor.sender_participant_id != current.sender_participant_id
        ):
            raise CausalError(
                "causal.supersedes_sender",
                "contextless renewal ancestry must remain sender-owned",
            )
        current = predecessor
    return False


def build_outbound_context(
    records: Sequence[Mapping[str, Any]],
    envelope: Mapping[str, Any],
    *,
    sender_participant_id: str,
    recipient_participant_id: str,
    renewal_of: str | None,
    retry_after_intent: str | None,
) -> CausalContext | None:
    """Derive causal intent metadata without accepting a caller frontier."""

    activation = _activation_sequence(records)
    if activation is None:
        return None
    sender_id = _canonical_uuid(sender_participant_id, field="sender_participant_id")
    recipient_id = _canonical_uuid(
        recipient_participant_id, field="recipient_participant_id"
    )
    if sender_id == recipient_id:
        raise CausalError(
            "causal.participant_mismatch",
            "causal conversations require two distinct project participants",
        )
    intents = _all_intents(records)
    index = _validate_intent_contexts(intents)
    if retry_after_intent is not None:
        retry_id = _canonical_uuid(retry_after_intent, field="retry_after_intent")
        matches = [intent for intent in intents if intent.record_id == retry_id]
        if len(matches) != 1:
            raise CausalError(
                "causal.retry_context",
                "retry intent does not identify one reusable causal context",
            )
        prior = matches[0]
        if (
            prior.sender_participant_id != sender_id
            or prior.recipient_participant_id != recipient_id
        ):
            raise CausalError(
                "causal.retry_context", "retry intent participants do not match"
            )
        if prior.context is None:
            if _contextless_intent_is_grandfathered(
                index,
                prior,
                activation_sequence=activation,
            ):
                return None
            raise CausalError(
                "causal.retry_context",
                "post-activation retry intent lacks causal context",
            )
        return deepcopy(prior.context)

    message_id = _canonical_uuid(envelope.get("message_id"), field="message_id")
    message_type = envelope.get("type")
    in_reply_to = envelope.get("in_reply_to")
    depends_on: tuple[str, ...] = ()
    supersedes_ids: tuple[str, ...] = ()
    anchor_context: CausalContext | None = None
    if message_type in REPLY_TYPES or message_type == "cancel":
        reply_id = _canonical_uuid(in_reply_to, field="in_reply_to")
        anchor_context = _anchor_context(
            index,
            reply_id,
            sender_participant_id=sender_id,
            recipient_participant_id=recipient_id,
            activation_sequence=activation,
            supersedes=False,
        )
        depends_on = (reply_id,)
    if renewal_of is not None:
        renewal_id = _canonical_uuid(renewal_of, field="renewal_of")
        renewal_context = _anchor_context(
            index,
            renewal_id,
            sender_participant_id=sender_id,
            recipient_participant_id=recipient_id,
            activation_sequence=activation,
            supersedes=True,
        )
        if (
            anchor_context is not None
            and renewal_context is not None
            and anchor_context.conversation_id != renewal_context.conversation_id
        ):
            raise CausalError(
                "causal.conversation_mismatch",
                "cancel target and renewal predecessor belong to different conversations",
            )
        anchor_context = anchor_context or renewal_context
        supersedes_ids = (renewal_id,)

    if (message_type in REPLY_TYPES or message_type == "cancel" or renewal_of) and (
        anchor_context is None
    ):
        return None
    conversation_id = (
        anchor_context.conversation_id if anchor_context is not None else message_id
    )
    context = CausalContext(
        conversation_id=conversation_id,
        depends_on=tuple(sorted(depends_on)),
        supersedes=tuple(sorted(supersedes_ids)),
        recipient_frontier=_recipient_frontier(
            records,
            intents,
            conversation_id=conversation_id,
            sender_participant_id=sender_id,
            recipient_participant_id=recipient_id,
        ),
    )
    parse_context(context.as_dict())
    return context


def _possible_dispatches(
    records: Sequence[Mapping[str, Any]], intents: Sequence[_Intent]
) -> frozenset[str]:
    outcomes: dict[str, list[Mapping[str, Any]]] = {}
    for record in records:
        if record.get("event_type") not in {
            "transport.accepted",
            "transport.not_accepted",
        }:
            continue
        attributes = record.get("attributes")
        if isinstance(attributes, dict) and isinstance(
            attributes.get("intent_record_id"), str
        ):
            outcomes.setdefault(attributes["intent_record_id"], []).append(record)
    possible: set[str] = set()
    for intent in intents:
        linked = outcomes.get(intent.record_id, [])
        conclusively_not_attempted = (
            len(linked) == 1
            and linked[0].get("event_type") == "transport.not_accepted"
            and isinstance(linked[0].get("attributes"), dict)
            and linked[0]["attributes"].get("delivery_state") == "not_attempted"
        )
        if not conclusively_not_attempted:
            possible.add(intent.message_id)
    return frozenset(possible)


def assess_inbound_order(
    records: Sequence[Mapping[str, Any]],
    raw: bytes,
    *,
    local_participant_id: str,
    sender_participant_id: str,
) -> CausalAssessment:
    """Hold stale request/cancel instructions after the feature gate activates."""

    activation = _activation_sequence(records)
    envelope = parse_exact_bytes(raw)
    if activation is None or envelope.get("type") not in {"request", "cancel"}:
        return CausalAssessment(False, False, None)
    local_id = _canonical_uuid(local_participant_id, field="local_participant_id")
    sender_id = _canonical_uuid(sender_participant_id, field="sender_participant_id")
    if local_id == sender_id:
        raise CausalError(
            "causal.participant_mismatch",
            "causal conversations require two distinct project participants",
        )
    intents = _all_intents(records)
    index = _validate_intent_contexts(intents)
    exact = [intent for intent in intents if intent.raw == raw]
    if not exact:
        return CausalAssessment(
            True,
            True,
            None,
            reason_code="causal.intent_missing",
            reason_detail=(
                "post-activation instruction has no exact outbound-intent context"
            ),
        )
    current = exact[-1]
    if (
        current.sender_participant_id != sender_id
        or current.recipient_participant_id != local_id
    ):
        raise CausalError(
            "causal.participant_mismatch",
            "correlated outbound intent names different participants",
        )
    if min(intent.sequence for intent in exact) <= activation:
        return CausalAssessment(False, False, None)
    if current.context is None:
        if _contextless_intent_is_grandfathered(
            index,
            current,
            activation_sequence=activation,
        ):
            return CausalAssessment(False, False, None)
        return CausalAssessment(
            True,
            True,
            None,
            reason_code="causal.context_missing",
            reason_detail="post-activation instruction lacks journal causal context",
        )
    first_sequence = min(
        intent.sequence
        for intent in intents
        if intent.context is not None
        and intent.context.conversation_id == current.context.conversation_id
        and _same_pair(intent, current)
    )
    if first_sequence <= activation:
        return CausalAssessment(False, False, current.context.conversation_id)

    possible = _possible_dispatches(records, intents)
    candidates = [
        intent.message_id
        for intent in intents
        if intent.message_id in possible
        and intent.sender_participant_id == local_id
        and intent.recipient_participant_id == sender_id
        and intent.context is not None
        and intent.context.conversation_id == current.context.conversation_id
    ]
    contexts = {
        intent.message_id: intent.context
        for intent in intents
        if intent.context is not None
        and intent.context.conversation_id == current.context.conversation_id
    }
    required = _frontier(candidates, cast(Mapping[str, CausalContext], contexts))
    missing = tuple(sorted(set(required) - set(current.context.recipient_frontier)))
    return CausalAssessment(
        enforced=True,
        held=bool(missing),
        conversation_id=current.context.conversation_id,
        missing_frontier=missing,
        required_frontier_count=len(required),
        reason_code="causal.stale_instruction" if missing else None,
        reason_detail=(
            "instruction does not cover the receiver's potentially dispatched frontier"
            if missing
            else None
        ),
    )
