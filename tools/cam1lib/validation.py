# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Semantic and pairwise correlation validation for parsed CAM/1 envelopes."""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import hashlib
import secrets
import uuid
from typing import Any

from .protocol import (
    DEFAULT_VALIDATION_POLICY,
    RECEIPT_TYPES,
    STATELESS_REPLY_TRANSITIONS,
    UTC_PATTERN,
    UUID_POINTERS,
    CamUsageError,
    CamValidationError,
    Problem,
    SemanticOutcome,
    ValidationPolicy,
    ValidationResult,
    _collection_limit_problems,
    _get,
    _pointer,
    _schema_problems,
    _unique_problems,
    parse_exact_bytes,
)


def _uuid_problem(value: Any, path: tuple[str, ...]) -> Problem | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError):
        if len(value) <= 128:
            shape = "group lengths " + "-".join(
                str(len(group)) for group in value.split("-")
            )
        else:
            shape = f"length {len(value)}; group count {value.count('-') + 1}"
        return Problem(
            "semantic.uuid",
            _pointer(path),
            f"invalid UUID; {shape}",
        )
    return None


def _uuid_values_equal(left: Any, right: Any) -> bool:
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    try:
        return uuid.UUID(left) == uuid.UUID(right)
    except (ValueError, AttributeError):
        return False


def _parse_timestamp(
    value: Any, path: tuple[str, ...]
) -> tuple[dt.datetime | None, Problem | None]:
    if not isinstance(value, str):
        return None, None
    if not UTC_PATTERN.fullmatch(value):
        return None, Problem(
            "semantic.timestamp",
            _pointer(path),
            "timestamp is outside the CAM/1 UTC profile",
        )
    try:
        parsed = dt.datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return None, Problem(
            "semantic.timestamp",
            _pointer(path),
            "timestamp is not a valid calendar time",
        )
    return parsed, None


def _nonce_problem(value: Any) -> Problem | None:
    if value is None or not isinstance(value, str):
        return None
    try:
        padding = "=" * ((4 - len(value) % 4) % 4)
        decoded = base64.b64decode(
            value + padding,
            altchars=b"-_",
            validate=True,
        )
    except (binascii.Error, ValueError):
        return Problem(
            "semantic.nonce",
            "/nonce",
            "nonce is not canonical unpadded base64url",
        )
    canonical = base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=")
    if canonical != value:
        return Problem(
            "semantic.nonce",
            "/nonce",
            "nonce is not canonical unpadded base64url",
        )
    if len(decoded) < 16:
        return Problem(
            "semantic.nonce",
            "/nonce",
            "nonce contains fewer than 128 bits",
        )
    return None


def _endpoint_matches(actual: Any, expected: Any) -> bool:
    if not isinstance(actual, dict) or not isinstance(expected, dict):
        return False
    for field in ("vendor", "agent_name"):
        if actual.get(field) != expected.get(field):
            return False
    expected_session = expected.get("session_id")
    if expected_session is not None and actual.get("session_id") != expected_session:
        return False
    expected_host = expected.get("host_id")
    return expected_host is None or actual.get("host_id") == expected_host


def _identifier_problems(envelope: dict[str, Any]) -> list[Problem]:
    problems: list[Problem] = []
    for path in UUID_POINTERS:
        problem = _uuid_problem(_get(envelope, path), path)
        if problem:
            problems.append(problem)
    return problems


