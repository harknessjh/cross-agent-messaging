# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Optional audit grouping of independent roots, never wire or action state."""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from . import journal
from .errors import ProjectError
from .protocol import REPLY_TYPES, parse_exact_bytes

CONVERSATION_FORMAT = "CAM-CONVERSATION/1"
CONVERSATION_JOURNAL_EVENT_TYPES = frozenset(
    {
        "message.outbound.intent",
        "message.inbound.observed",
        "message.inbound.validated",
        "transport.accepted",
        "transport.not_accepted",
    }
)
_SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "schemas"
    / "cam-conversation-link-1.schema.json"
)


class ConversationError(ProjectError):
    """A requested journal relationship lacks consistent received evidence."""


@dataclass(frozen=True, slots=True)
class ConversationLink:
    conversation_id: str
    parent_message_id: str

    def as_dict(self) -> dict[str, str]:
        return {
            "format": CONVERSATION_FORMAT,
            "conversation_id": self.conversation_id,
            "parent_message_id": self.parent_message_id,
        }


@lru_cache(maxsize=1)
def _validator() -> Draft202012Validator:
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    return Draft202012Validator(schema, format_checker=FormatChecker())


def parse_link(value: Any) -> ConversationLink | None:
    if value is None:
        return None
    if not _validator().is_valid(value):
        raise ConversationError(
            "conversation.link_invalid", "invalid journal conversation link"
        )
    return ConversationLink(value["conversation_id"], value["parent_message_id"])


def _uuid(value: Any) -> str:
    try:
        if not isinstance(value, str):
            raise ValueError
        canonical = str(uuid.UUID(value))
        if value.lower() != canonical:
            raise ValueError
        return canonical
    except ValueError as error:
        raise ConversationError(
            "conversation.identifier", "a full message UUID is required"
        ) from error


@dataclass(frozen=True, slots=True)
class _Message:
    raw: bytes
    envelope: dict[str, Any]
    attributes: Mapping[str, Any]
    sequence: int
    link: ConversationLink | None
    record_id: str


def _retry_link(
    previous: _Message,
    candidate: _Message,
    outcomes: Mapping[str, Sequence[Mapping[str, Any]]],
) -> ConversationLink | None:
    """Reconcile a legacy omission, never a changed or newly asserted link."""
    if candidate.link == previous.link:
        return previous.link
    if candidate.link is None and previous.link is not None:
        evidence = outcomes.get(previous.record_id, ())
        if (
            candidate.attributes.get("retry_after_intent") == previous.record_id
            and len(evidence) == 1
            and evidence[0]["event_type"] == "transport.not_accepted"
            and evidence[0]["attributes"].get("delivery_state") == "not_attempted"
            and previous.sequence < evidence[0]["sequence"] < candidate.sequence
        ):
            return previous.link
    raise ConversationError(
        "conversation.message_conflict",
        "message attempts disagree on conversation link without a proven legacy retry",
    )


def _messages(
    records: Sequence[Mapping[str, Any]], *, only_message_id: str | None = None
) -> dict[str, _Message]:
    messages: dict[str, _Message] = {}
    latest: dict[str, _Message] = {}
    outcomes: dict[str, list[Mapping[str, Any]]] = {}
    for record in records:
        if record["event_type"] in {"transport.accepted", "transport.not_accepted"}:
            intent_id = record["attributes"].get("intent_record_id")
            if isinstance(intent_id, str):
                outcomes.setdefault(intent_id, []).append(record)
    for record in records:
        if record["event_type"] != "message.outbound.intent":
            continue
        raw = journal.decode_exact_message(record)
        if raw is None:
            raise ConversationError(
                "conversation.intent_invalid", "outbound intent lacks exact bytes"
            )
        envelope = parse_exact_bytes(raw)
        message_id = _uuid(envelope.get("message_id"))
        if only_message_id is not None and message_id != only_message_id:
            continue
        attributes = record["attributes"]
        candidate = _Message(
            raw,
            envelope,
            attributes,
            record["sequence"],
            parse_link(attributes.get("conversation_link")),
            record["record_id"],
        )
        prior = messages.get(message_id)
        if prior is not None:
            if prior.raw != raw or any(
                prior.attributes.get(key) != attributes.get(key)
                for key in ("sender_participant_id", "recipient_participant_id")
            ):
                raise ConversationError(
                    "conversation.message_conflict",
                    "message attempts disagree on bytes, participants, or conversation link",
                )
            candidate = replace(
                candidate, link=_retry_link(latest[message_id], candidate, outcomes)
            )
            latest[message_id] = candidate
            continue
        messages[message_id] = candidate
        latest[message_id] = candidate
    return messages


