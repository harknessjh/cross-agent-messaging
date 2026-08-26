# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Deterministic CAM/1 envelope serialization and safe first-contact builders."""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import secrets
import uuid
from typing import Any

from .protocol import (
    ACK_STATUSES,
    DEFAULT_MAX_TTL_SECONDS,
    DEFAULT_TTL_SECONDS,
    STATELESS_REPLY_TRANSITIONS,
    CamUsageError,
    serialize_envelope,
)
from .validation import _normalize_now, validate_exact_bytes


def _utc_text(value: dt.datetime) -> str:
    value = value.astimezone(dt.UTC)
    if value.microsecond:
        return value.isoformat(timespec="microseconds").replace("+00:00", "Z")
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


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


def _safe_constraints() -> dict[str, bool]:
    return {
        "no_repository_changes": True,
        "no_external_side_effects": True,
        "no_secrets": True,
    }


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
    intent: str = "Verify a harmless bidirectional messaging path",
    body: str = (
        "Harmless first contact only. Preserve and validate this exact envelope. "
        "Return a complete correlated CAM/1 acknowledgment through reply_to; use "
        "needs_human_confirmation with nonce null if this peer is not "
        "operator-confirmed. Do not make changes."
    ),
    expires_in: int = DEFAULT_TTL_SECONDS,
    now: dt.datetime | None = None,
) -> bytes:
    if expires_in <= 0 or expires_in > DEFAULT_MAX_TTL_SECONDS:
        raise CamUsageError(
            "argument.expires_in",
            f"expires-in must be between 1 and {DEFAULT_MAX_TTL_SECONDS}",
        )
    sent = _normalize_now(now)
    sent = sent.replace(microsecond=0)
    message_id = str(uuid.uuid4())
    recipient: dict[str, Any] = {
        "vendor": recipient_vendor,
        "agent_name": recipient_name,
        "session_id": recipient_session,
    }
    if recipient_host_id is not None:
        recipient["host_id"] = recipient_host_id
    envelope: dict[str, Any] = {
        "protocol": "CAM/1",
        "message_id": message_id,
        "type": "hello",
        "sent_at": _utc_text(sent),
        "expires_at": _utc_text(sent + dt.timedelta(seconds=expires_in)),
        "claimed_sender": {
            "vendor": sender_vendor,
            "agent_name": sender_name,
            "session_id": sender_session,
            "host_id": sender_host_id,
        },
        "recipient": recipient,
        "reply_to": {
            "transport": reply_transport,
            "address": reply_address,
        },
        "in_reply_to": None,
        "receipt": None,
        "nonce": _nonce(),
        "intent": intent,
        "action": {
            "risk_class": "informational",
            "operation": "acknowledge",
            "scope": _empty_scope(),
            "idempotency_key": message_id,
        },
        "authorization": {
            "basis": "first_contact",
            "authority": None,
            "reference": None,
            "verified_at": None,
            "expires_at": None,
        },
        "constraints": _safe_constraints(),
        "body": body,
        "body_sha256": _body_digest(body),
        "evidence": [],
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
            "argument.status",
            "status is not valid for an acknowledgment",
        )
    if expires_in <= 0 or expires_in > DEFAULT_MAX_TTL_SECONDS:
        raise CamUsageError(
            "argument.expires_in",
            f"expires-in must be between 1 and {DEFAULT_MAX_TTL_SECONDS}",
        )
    sent = _normalize_now(now)
    sent = sent.replace(microsecond=0)
    request_result = validate_exact_bytes(request_raw, now=sent)
    request = request_result.envelope
    if (
        request.get("type"),
        "ack",
        status_value,
    ) not in STATELESS_REPLY_TRANSITIONS:
        raise CamUsageError(
            "argument.status",
            "status is not a legal acknowledgment for the request type",
        )

    if detail is None:
        if status_value == "needs_human_confirmation":
            detail = "Operator verification is required before enrollment."
        else:
            detail = "Harmless first-contact message received; no action taken."
    if body is None:
        if status_value == "needs_human_confirmation":
            body = "received; no action taken; operator verification required before enrollment"
        else:
            body = f"{status_value}; no action taken"

    message_id = str(uuid.uuid4())
    original_sender = request["claimed_sender"]
    recipient: dict[str, Any] = {
        "vendor": original_sender["vendor"],
        "agent_name": original_sender["agent_name"],
        "session_id": original_sender["session_id"],
    }
    if original_sender.get("host_id") is not None:
        recipient["host_id"] = original_sender["host_id"]
    envelope: dict[str, Any] = {
        "protocol": "CAM/1",
        "message_id": message_id,
        "type": "ack",
        "sent_at": _utc_text(sent),
        "expires_at": _utc_text(sent + dt.timedelta(seconds=expires_in)),
        "claimed_sender": {
            "vendor": sender_vendor,
            "agent_name": sender_name,
            "session_id": sender_session,
            "host_id": sender_host_id,
        },
        "recipient": recipient,
        "reply_to": {
            "transport": reply_transport,
            "address": reply_address,
        },
        "in_reply_to": request["message_id"],
        "receipt": {
            "status": status_value,
            "for_message_id": request["message_id"],
            "detail": detail,
        },
        "nonce": (
            None
            if status_value == "needs_human_confirmation"
            or (request.get("type") == "challenge" and status_value == "rejected")
            else request.get("nonce")
        ),
        "intent": intent,
        "action": {
            "risk_class": "informational",
            "operation": "acknowledge",
            "scope": _empty_scope(),
            "idempotency_key": message_id,
        },
        "authorization": {
            "basis": "first_contact",
            "authority": None,
            "reference": None,
            "verified_at": None,
            "expires_at": None,
        },
        "constraints": _safe_constraints(),
        "body": body,
        "body_sha256": _body_digest(body),
        "evidence": [],
    }
    raw = serialize_envelope(envelope)
    validate_exact_bytes(raw, against_raw=request_raw, now=sent)
    return raw