def _message_time_problems(
    envelope: dict[str, Any],
    *,
    now: dt.datetime,
    policy: ValidationPolicy,
) -> tuple[list[Problem], bool, dt.datetime | None]:
    problems: list[Problem] = []
    fresh = True

    sent_at, sent_problem = _parse_timestamp(envelope.get("sent_at"), ("sent_at",))
    expires_at, expires_problem = _parse_timestamp(
        envelope.get("expires_at"), ("expires_at",)
    )
    if sent_problem:
        problems.append(sent_problem)
    if expires_problem:
        problems.append(expires_problem)
    if sent_at is not None and expires_at is not None:
        if expires_at <= sent_at:
            problems.append(
                Problem(
                    "semantic.expiry_order",
                    "/expires_at",
                    "expiry must be later than sent_at",
                )
            )
        if (expires_at - sent_at).total_seconds() > policy.max_ttl_seconds:
            problems.append(
                Problem(
                    "semantic.ttl",
                    "/expires_at",
                    f"message lifetime exceeds {policy.max_ttl_seconds} seconds",
                )
            )
        if (sent_at - now).total_seconds() > policy.clock_skew_seconds:
            problems.append(
                Problem(
                    "semantic.future",
                    "/sent_at",
                    "sent_at exceeds "
                    f"{policy.clock_skew_seconds} seconds of clock skew",
                )
            )
        if expires_at <= now:
            fresh = False
            if not policy.allow_expired:
                problems.append(
                    Problem("semantic.expired", "/expires_at", "message has expired")
                )
    return problems, fresh, sent_at


def _authorization_time_problems(
    envelope: dict[str, Any],
    *,
    sent_at: dt.datetime | None,
    policy: ValidationPolicy,
) -> list[Problem]:
    problems: list[Problem] = []

    authorization = envelope.get("authorization")
    if isinstance(authorization, dict):
        verified, verified_problem = _parse_timestamp(
            authorization.get("verified_at"),
            ("authorization", "verified_at"),
        )
        auth_expires, auth_expires_problem = _parse_timestamp(
            authorization.get("expires_at"),
            ("authorization", "expires_at"),
        )
        if verified_problem:
            problems.append(verified_problem)
        if auth_expires_problem:
            problems.append(auth_expires_problem)
        if verified is not None and auth_expires is not None:
            if auth_expires <= verified:
                problems.append(
                    Problem(
                        "semantic.authorization_expiry",
                        "/authorization/expires_at",
                        "authorization expiry must be later than verification",
                    )
                )
            if (
                sent_at is not None
                and (verified - sent_at).total_seconds() > policy.clock_skew_seconds
            ):
                problems.append(
                    Problem(
                        "semantic.authorization_future",
                        "/authorization/verified_at",
                        "authorization verification is later than the message",
                    )
                )
    return problems


def _nonce_semantic_problems(envelope: dict[str, Any]) -> list[Problem]:
    problems: list[Problem] = []
    nonce_problem = _nonce_problem(envelope.get("nonce"))
    if nonce_problem:
        problems.append(nonce_problem)

    message_type = envelope.get("type")
    if (
        isinstance(message_type, str)
        and message_type in {"challenge", "verify"}
        and envelope.get("nonce") is None
    ):
        problems.append(
            Problem(
                f"semantic.{message_type}_nonce",
                "/nonce",
                f"{message_type} messages require a nonce",
            )
        )
    return problems


def _body_hash_outcome(envelope: dict[str, Any]) -> tuple[list[Problem], bool]:
    problems: list[Problem] = []
    body_hash_valid = False

    body = envelope.get("body")
    claimed_digest = envelope.get("body_sha256")
    if (
        isinstance(body, str)
        and isinstance(claimed_digest, str)
        and len(claimed_digest) == 64
        and claimed_digest.isascii()
        and all(character in "0123456789abcdef" for character in claimed_digest)
    ):
        actual_digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        body_hash_valid = secrets.compare_digest(actual_digest, claimed_digest)
        if not body_hash_valid:
            problems.append(
                Problem(
                    "semantic.body_hash",
                    "/body_sha256",
                    "body digest does not match the decoded body",
                )
            )
    return problems, body_hash_valid