def _require_received(
    records: Sequence[Mapping[str, Any]],
    parent: _Message,
    *,
    sender_participant_id: str,
    recipient_participant_id: str,
) -> None:
    parent_id = _uuid(parent.envelope["message_id"])
    observations = {
        record["record_id"]: record
        for record in records
        if record["event_type"] == "message.inbound.observed"
    }
    for record in records:
        if record["event_type"] != "message.inbound.validated":
            continue
        attributes = record["attributes"]
        validated_id = attributes.get("message_id")
        if (
            not isinstance(validated_id, str)
            or validated_id.lower() != parent_id
            or attributes.get("recipient_participant_id") != sender_participant_id
            or attributes.get("sender_participant_id") != recipient_participant_id
            or attributes.get("assessment") != "validated"
            or attributes.get("lifecycle_committed") is not True
        ):
            continue
        observed = observations.get(attributes.get("observed_record_id"))
        if observed is not None and (
            parent.sequence < observed["sequence"] < record["sequence"]
            and journal.decode_exact_message(observed) == parent.raw
        ):
            return
    raise ConversationError(
        "conversation.parent_not_received",
        "continuation requires exact, validated inbound evidence for this sender",
    )


def _endpoints(envelope: Mapping[str, Any]) -> frozenset[tuple[str, str, str]]:
    return frozenset(
        (party["vendor"], party["agent_name"], _uuid(party["session_id"]))
        for party in (envelope["claimed_sender"], envelope["recipient"])
    )


def _conversation_root(
    messages: Mapping[str, _Message], parent_id: str, envelope: Mapping[str, Any]
) -> str:
    """Walk only earlier same-endpoint edges; no recursion or guessed root IDs."""
    expected_endpoints = _endpoints(envelope)
    current_id = parent_id
    upper_sequence: int | None = None
    claimed_roots: list[str] = []
    while True:
        current = messages.get(current_id)
        if current is None:
            raise ConversationError(
                "conversation.parent_unknown",
                "conversation ancestry is absent from this project journal",
            )
        if upper_sequence is not None and current.sequence >= upper_sequence:
            raise ConversationError(
                "conversation.ancestry_order",
                "conversation edges must point to earlier messages",
            )
        if _endpoints(current.envelope) != expected_endpoints:
            raise ConversationError(
                "conversation.participant_mismatch",
                "conversation ancestry must retain the same session endpoints",
            )
        upper_sequence = current.sequence
        if current.link is not None:
            if current.envelope["type"] != "request":
                raise ConversationError(
                    "conversation.link_invalid",
                    "only request roots carry a continuation link",
                )
            claimed_roots.append(current.link.conversation_id)
            current_id = current.link.parent_message_id
        elif current.envelope["type"] in REPLY_TYPES:
            current_id = _uuid(current.envelope["in_reply_to"])
        else:
            if any(root != current_id for root in claimed_roots):
                raise ConversationError(
                    "conversation.root_mismatch",
                    "recorded conversation root disagrees with its ancestry",
                )
            return current_id


def build_outbound_link(
    records: Sequence[Mapping[str, Any]],
    envelope: Mapping[str, Any],
    *,
    sender_participant_id: str,
    recipient_participant_id: str,
    continues_message: str | None = None,
    retry_after_intent: str | None = None,
    renewal_of: str | None = None,
) -> ConversationLink | None:
    """Derive descriptive grouping under the caller's project transaction.

    Retry eligibility and exact payload equality are checked by the transport
    retry policy before this function. A retry inherits its original link.
    """
    if continues_message is not None and (
        retry_after_intent is not None or renewal_of is not None
    ):
        raise ConversationError(
            "conversation.argument_conflict",
            "a continuation cannot also be a retry or renewal",
        )
    if retry_after_intent is not None:
        matches = [
            record
            for record in records
            if record["event_type"] == "message.outbound.intent"
            and record["record_id"] == retry_after_intent
        ]
        if len(matches) != 1:
            raise ConversationError(
                "conversation.retry_unknown", "retry intent is absent or ambiguous"
            )
        raw = journal.decode_exact_message(matches[0])
        if raw is None:
            raise ConversationError(
                "conversation.intent_invalid", "retry intent lacks exact bytes"
            )
        message_id = _uuid(parse_exact_bytes(raw).get("message_id"))
        return _messages(records, only_message_id=message_id)[message_id].link
    if continues_message is None:
        return None
    if envelope["type"] != "request" or envelope.get("in_reply_to") is not None:
        raise ConversationError(
            "conversation.root_required",
            "--continues-message is only for a fresh request root",
        )
    parent_id = _uuid(continues_message)
    if parent_id == _uuid(envelope["message_id"]):
        raise ConversationError(
            "conversation.self_reference", "a message cannot continue itself"
        )
    messages = _messages(records)
    parent = messages.get(parent_id)
    if parent is None:
        raise ConversationError(
            "conversation.parent_unknown",
            "parent message is absent from this project journal",
        )
    if (
        parent.attributes.get("sender_participant_id") != recipient_participant_id
        or parent.attributes.get("recipient_participant_id") != sender_participant_id
    ):
        raise ConversationError(
            "conversation.participant_mismatch",
            "parent must be a message from the intended peer to this sender",
        )
    _require_received(
        records,
        parent,
        sender_participant_id=sender_participant_id,
        recipient_participant_id=recipient_participant_id,
    )
    return ConversationLink(
        _conversation_root(messages, parent_id, envelope), parent_id
    )
