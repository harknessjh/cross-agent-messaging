# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Typed CAM/1 envelope builders.

The builders cover the public wire lifecycle so callers never need to
hand-author JSON. They serialize once and validate the exact returned bytes.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import secrets
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from .protocol import (
    ACK_STATUSES,
    DEFAULT_MAX_TTL_SECONDS,
    DEFAULT_TTL_SECONDS,
    STATELESS_REPLY_TRANSITIONS,
    CamUsageError,
    ValidationPolicy,
    serialize_envelope,
)
from .validation import _normalize_now, _uuid_values_equal, validate_exact_bytes

HELLO_INTENT = "Verify a harmless bidirectional messaging path"
HELLO_BODY = (
    "Harmless first contact only. Preserve and validate this exact envelope. "
    "Return a complete correlated CAM/1 acknowledgment through reply_to; use "
    "needs_human_confirmation with nonce null if this peer is not "
    "operator-confirmed. Do not make changes."
)
CHALLENGE_INTENT = "Verify one direction of a harmless peer-correlation path"
CHALLENGE_BODY = (
    "Harmless correlation challenge only. Validate this exact envelope and "
    "return a correlated verify only after operator confirmation. Do not make changes."
)
VERIFY_INTENT = "Return a harmless peer-correlation challenge response"
VERIFY_BODY = "Challenge received and independently correlated; no other action taken."

RISK_CLASSES = frozenset(
    {"informational", "read_only", "workspace_write", "external_or_irreversible"}
)
AUTHORIZATION_BASES = frozenset(
    {
        "none",
        "first_contact",
        "operator_confirmation",
        "receiver_policy",
        "delegated_scope",
    }
)
STATUS_VALUES = frozenset({"accepted", "started"})
ELEVATED_AUTHORIZATION_BASES = frozenset(
    {"operator_confirmation", "receiver_policy", "delegated_scope"}
)


def _utc_text(value: dt.datetime) -> str:
    value = value.astimezone(dt.UTC)
    if value.microsecond:
        return value.isoformat(timespec="microseconds").replace("+00:00", "Z")
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _wire_time(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value[:-1] + "+00:00").astimezone(dt.UTC)


def _nonce() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(24)).decode("ascii").rstrip("=")