def _reply_semantic_problems(envelope: dict[str, Any]) -> list[Problem]:
    problems: list[Problem] = []
    message_type = envelope.get("type")

    receipt = envelope.get("receipt")
    in_reply_to = envelope.get("in_reply_to")
    if (
        isinstance(message_type, str)
        and message_type in RECEIPT_TYPES
        and isinstance(receipt, dict)
        and not _uuid_values_equal(receipt.get("for_message_id"), in_reply_to)
    ):
        problems.append(
            Problem(
                "semantic.receipt_correlation",
                "/receipt/for_message_id",
                "receipt message ID does not equal in_reply_to",
            )
        )
    if (
        isinstance(message_type, str)
        and message_type in {"status", "result", "error"}
        and envelope.get("nonce") is not None
    ):
        problems.append(
            Problem(
                "semantic.progress_nonce",
                "/nonce",
                "progress and result messages must not reuse the challenge nonce",
            )
        )
    return problems


def _callback_problems(envelope: dict[str, Any]) -> list[Problem]:
    problems: list[Problem] = []

    reply_to = envelope.get("reply_to")
    if isinstance(reply_to, dict) and reply_to.get("transport") == "codex_queue":
        callback_problem = _uuid_problem(
            reply_to.get("address"), ("reply_to", "address")
        )
        if callback_problem:
            problems.append(
                Problem(
                    "semantic.codex_callback",
                    "/reply_to/address",
                    "codex_queue callback must be a literal valid UUID",
                )
            )
    return problems


def _transition_problems(
    envelope: dict[str, Any],
    original: dict[str, Any],
) -> list[Problem]:
    message_type = envelope.get("type")
    original_type = original.get("type")
    receipt = envelope.get("receipt")
    status_value = receipt.get("status") if isinstance(receipt, dict) else None

    if not isinstance(original_type, str) or not isinstance(message_type, str):
        return []
    if status_value is not None and not isinstance(status_value, str):
        return []

    if (original_type, message_type, status_value) not in STATELESS_REPLY_TRANSITIONS:
        allowed_types = {
            candidate_type
            for source_type, candidate_type, _ in STATELESS_REPLY_TRANSITIONS
            if source_type == original_type
        }
        path = (
            "/receipt/status"
            if message_type in allowed_types and message_type in RECEIPT_TYPES
            else "/type"
        )
        return [
            Problem(
                "correlation.transition",
                path,
                "candidate is not a legal stateless reply to the supplied original",
            )
        ]
    return []


def _correlation_outcome(
    envelope: dict[str, Any],
    original: dict[str, Any] | None,
) -> tuple[list[Problem], bool | None]:
    if original is None:
        return [], None

    problems: list[Problem] = []
    correlated = True
    message_type = envelope.get("type")
    receipt = envelope.get("receipt")
    in_reply_to = envelope.get("in_reply_to")
    original_id = original.get("message_id")
    transition_problems = _transition_problems(envelope, original)
    if transition_problems:
        correlated = False
        problems.extend(transition_problems)

    if not _uuid_values_equal(in_reply_to, original_id):
        correlated = False
        problems.append(
            Problem(
                "correlation.in_reply_to",
                "/in_reply_to",
                "reply does not reference the supplied original",
            )
        )
    if _uuid_values_equal(envelope.get("message_id"), original_id):
        correlated = False
        problems.append(
            Problem(
                "correlation.message_id",
                "/message_id",
                "reply must use a message ID distinct from the supplied original",
            )
        )
    if isinstance(receipt, dict) and not _uuid_values_equal(
        receipt.get("for_message_id"), original_id
    ):
        correlated = False
        problems.append(
            Problem(
                "correlation.receipt",
                "/receipt/for_message_id",
                "receipt does not reference the supplied original",
            )
        )
    if not _endpoint_matches(envelope.get("recipient"), original.get("claimed_sender")):
        correlated = False
        problems.append(
            Problem(
                "correlation.recipient",
                "/recipient",
                "reply recipient does not match the original sender",
            )
        )
    if not _endpoint_matches(envelope.get("claimed_sender"), original.get("recipient")):
        correlated = False
        problems.append(
            Problem(
                "correlation.sender",
                "/claimed_sender",
                "reply sender does not match the original recipient",
            )
        )

    original_nonce = original.get("nonce")
    original_type = original.get("type")
    status_value = receipt.get("status") if isinstance(receipt, dict) else None
    if (
        original_type == "challenge"
        and message_type == "ack"
        and isinstance(status_value, str)
        and status_value in {"needs_human_confirmation", "rejected"}
    ):
        if envelope.get("nonce") is not None:
            correlated = False
            problems.append(
                Problem(
                    "correlation.challenge_ack_nonce",
                    "/nonce",
                    "an acknowledgment answering a challenge must use nonce null",
                )
            )
    elif message_type == "ack" and status_value == "needs_human_confirmation":
        if envelope.get("nonce") is not None:
            correlated = False
            problems.append(
                Problem(
                    "correlation.interim_nonce",
                    "/nonce",
                    "interim acknowledgment must use nonce null",
                )
            )
    elif (
        not transition_problems
        and isinstance(message_type, str)
        and message_type in {"ack", "verify"}
        and envelope.get("nonce") != original_nonce
    ):
        correlated = False
        problems.append(
            Problem(
                "correlation.nonce",
                "/nonce",
                "reply does not echo the original challenge nonce",
            )
        )
    return problems, correlated