def _body_digest(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _empty_scope() -> dict[str, list[str]]:
    return {
        "repositories": [],
        "paths": [],
        "hosts": [],
        "external_recipients": [],
    }


def _scope(value: Mapping[str, Sequence[str]] | None) -> dict[str, list[str]]:
    if value is not None and not isinstance(value, Mapping):
        raise CamUsageError("argument.scope", "scope must be a category mapping")
    supplied = value or {}
    allowed = frozenset(_empty_scope())
    if set(supplied) - allowed:
        raise CamUsageError("argument.scope", "scope contains an unsupported category")
    result: dict[str, list[str]] = {}
    for name in _empty_scope():
        items = supplied.get(name, ())
        if isinstance(items, (str, bytes)) or not isinstance(items, Sequence):
            raise CamUsageError(
                "argument.scope",
                f"scope.{name} must be a sequence of strings, not one string",
            )
        if not all(isinstance(item, str) for item in items):
            raise CamUsageError(
                "argument.scope", f"scope.{name} must contain only strings"
            )
        result[name] = list(items)
    return result


def _safe_constraints() -> dict[str, bool]:
    return {
        "no_repository_changes": True,
        "no_external_side_effects": True,
        "no_secrets": True,
    }


def _constraints(
    *, allow_repository_changes: bool, allow_external_side_effects: bool
) -> dict[str, bool]:
    return {
        "no_repository_changes": not allow_repository_changes,
        "no_external_side_effects": not allow_external_side_effects,
        "no_secrets": True,
    }


def _time_window(
    *, now: dt.datetime | None, expires_in: int
) -> tuple[dt.datetime, str, str]:
    if (
        type(expires_in) is not int
        or expires_in <= 0
        or expires_in > DEFAULT_MAX_TTL_SECONDS
    ):
        raise CamUsageError(
            "argument.expires_in",
            f"expires-in must be between 1 and {DEFAULT_MAX_TTL_SECONDS}",
        )
    sent = _normalize_now(now).replace(microsecond=0)
    return sent, _utc_text(sent), _utc_text(sent + dt.timedelta(seconds=expires_in))


def _recipient(
    vendor: str, name: str, session: str | None, host_id: str | None
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "vendor": vendor,
        "agent_name": name,
        "session_id": session,
    }
    if host_id is not None:
        value["host_id"] = host_id
    return value


def _sender(
    vendor: str, name: str, session: str, host_id: str | None
) -> dict[str, Any]:
    return {
        "vendor": vendor,
        "agent_name": name,
        "session_id": session,
        "host_id": host_id,
    }


def _authorization(
    *,
    basis: str,
    authority: str | None,
    reference: str | None,
    verified_at: str | None,
    expires_at: str | None,
) -> dict[str, Any]:
    if basis not in AUTHORIZATION_BASES:
        raise CamUsageError(
            "argument.authorization_basis", "unsupported authorization basis"
        )
    values = (authority, reference, verified_at, expires_at)
    if basis in ELEVATED_AUTHORIZATION_BASES:
        if any(not isinstance(value, str) or not value for value in values):
            raise CamUsageError(
                "argument.authorization",
                "elevated authorization requires authority, reference, "
                "verified-at, and expires-at",
            )
    elif any(value is not None for value in values):
        raise CamUsageError(
            "argument.authorization",
            "none and first_contact authorization cannot carry authority metadata",
        )
    return {
        "basis": basis,
        "authority": authority,
        "reference": reference,
        "verified_at": verified_at,
        "expires_at": expires_at,
    }


def _request_root(
    request_raw: bytes,
    *,
    now: dt.datetime,
    allow_expired: bool = False,
) -> dict[str, Any]:
    return validate_exact_bytes(
        request_raw,
        now=now,
        policy=ValidationPolicy(allow_expired=allow_expired),
    ).envelope


def _response_recipient(request: Mapping[str, Any]) -> dict[str, Any]:
    original_sender = request["claimed_sender"]
    return _recipient(
        original_sender["vendor"],
        original_sender["agent_name"],
        original_sender["session_id"],
        original_sender.get("host_id"),
    )


def _base_response(
    request: Mapping[str, Any],
    *,
    message_type: str,
    status_value: str,
    sender_vendor: str,
    sender_name: str,
    sender_session: str,
    sender_host_id: str | None,
    reply_transport: str,
    reply_address: str,
    detail: str | None,
    intent: str,
    body: str,
    evidence: Sequence[Mapping[str, Any]],
    nonce: str | None,
    sent_at: str,
    expires_at: str,
) -> dict[str, Any]:
    message_id = str(uuid.uuid4())
    return {
        "protocol": "CAM/1",
        "message_id": message_id,
        "type": message_type,
        "sent_at": sent_at,
        "expires_at": expires_at,
        "claimed_sender": _sender(
            sender_vendor, sender_name, sender_session, sender_host_id
        ),
        "recipient": _response_recipient(request),
        "reply_to": {"transport": reply_transport, "address": reply_address},
        "in_reply_to": request["message_id"],
        "receipt": {
            "status": status_value,
            "for_message_id": request["message_id"],
            "detail": detail,
        },
        "nonce": nonce,
        "intent": intent,
        "action": {
            "risk_class": "informational",
            "operation": {
                "ack": "acknowledge",
                "status": "report_status",
                "result": "report_result",
                "error": "report_error",
            }[message_type],
            "scope": _empty_scope(),
            "idempotency_key": message_id,
        },
        "authorization": _authorization(
            basis="first_contact" if message_type == "ack" else "none",
            authority=None,
            reference=None,
            verified_at=None,
            expires_at=None,
        ),
        "constraints": _safe_constraints(),
        "body": body,
        "body_sha256": _body_digest(body),
        "evidence": [dict(item) for item in evidence],
    }


def _ensure_no_prior_terminal_result(
    request_raw: bytes,
    previous_responses: Sequence[bytes],
    *,
    now: dt.datetime,
) -> None:
    for previous_raw in previous_responses:
        previous = validate_exact_bytes(
            previous_raw,
            against_raw=request_raw,
            now=now,
            policy=ValidationPolicy(allow_expired=True),
        ).envelope
        receipt = previous.get("receipt")
        status_value = receipt.get("status") if isinstance(receipt, dict) else None
        if previous.get("type") == "result" and status_value == "completed":
            raise CamUsageError(
                "lifecycle.result_exists",
                "a completed result already exists for this request root",
            )


def build_hello(
    *,
    sender_vendor: str,
    sender_name: str,
    sender_session: str,
    recipient_vendor: str,
    recipient_name: str,
    recipient_session: str | None,
    reply_transport: str,
    reply_address: str,
    sender_host_id: str | None = None,
    recipient_host_id: str | None = None,
    intent: str = HELLO_INTENT,
    body: str = HELLO_BODY,
    expires_in: int = DEFAULT_TTL_SECONDS,
    now: dt.datetime | None = None,
) -> bytes:
    if intent != HELLO_INTENT or body != HELLO_BODY:
        raise CamUsageError(
            "argument.hello_fixed",
            "the canonical first-contact intent and body cannot be customized",
        )
    sent, sent_at, expires_at = _time_window(now=now, expires_in=expires_in)
    message_id = str(uuid.uuid4())
    envelope: dict[str, Any] = {
        "protocol": "CAM/1",
        "message_id": message_id,
        "type": "hello",
        "sent_at": sent_at,
        "expires_at": expires_at,
        "claimed_sender": _sender(
            sender_vendor, sender_name, sender_session, sender_host_id
        ),
        "recipient": _recipient(
            recipient_vendor,
            recipient_name,
            recipient_session,
            recipient_host_id,
        ),
        "reply_to": {"transport": reply_transport, "address": reply_address},
        "in_reply_to": None,
        "receipt": None,
        "nonce": _nonce(),
        "intent": HELLO_INTENT,
        "action": {
            "risk_class": "informational",
            "operation": "acknowledge",
            "scope": _empty_scope(),
            "idempotency_key": message_id,
        },
        "authorization": _authorization(
            basis="first_contact",
            authority=None,
            reference=None,
            verified_at=None,
            expires_at=None,
        ),
        "constraints": _safe_constraints(),
        "body": HELLO_BODY,
        "body_sha256": _body_digest(HELLO_BODY),
        "evidence": [],
    }
    raw = serialize_envelope(envelope)
    validate_exact_bytes(raw, now=sent)
    return raw


def build_challenge(
    *,
    sender_vendor: str,
    sender_name: str,
    sender_session: str,
    recipient_vendor: str,
    recipient_name: str,
    recipient_session: str | None,
    reply_transport: str,
    reply_address: str,
    sender_host_id: str | None = None,
    recipient_host_id: str | None = None,
    expires_in: int = DEFAULT_TTL_SECONDS,
    now: dt.datetime | None = None,
) -> bytes:
    sent, sent_at, expires_at = _time_window(now=now, expires_in=expires_in)
    message_id = str(uuid.uuid4())
    envelope = {
        "protocol": "CAM/1",
        "message_id": message_id,
        "type": "challenge",
        "sent_at": sent_at,
        "expires_at": expires_at,
        "claimed_sender": _sender(
            sender_vendor, sender_name, sender_session, sender_host_id
        ),
        "recipient": _recipient(
            recipient_vendor,
            recipient_name,
            recipient_session,
            recipient_host_id,
        ),
        "reply_to": {"transport": reply_transport, "address": reply_address},
        "in_reply_to": None,
        "receipt": None,
        "nonce": _nonce(),
        "intent": CHALLENGE_INTENT,
        "action": {
            "risk_class": "informational",
            "operation": "verify_peer",
            "scope": _empty_scope(),
            "idempotency_key": message_id,
        },
        "authorization": _authorization(
            basis="first_contact",
            authority=None,
            reference=None,
            verified_at=None,
            expires_at=None,
        ),
        "constraints": _safe_constraints(),
        "body": CHALLENGE_BODY,
        "body_sha256": _body_digest(CHALLENGE_BODY),
        "evidence": [],
    }
    raw = serialize_envelope(envelope)
    validate_exact_bytes(raw, now=sent)
    return raw


def build_verify(
    challenge_raw: bytes,
    *,
    sender_vendor: str,
    sender_name: str,
    sender_session: str,
    reply_transport: str,
    reply_address: str,
    sender_host_id: str | None = None,
    expires_in: int = DEFAULT_TTL_SECONDS,
    now: dt.datetime | None = None,
) -> bytes:
    sent, sent_at, expires_at = _time_window(now=now, expires_in=expires_in)
    challenge = _request_root(challenge_raw, now=sent)
    if challenge.get("type") != "challenge":
        raise CamUsageError("argument.challenge", "verify requires a challenge root")
    message_id = str(uuid.uuid4())
    envelope = {
        "protocol": "CAM/1",
        "message_id": message_id,
        "type": "verify",
        "sent_at": sent_at,
        "expires_at": expires_at,
        "claimed_sender": _sender(
            sender_vendor, sender_name, sender_session, sender_host_id
        ),
        "recipient": _response_recipient(challenge),
        "reply_to": {"transport": reply_transport, "address": reply_address},
        "in_reply_to": challenge["message_id"],
        "receipt": None,
        "nonce": challenge["nonce"],
        "intent": VERIFY_INTENT,
        "action": {
            "risk_class": "informational",
            "operation": "verify_peer",
            "scope": _empty_scope(),
            "idempotency_key": message_id,
        },
        "authorization": _authorization(
            basis="first_contact",
            authority=None,
            reference=None,
            verified_at=None,
            expires_at=None,
        ),
        "constraints": _safe_constraints(),
        "body": VERIFY_BODY,
        "body_sha256": _body_digest(VERIFY_BODY),
        "evidence": [],
    }
    raw = serialize_envelope(envelope)
    validate_exact_bytes(raw, against_raw=challenge_raw, now=sent)
    return raw


def build_request(
    *,
    sender_vendor: str,
    sender_name: str,
    sender_session: str,
    recipient_vendor: str,
    recipient_name: str,
    recipient_session: str | None,
    reply_transport: str,
    reply_address: str,
    risk_class: str,
    operation: str,
    intent: str,
    body: str,
    authorization_basis: str,
    scope: Mapping[str, Sequence[str]] | None = None,
    authority: str | None = None,
    authorization_reference: str | None = None,
    authorization_verified_at: str | None = None,
    authorization_expires_at: str | None = None,
    allow_repository_changes: bool = False,
    allow_external_side_effects: bool = False,
    idempotency_key: str | None = None,
    evidence: Sequence[Mapping[str, Any]] = (),
    sender_host_id: str | None = None,
    recipient_host_id: str | None = None,
    expires_in: int = DEFAULT_TTL_SECONDS,
    now: dt.datetime | None = None,
) -> bytes:
    if risk_class not in RISK_CLASSES:
        raise CamUsageError("argument.risk_class", "unsupported risk class")
    if authorization_basis == "first_contact":
        raise CamUsageError(
            "argument.authorization_basis",
            "first_contact is reserved for harmless enrollment messages",
        )
    if authorization_basis == "none" and risk_class != "informational":
        raise CamUsageError(
            "argument.authorization",
            "non-informational requests require an elevated authorization claim",
        )
    if allow_external_side_effects and risk_class != "external_or_irreversible":
        raise CamUsageError(
            "argument.risk_class",
            "external side effects require external_or_irreversible risk class",
        )
    if allow_repository_changes and risk_class in {"informational", "read_only"}:
        raise CamUsageError(
            "argument.risk_class",
            "repository changes require workspace_write or higher risk class",
        )
    sent, sent_at, expires_at = _time_window(now=now, expires_in=expires_in)
    message_id = str(uuid.uuid4())
    envelope = {
        "protocol": "CAM/1",
        "message_id": message_id,
        "type": "request",
        "sent_at": sent_at,
        "expires_at": expires_at,
        "claimed_sender": _sender(
            sender_vendor, sender_name, sender_session, sender_host_id
        ),
        "recipient": _recipient(
            recipient_vendor,
            recipient_name,
            recipient_session,
            recipient_host_id,
        ),
        "reply_to": {"transport": reply_transport, "address": reply_address},
        "in_reply_to": None,
        "receipt": None,
        "nonce": _nonce(),
        "intent": intent,
        "action": {
            "risk_class": risk_class,
            "operation": operation,
            "scope": _scope(scope),
            "idempotency_key": (
                message_id if idempotency_key is None else idempotency_key
            ),
        },
        "authorization": _authorization(
            basis=authorization_basis,
            authority=authority,
            reference=authorization_reference,
            verified_at=authorization_verified_at,
            expires_at=authorization_expires_at,
        ),
        "constraints": _constraints(
            allow_repository_changes=allow_repository_changes,
            allow_external_side_effects=allow_external_side_effects,
        ),
        "body": body,
        "body_sha256": _body_digest(body),
        "evidence": [dict(item) for item in evidence],
    }
    raw = serialize_envelope(envelope)
    validate_exact_bytes(raw, now=sent)
    return raw


def build_ack(
    request_raw: bytes,
    *,
    sender_vendor: str,
    sender_name: str,
    sender_session: str,
    reply_transport: str,
    reply_address: str,
    sender_host_id: str | None = None,
    status_value: str = "needs_human_confirmation",
    detail: str | None = None,
    intent: str = "Acknowledge CAM/1 first contact",
    body: str | None = None,
    expires_in: int = DEFAULT_TTL_SECONDS,
    now: dt.datetime | None = None,
) -> bytes:
    if status_value not in ACK_STATUSES:
        raise CamUsageError(
            "argument.status", "status is not valid for an acknowledgment"
        )
    sent, sent_at, expires_at = _time_window(now=now, expires_in=expires_in)
    request = _request_root(request_raw, now=sent)
    if (request.get("type"), "ack", status_value) not in STATELESS_REPLY_TRANSITIONS:
        raise CamUsageError(
            "argument.status",
            "status is not a legal acknowledgment for the request type",
        )
    if detail is None:
        detail = (
            "Operator verification is required before enrollment."
            if status_value == "needs_human_confirmation"
            else "Message received; no additional action is established by this acknowledgment."
        )
    if body is None:
        body = (
            "received; no action taken; operator verification required before enrollment"
            if status_value == "needs_human_confirmation"
            else f"{status_value}; no additional action established"
        )
    nonce = (
        None
        if status_value == "needs_human_confirmation"
        or (request.get("type") == "challenge" and status_value == "rejected")
        else request.get("nonce")
    )
    envelope = _base_response(
        request,
        message_type="ack",
        status_value=status_value,
        sender_vendor=sender_vendor,
        sender_name=sender_name,
        sender_session=sender_session,
        sender_host_id=sender_host_id,
        reply_transport=reply_transport,
        reply_address=reply_address,
        detail=detail,
        intent=intent,
        body=body,
        evidence=(),
        nonce=nonce,
        sent_at=sent_at,
        expires_at=expires_at,
    )
    raw = serialize_envelope(envelope)
    validate_exact_bytes(raw, against_raw=request_raw, now=sent)
    return raw


def _build_progress_reply(
    request_raw: bytes,
    *,
    message_type: str,
    status_value: str,
    sender_vendor: str,
    sender_name: str,
    sender_session: str,
    reply_transport: str,
    reply_address: str,
    detail: str | None,
    intent: str,
    body: str,
    evidence: Sequence[Mapping[str, Any]],
    previous_responses: Sequence[bytes],
    sender_host_id: str | None,
    expires_in: int,
    now: dt.datetime | None,
) -> bytes:
    sent, sent_at, expires_at = _time_window(now=now, expires_in=expires_in)
    request = _request_root(request_raw, now=sent, allow_expired=True)
    root_type = request.get("type")
    if root_type not in {"request", "cancel"}:
        raise CamUsageError(
            "argument.request",
            f"{message_type} requires a request or cancel root",
        )
    _ensure_no_prior_terminal_result(request_raw, previous_responses, now=sent)
    if (root_type, message_type, status_value) not in STATELESS_REPLY_TRANSITIONS:
        raise CamUsageError("argument.status", "illegal request lifecycle response")
    envelope = _base_response(
        request,
        message_type=message_type,
        status_value=status_value,
        sender_vendor=sender_vendor,
        sender_name=sender_name,
        sender_session=sender_session,
        sender_host_id=sender_host_id,
        reply_transport=reply_transport,
        reply_address=reply_address,
        detail=detail,
        intent=intent,
        body=body,
        evidence=evidence,
        nonce=None,
        sent_at=sent_at,
        expires_at=expires_at,
    )
    raw = serialize_envelope(envelope)
    validate_exact_bytes(raw, against_raw=request_raw, now=sent)
    return raw


def build_status(
    request_raw: bytes,
    *,
    sender_vendor: str,
    sender_name: str,
    sender_session: str,
    reply_transport: str,
    reply_address: str,
    status_value: str,
    body: str,
    detail: str | None = None,
    intent: str = "Report CAM/1 request progress",
    previous_responses: Sequence[bytes] = (),
    sender_host_id: str | None = None,
    expires_in: int = DEFAULT_TTL_SECONDS,
    now: dt.datetime | None = None,
) -> bytes:
    if status_value not in STATUS_VALUES:
        raise CamUsageError("argument.status", "status must be accepted or started")
    return _build_progress_reply(
        request_raw,
        message_type="status",
        status_value=status_value,
        sender_vendor=sender_vendor,
        sender_name=sender_name,
        sender_session=sender_session,
        reply_transport=reply_transport,
        reply_address=reply_address,
        detail=detail,
        intent=intent,
        body=body,
        evidence=(),
        previous_responses=previous_responses,
        sender_host_id=sender_host_id,
        expires_in=expires_in,
        now=now,
    )


def build_result(
    request_raw: bytes,
    *,
    sender_vendor: str,
    sender_name: str,
    sender_session: str,
    reply_transport: str,
    reply_address: str,
    body: str,
    detail: str | None = None,
    intent: str = "Report a completed CAM/1 request",
    evidence: Sequence[Mapping[str, Any]] = (),
    previous_responses: Sequence[bytes] = (),
    sender_host_id: str | None = None,
    expires_in: int = DEFAULT_TTL_SECONDS,
    now: dt.datetime | None = None,
) -> bytes:
    return _build_progress_reply(
        request_raw,
        message_type="result",
        status_value="completed",
        sender_vendor=sender_vendor,
        sender_name=sender_name,
        sender_session=sender_session,
        reply_transport=reply_transport,
        reply_address=reply_address,
        detail=detail,
        intent=intent,
        body=body,
        evidence=evidence,
        previous_responses=previous_responses,
        sender_host_id=sender_host_id,
        expires_in=expires_in,
        now=now,
    )


def build_error(
    request_raw: bytes,
    *,
    sender_vendor: str,
    sender_name: str,
    sender_session: str,
    reply_transport: str,
    reply_address: str,
    body: str,
    detail: str | None = None,
    intent: str = "Report a failed CAM/1 request",
    evidence: Sequence[Mapping[str, Any]] = (),
    previous_responses: Sequence[bytes] = (),
    sender_host_id: str | None = None,
    expires_in: int = DEFAULT_TTL_SECONDS,
    now: dt.datetime | None = None,
) -> bytes:
    return _build_progress_reply(
        request_raw,
        message_type="error",
        status_value="failed",
        sender_vendor=sender_vendor,
        sender_name=sender_name,
        sender_session=sender_session,
        reply_transport=reply_transport,
        reply_address=reply_address,
        detail=detail,
        intent=intent,
        body=body,
        evidence=evidence,
        previous_responses=previous_responses,
        sender_host_id=sender_host_id,
        expires_in=expires_in,
        now=now,
    )


def build_cancel(
    request_raw: bytes,
    *,
    sender_vendor: str,
    sender_name: str,
    sender_session: str,
    reply_transport: str,
    reply_address: str,
    authority: str,
    authorization_reference: str,
    authorization_verified_at: str,
    authorization_expires_at: str,
    body: str = "Stop this request if it has not passed an irreversible boundary.",
    sender_host_id: str | None = None,
    expires_in: int = DEFAULT_TTL_SECONDS,
    now: dt.datetime | None = None,
) -> bytes:
    sent, sent_at, expires_at = _time_window(now=now, expires_in=expires_in)
    request = _request_root(request_raw, now=sent, allow_expired=True)
    if request.get("type") != "request":
        raise CamUsageError("argument.request", "cancel requires a request root")
    original_sender = request["claimed_sender"]
    if (
        sender_vendor != original_sender["vendor"]
        or not _uuid_values_equal(sender_session, original_sender["session_id"])
        or (
            original_sender.get("host_id") is not None
            and sender_host_id != original_sender.get("host_id")
        )
    ):
        raise CamUsageError(
            "argument.sender",
            "cancel sender must equal the original request sender",
        )
    message_id = str(uuid.uuid4())
    envelope = {
        "protocol": "CAM/1",
        "message_id": message_id,
        "type": "cancel",
        "sent_at": sent_at,
        "expires_at": expires_at,
        "claimed_sender": _sender(
            sender_vendor, sender_name, sender_session, sender_host_id
        ),
        "recipient": dict(request["recipient"]),
        "reply_to": {"transport": reply_transport, "address": reply_address},
        "in_reply_to": request["message_id"],
        "receipt": None,
        "nonce": _nonce(),
        "intent": "Request cancellation of an existing CAM/1 request",
        "action": {
            "risk_class": request["action"]["risk_class"],
            "operation": "cancel",
            "scope": dict(request["action"]["scope"]),
            "idempotency_key": message_id,
        },
        "authorization": _authorization(
            basis="operator_confirmation",
            authority=authority,
            reference=authorization_reference,
            verified_at=authorization_verified_at,
            expires_at=authorization_expires_at,
        ),
        "constraints": dict(request["constraints"]),
        "body": body,
        "body_sha256": _body_digest(body),
        "evidence": [],
    }
    raw = serialize_envelope(envelope)
    validate_exact_bytes(raw, now=sent)
    return raw


def build_status_inquiry(
    request_raw: bytes,
    *,
    expires_in: int = DEFAULT_TTL_SECONDS,
    now: dt.datetime | None = None,
) -> bytes:
    sent = _normalize_now(now).replace(microsecond=0)
    request = _request_root(request_raw, now=sent, allow_expired=True)
    if request.get("type") != "request":
        raise CamUsageError(
            "argument.request", "status inquiry requires a request root"
        )
    reply_to = request.get("reply_to")
    if not isinstance(reply_to, dict):
        raise CamUsageError(
            "argument.reply_to", "status inquiry requires a usable reply route"
        )
    body = (
        f"Report the current state of request {request['message_id']}; "
        "do not repeat or extend the original action."
    )
    return build_request(
        sender_vendor=request["claimed_sender"]["vendor"],
        sender_name=request["claimed_sender"]["agent_name"],
        sender_session=request["claimed_sender"]["session_id"],
        sender_host_id=request["claimed_sender"].get("host_id"),
        recipient_vendor=request["recipient"]["vendor"],
        recipient_name=request["recipient"]["agent_name"],
        recipient_session=request["recipient"].get("session_id"),
        recipient_host_id=request["recipient"].get("host_id"),
        reply_transport=reply_to["transport"],
        reply_address=reply_to["address"],
        risk_class="informational",
        operation="inquire_status",
        intent="Request request-lifecycle status without repeating work",
        body=body,
        authorization_basis="none",
        expires_in=expires_in,
        now=sent,
    )


def renew_request(
    request_raw: bytes,
    *,
    authorization_basis: str,
    confirm_no_known_pending: bool,
    authority: str | None = None,
    authorization_reference: str | None = None,
    authorization_verified_at: str | None = None,
    authorization_expires_at: str | None = None,
    expires_in: int = DEFAULT_TTL_SECONDS,
    now: dt.datetime | None = None,
) -> bytes:
    sent = _normalize_now(now).replace(microsecond=0)
    request = _request_root(request_raw, now=sent, allow_expired=True)
    if request.get("type") != "request":
        raise CamUsageError("argument.request", "only a request root can be renewed")
    if _wire_time(request["expires_at"]) > sent:
        raise CamUsageError("argument.request_not_expired", "request is still fresh")
    if confirm_no_known_pending is not True:
        raise CamUsageError(
            "argument.pending_confirmation",
            "renewal requires explicit confirmation that no accepted or pending item is known",
        )
    reply_to = request.get("reply_to")
    if not isinstance(reply_to, dict):
        raise CamUsageError(
            "argument.reply_to", "request renewal requires a usable reply route"
        )
    return build_request(
        sender_vendor=request["claimed_sender"]["vendor"],
        sender_name=request["claimed_sender"]["agent_name"],
        sender_session=request["claimed_sender"]["session_id"],
        sender_host_id=request["claimed_sender"].get("host_id"),
        recipient_vendor=request["recipient"]["vendor"],
        recipient_name=request["recipient"]["agent_name"],
        recipient_session=request["recipient"].get("session_id"),
        recipient_host_id=request["recipient"].get("host_id"),
        reply_transport=reply_to["transport"],
        reply_address=reply_to["address"],
        risk_class=request["action"]["risk_class"],
        operation=request["action"]["operation"],
        scope=request["action"]["scope"],
        intent=request["intent"],
        body=request["body"],
        authorization_basis=authorization_basis,
        authority=authority,
        authorization_reference=authorization_reference,
        authorization_verified_at=authorization_verified_at,
        authorization_expires_at=authorization_expires_at,
        allow_repository_changes=not request["constraints"]["no_repository_changes"],
        allow_external_side_effects=not request["constraints"][
            "no_external_side_effects"
        ],
        idempotency_key=request["action"]["idempotency_key"],
        evidence=request["evidence"],
        expires_in=expires_in,
        now=sent,
    )


def build_late_rejection(
    request_raw: bytes,
    *,
    sender_vendor: str,
    sender_name: str,
    sender_session: str,
    reply_transport: str,
    reply_address: str,
    sender_host_id: str | None = None,
    expires_in: int = DEFAULT_TTL_SECONDS,
    now: dt.datetime | None = None,
) -> bytes:
    sent, sent_at, expires_at = _time_window(now=now, expires_in=expires_in)
    request = _request_root(request_raw, now=sent, allow_expired=True)
    if request.get("type") not in {"hello", "challenge", "request", "cancel"}:
        raise CamUsageError("argument.request", "root cannot receive a rejection")
    if _wire_time(request["expires_at"]) > sent:
        raise CamUsageError(
            "argument.request_not_expired", "use build-ack for a fresh root"
        )
    envelope = _base_response(
        request,
        message_type="ack",
        status_value="rejected",
        sender_vendor=sender_vendor,
        sender_name=sender_name,
        sender_session=sender_session,
        sender_host_id=sender_host_id,
        reply_transport=reply_transport,
        reply_address=reply_address,
        detail="The root envelope expired before handling; no action was taken.",
        intent="Reject an expired CAM/1 root without action",
        body="rejected; root expired before handling; no action taken",
        evidence=(),
        nonce=None,
        sent_at=sent_at,
        expires_at=expires_at,
    )
    raw = serialize_envelope(envelope)
    validate_exact_bytes(raw, against_raw=request_raw, now=sent)
    return raw