def _semantic_problems(
    envelope: dict[str, Any],
    *,
    now: dt.datetime,
    policy: ValidationPolicy,
    original: dict[str, Any] | None,
) -> SemanticOutcome:
    time_problems, fresh, sent_at = _message_time_problems(
        envelope,
        now=now,
        policy=policy,
    )
    body_problems, body_hash_valid = _body_hash_outcome(envelope)
    correlation_problems, correlated = _correlation_outcome(envelope, original)
    problems = _unique_problems(
        (
            *_identifier_problems(envelope),
            *time_problems,
            *_authorization_time_problems(
                envelope,
                sent_at=sent_at,
                policy=policy,
            ),
            *_nonce_semantic_problems(envelope),
            *body_problems,
            *_reply_semantic_problems(envelope),
            *_callback_problems(envelope),
            *correlation_problems,
        )
    )

    return SemanticOutcome(
        problems=tuple(problems),
        fresh=fresh,
        body_hash_valid=body_hash_valid,
        correlated=correlated,
    )


def _normalize_now(value: dt.datetime | None) -> dt.datetime:
    observed = value or dt.datetime.now(dt.UTC)
    if observed.tzinfo is None or observed.utcoffset() is None:
        raise CamUsageError("argument.now", "now must be timezone-aware")
    return observed.astimezone(dt.UTC)


def validate_exact_bytes(
    raw: bytes,
    *,
    against_raw: bytes | None = None,
    now: dt.datetime | None = None,
    policy: ValidationPolicy = DEFAULT_VALIDATION_POLICY,
) -> ValidationResult:
    """Validate exact serialized CAM/1 bytes and optional reply correlation."""

    if not isinstance(policy, ValidationPolicy):
        raise CamUsageError(
            "argument.policy",
            "policy must be a ValidationPolicy instance",
        )
    observed_now = _normalize_now(now)

    envelope = parse_exact_bytes(raw)
    problems = _collection_limit_problems(envelope)
    if not problems:
        problems = _schema_problems(envelope)

    original: dict[str, Any] | None = None
    if against_raw is not None:
        original_result = validate_exact_bytes(
            against_raw,
            now=observed_now,
            policy=policy,
        )
        original = original_result.envelope

    semantic = _semantic_problems(
        envelope,
        now=observed_now,
        policy=policy,
        original=original,
    )
    problems.extend(semantic.problems)
    problems = _unique_problems(problems)
    if problems:
        raise CamValidationError(problems)
    return ValidationResult(
        envelope=envelope,
        fresh=semantic.fresh,
        body_hash_valid=semantic.body_hash_valid,
        correlated=semantic.correlated,
    )
