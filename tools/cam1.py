"""Offline CAM/1 envelope builder and validator.

This module deliberately performs no messaging, subprocess, socket, queue, or
network operation. It validates the exact bytes supplied by the caller and can
construct complete first-contact and acknowledgment envelopes.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import datetime as dt
import hashlib
import json
import os
import re
import secrets
import stat
import sys
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "cam-1.schema.json"
MAX_ENVELOPE_BYTES = 1_048_576
MAX_NESTING = 16
DEFAULT_TTL_SECONDS = 600
DEFAULT_MAX_TTL_SECONDS = 3_600
DEFAULT_CLOCK_SKEW_SECONDS = 300
MAX_PROBLEMS = 64

UTC_PATTERN = re.compile(
    r"^[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])"
    r"T([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9](\.[0-9]+)?Z$"
)

REPLY_TYPES = {"ack", "status", "result", "error"}
ACK_STATUSES = {
    "needs_human_confirmation",
    "received",
    "accepted",
    "rejected",
}
UUID_POINTERS = (
    ("message_id",),
    ("action", "idempotency_key"),
    ("in_reply_to",),
    ("receipt", "for_message_id"),
)


@dataclass(frozen=True)
class Problem:
    """A bounded validation diagnostic that does not expose field values."""

    code: str
    path: str
    detail: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", self.code[:80])
        object.__setattr__(self, "path", self.path[:256])
        object.__setattr__(self, "detail", self.detail[:240])

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "path": self.path, "detail": self.detail}


@dataclass(frozen=True)
class ValidationResult:
    envelope: dict[str, Any]
    fresh: bool
    body_hash_valid: bool
    correlated: bool | None

    def summary(self) -> dict[str, Any]:
        return {
            "protocol": "CAM/1",
            "structurally_valid": True,
            "fresh": self.fresh,
            "body_hash_valid": self.body_hash_valid,
            "correlated": self.correlated,
            "type": self.envelope["type"],
        }


class CamValidationError(Exception):
    def __init__(self, problems: Sequence[Problem]):
        self.problems = tuple(problems[:MAX_PROBLEMS])
        super().__init__("CAM/1 validation failed")


class DuplicateKeyError(ValueError):
    pass


class CliError(Exception):
    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(detail)


def _load_schema() -> dict[str, Any]:
    with SCHEMA_PATH.open("r", encoding="utf-8") as handle:
        schema = cast(dict[str, Any], json.load(handle))
    Draft202012Validator.check_schema(schema)
    return schema


SCHEMA = _load_schema()
VALIDATOR = Draft202012Validator(SCHEMA, format_checker=FormatChecker())


def _pointer(parts: Iterable[Any]) -> str:
    escaped = [str(part).replace("~", "~0").replace("/", "~1") for part in parts]
    return "/" + "/".join(escaped) if escaped else ""


def _unique_problems(problems: Iterable[Problem]) -> list[Problem]:
    found: dict[tuple[str, str, str], Problem] = {}
    for problem in problems:
        key = (problem.path, problem.code, problem.detail)
        found.setdefault(key, problem)
        if len(found) >= MAX_PROBLEMS:
            break
    return sorted(found.values(), key=lambda item: (item.path, item.code, item.detail))


def _reject_constant(value: str) -> None:
    del value
    raise ValueError("non-finite JSON number")


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateKeyError("duplicate object member")
        result[key] = value
    return result


def _scan_nesting(text: str) -> Problem | None:
    depth = 0
    in_string = False
    escaped = False
    for character in text:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            if depth > MAX_NESTING:
                return Problem(
                    "wire.nesting_limit",
                    "",
                    f"object/array nesting exceeds {MAX_NESTING}",
                )
        elif character in "]}":
            depth = max(0, depth - 1)
    return None


def _find_surrogate(value: Any) -> Problem | None:
    if isinstance(value, str):
        if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
            return Problem(
                "wire.unicode_scalar",
                "",
                "string contains an unpaired surrogate",
            )
    elif isinstance(value, dict):
        for key, item in value.items():
            problem = _find_surrogate(key)
            if problem:
                return problem
            problem = _find_surrogate(item)
            if problem:
                return problem
    elif isinstance(value, list):
        for item in value:
            problem = _find_surrogate(item)
            if problem:
                return problem
    return None


def parse_exact_bytes(raw: bytes) -> dict[str, Any]:
    """Parse exact envelope bytes after enforcing pre-parse wire limits."""

    if len(raw) > MAX_ENVELOPE_BYTES:
        raise CamValidationError(
            [
                Problem(
                    "wire.size_limit",
                    "",
                    f"envelope exceeds {MAX_ENVELOPE_BYTES} bytes",
                )
            ]
        )
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise CamValidationError(
            [Problem("wire.utf8", "", f"malformed UTF-8 near byte {error.start}")]
        ) from None
    nesting_problem = _scan_nesting(text)
    if nesting_problem:
        raise CamValidationError([nesting_problem])
    try:
        value = json.loads(
            text,
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_constant,
        )
    except DuplicateKeyError:
        raise CamValidationError(
            [Problem("wire.duplicate_key", "", "duplicate object member")]
        ) from None
    except (json.JSONDecodeError, ValueError) as error:
        offset = getattr(error, "pos", None)
        detail = "invalid JSON"
        if isinstance(offset, int):
            detail = f"invalid JSON near character {offset}"
        raise CamValidationError([Problem("wire.json", "", detail)]) from None
    if not isinstance(value, dict):
        raise CamValidationError(
            [Problem("wire.root_type", "", "envelope root must be an object")]
        )
    surrogate_problem = _find_surrogate(value)
    if surrogate_problem:
        raise CamValidationError([surrogate_problem])
    return value


def _required_field_problems(envelope: dict[str, Any]) -> list[Problem]:
    def lookup(path: tuple[str, ...]) -> Any:
        value: Any = envelope
        for part in path:
            if not isinstance(value, dict) or part not in value:
                return None
            value = value[part]
        return value

    definitions = SCHEMA["$defs"]
    required_objects = (
        ((), SCHEMA),
        (("claimed_sender",), definitions["senderEndpoint"]),
        (("recipient",), definitions["recipientEndpoint"]),
        (("reply_to",), definitions["replyRoute"]),
        (("receipt",), definitions["receipt"]),
        (("action",), definitions["action"]),
        (("action", "scope"), definitions["scope"]),
        (("authorization",), definitions["authorization"]),
        (("constraints",), definitions["constraints"]),
    )
    problems: list[Problem] = []
    for path, schema in required_objects:
        value = lookup(path)
        if not isinstance(value, dict):
            continue
        for name in schema.get("required", []):
            if name not in value:
                problems.append(
                    Problem(
                        "schema.required",
                        _pointer(path + (name,)),
                        "required field is missing",
                    )
                )
                if len(problems) >= MAX_PROBLEMS:
                    return problems
    evidence = envelope.get("evidence")
    if isinstance(evidence, list):
        evidence_schema = definitions["evidenceItem"]
        for index, item in enumerate(evidence):
            if not isinstance(item, dict):
                continue
            for name in evidence_schema.get("required", []):
                if name not in item:
                    problems.append(
                        Problem(
                            "schema.required",
                            _pointer(("evidence", index, name)),
                            "required field is missing",
                        )
                    )
                    if len(problems) >= MAX_PROBLEMS:
                        return problems
    return problems


def _schema_problems(envelope: dict[str, Any]) -> list[Problem]:
    problems = _required_field_problems(envelope)
    generic_details = {
        "additionalProperties": "unexpected field",
        "const": "value does not match the required constant",
        "enum": "value is outside the allowed set",
        "format": "value has an invalid format",
        "maxItems": "array contains too many items",
        "maxLength": "string is too long",
        "minLength": "string is too short",
        "oneOf": "value does not match exactly one permitted shape",
        "pattern": "string does not match the required syntax",
        "type": "value has the wrong JSON type",
        "uniqueItems": "array items must be unique",
    }
    for error in VALIDATOR.iter_errors(envelope):
        if len(problems) >= MAX_PROBLEMS:
            break
        path = tuple(error.absolute_path)
        if error.validator == "required":
            continue
        code = f"schema.{error.validator}"
        detail = generic_details.get(error.validator, "schema constraint failed")
        problems.append(Problem(code, _pointer(path), detail))
    return _unique_problems(problems)


def _get(envelope: dict[str, Any], path: tuple[str, ...]) -> Any:
    value: Any = envelope
    for part in path:
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _collection_limit_problems(envelope: dict[str, Any]) -> list[Problem]:
    limits = (
        (("evidence",), 64),
        (("action", "scope", "repositories"), 64),
        (("action", "scope", "paths"), 64),
        (("action", "scope", "hosts"), 64),
        (("action", "scope", "external_recipients"), 64),
    )
    problems = []
    for path, limit in limits:
        value = _get(envelope, path)
        if isinstance(value, list) and len(value) > limit:
            problems.append(
                Problem(
                    "wire.collection_limit",
                    _pointer(path),
                    f"array contains more than {limit} items",
                )
            )
    return problems


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


def _semantic_problems(
    envelope: dict[str, Any],
    *,
    now: dt.datetime,
    allow_expired: bool,
    max_ttl_seconds: int,
    clock_skew_seconds: int,
    original: dict[str, Any] | None,
) -> tuple[list[Problem], bool, bool, bool | None]:
    problems: list[Problem] = []
    fresh = True
    body_hash_valid = False
    correlated: bool | None = None

    for path in UUID_POINTERS:
        problem = _uuid_problem(_get(envelope, path), path)
        if problem:
            problems.append(problem)

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
        if (expires_at - sent_at).total_seconds() > max_ttl_seconds:
            problems.append(
                Problem(
                    "semantic.ttl",
                    "/expires_at",
                    f"message lifetime exceeds {max_ttl_seconds} seconds",
                )
            )
        if sent_at > now + dt.timedelta(seconds=clock_skew_seconds):
            problems.append(
                Problem(
                    "semantic.future",
                    "/sent_at",
                    f"sent_at exceeds {clock_skew_seconds} seconds of clock skew",
                )
            )
        if expires_at <= now:
            fresh = False
            if not allow_expired:
                problems.append(
                    Problem("semantic.expired", "/expires_at", "message has expired")
                )

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
            if sent_at is not None and verified > sent_at + dt.timedelta(
                seconds=clock_skew_seconds
            ):
                problems.append(
                    Problem(
                        "semantic.authorization_future",
                        "/authorization/verified_at",
                        "authorization verification is later than the message",
                    )
                )
    nonce_problem = _nonce_problem(envelope.get("nonce"))
    if nonce_problem:
        problems.append(nonce_problem)

    message_type = envelope.get("type")
    if message_type in {"challenge", "verify"} and envelope.get("nonce") is None:
        problems.append(
            Problem(
                f"semantic.{message_type}_nonce",
                "/nonce",
                f"{message_type} messages require a nonce",
            )
        )

    body = envelope.get("body")
    claimed_digest = envelope.get("body_sha256")
    if isinstance(body, str) and isinstance(claimed_digest, str):
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

    receipt = envelope.get("receipt")
    in_reply_to = envelope.get("in_reply_to")
    if (
        message_type in REPLY_TYPES
        and isinstance(receipt, dict)
        and receipt.get("for_message_id") != in_reply_to
    ):
        problems.append(
            Problem(
                "semantic.receipt_correlation",
                "/receipt/for_message_id",
                "receipt message ID does not equal in_reply_to",
            )
        )
    if (
        message_type in {"status", "result", "error"}
        and envelope.get("nonce") is not None
    ):
        problems.append(
            Problem(
                "semantic.progress_nonce",
                "/nonce",
                "progress and result messages must not reuse the challenge nonce",
            )
        )

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

    if original is not None:
        correlated = True
        original_id = original.get("message_id")
        if in_reply_to != original_id:
            correlated = False
            problems.append(
                Problem(
                    "correlation.in_reply_to",
                    "/in_reply_to",
                    "reply does not reference the supplied original",
                )
            )
        if isinstance(receipt, dict) and receipt.get("for_message_id") != original_id:
            correlated = False
            problems.append(
                Problem(
                    "correlation.receipt",
                    "/receipt/for_message_id",
                    "receipt does not reference the supplied original",
                )
            )
        if not _endpoint_matches(
            envelope.get("recipient"), original.get("claimed_sender")
        ):
            correlated = False
            problems.append(
                Problem(
                    "correlation.recipient",
                    "/recipient",
                    "reply recipient does not match the original sender",
                )
            )
        if not _endpoint_matches(
            envelope.get("claimed_sender"), original.get("recipient")
        ):
            correlated = False
            problems.append(
                Problem(
                    "correlation.sender",
                    "/claimed_sender",
                    "reply sender does not match the original recipient",
                )
            )
        original_nonce = original.get("nonce")
        status_value = receipt.get("status") if isinstance(receipt, dict) else None
        if message_type == "ack" and status_value == "needs_human_confirmation":
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
            message_type in {"ack", "verify"}
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

    return _unique_problems(problems), fresh, body_hash_valid, correlated


def validate_exact_bytes(
    raw: bytes,
    *,
    against_raw: bytes | None = None,
    now: dt.datetime | None = None,
    allow_expired: bool = False,
    max_ttl_seconds: int = DEFAULT_MAX_TTL_SECONDS,
    clock_skew_seconds: int = DEFAULT_CLOCK_SKEW_SECONDS,
) -> ValidationResult:
    """Validate exact serialized CAM/1 bytes and optional reply correlation."""

    if max_ttl_seconds <= 0:
        raise CliError("argument.max_ttl", "maximum TTL must be positive")
    if clock_skew_seconds < 0:
        raise CliError("argument.clock_skew", "clock skew must not be negative")
    observed_now = now or dt.datetime.now(dt.timezone.utc)
    if observed_now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    observed_now = observed_now.astimezone(dt.timezone.utc)

    envelope = parse_exact_bytes(raw)
    problems = _collection_limit_problems(envelope)
    if not problems:
        problems = _schema_problems(envelope)

    original: dict[str, Any] | None = None
    if against_raw is not None:
        original_result = validate_exact_bytes(
            against_raw,
            now=observed_now,
            allow_expired=allow_expired,
            max_ttl_seconds=max_ttl_seconds,
            clock_skew_seconds=clock_skew_seconds,
        )
        original = original_result.envelope

    semantic, fresh, body_hash_valid, correlated = _semantic_problems(
        envelope,
        now=observed_now,
        allow_expired=allow_expired,
        max_ttl_seconds=max_ttl_seconds,
        clock_skew_seconds=clock_skew_seconds,
        original=original,
    )
    problems.extend(semantic)
    problems = _unique_problems(problems)
    if problems:
        raise CamValidationError(problems)
    return ValidationResult(envelope, fresh, body_hash_valid, correlated)


def serialize_envelope(envelope: dict[str, Any]) -> bytes:
    return json.dumps(
        envelope,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _utc_text(value: dt.datetime) -> str:
    value = value.astimezone(dt.timezone.utc)
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
        raise CliError(
            "argument.expires_in",
            f"expires-in must be between 1 and {DEFAULT_MAX_TTL_SECONDS}",
        )
    sent = (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
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
        "audit_ref": None,
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
        raise CliError("argument.status", "status is not valid for an acknowledgment")
    if expires_in <= 0 or expires_in > DEFAULT_MAX_TTL_SECONDS:
        raise CliError(
            "argument.expires_in",
            f"expires-in must be between 1 and {DEFAULT_MAX_TTL_SECONDS}",
        )
    sent = (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
    sent = sent.replace(microsecond=0)
    request_result = validate_exact_bytes(request_raw, now=sent)
    request = request_result.envelope

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
            None if status_value == "needs_human_confirmation" else request.get("nonce")
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
        "audit_ref": None,
    }
    raw = serialize_envelope(envelope)
    validate_exact_bytes(raw, against_raw=request_raw, now=sent)
    return raw


def _read_bounded(path_text: str) -> bytes:
    if path_text == "-":
        raw = sys.stdin.buffer.read(MAX_ENVELOPE_BYTES + 1)
        if len(raw) > MAX_ENVELOPE_BYTES:
            raise CamValidationError(
                [
                    Problem(
                        "wire.size_limit",
                        "",
                        f"envelope exceeds {MAX_ENVELOPE_BYTES} bytes",
                    )
                ]
            )
        return raw

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path_text, flags)
    except OSError:
        raise CliError("input.open", "input must be a readable regular file") from None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise CliError("input.type", "input must be a regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            raw = handle.read(MAX_ENVELOPE_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(raw) > MAX_ENVELOPE_BYTES:
        raise CamValidationError(
            [
                Problem(
                    "wire.size_limit",
                    "",
                    f"envelope exceeds {MAX_ENVELOPE_BYTES} bytes",
                )
            ]
        )
    return raw


def _write_output(raw: bytes, path_text: str | None) -> None:
    if path_text is None or path_text == "-":
        sys.stdout.buffer.write(raw)
        sys.stdout.buffer.flush()
        return
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path_text, flags, 0o600)
    except OSError:
        raise CliError(
            "output.create",
            "output path must be new, non-symlinked, and writable",
        ) from None
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _add_endpoint_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--sender-vendor", required=True)
    parser.add_argument("--sender-name", required=True)
    parser.add_argument("--sender-session", required=True)
    parser.add_argument("--sender-host-id")
    parser.add_argument("--reply-transport", required=True)
    parser.add_argument("--reply-address", required=True)
    parser.add_argument("--expires-in", type=int, default=DEFAULT_TTL_SECONDS)
    parser.add_argument("--output")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build and validate CAM/1 envelopes without sending them."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser(
        "validate", help="validate exact serialized envelope bytes"
    )
    validate_parser.add_argument("message", nargs="?", default="-")
    validate_parser.add_argument("--against")
    validate_parser.add_argument("--allow-expired", action="store_true")
    validate_parser.add_argument(
        "--max-ttl-seconds", type=int, default=DEFAULT_MAX_TTL_SECONDS
    )
    validate_parser.add_argument(
        "--clock-skew-seconds", type=int, default=DEFAULT_CLOCK_SKEW_SECONDS
    )

    hello_parser = subparsers.add_parser(
        "build-hello", help="build a complete harmless first-contact envelope"
    )
    _add_endpoint_arguments(hello_parser)
    hello_parser.add_argument("--recipient-vendor", required=True)
    hello_parser.add_argument("--recipient-name", required=True)
    hello_parser.add_argument("--recipient-session")
    hello_parser.add_argument("--recipient-host-id")
    hello_parser.add_argument(
        "--intent", default="Verify a harmless bidirectional messaging path"
    )
    hello_parser.add_argument("--body")

    ack_parser = subparsers.add_parser(
        "build-ack", help="build a complete acknowledgment from an exact request"
    )
    _add_endpoint_arguments(ack_parser)
    ack_parser.add_argument("--request", required=True)
    ack_parser.add_argument(
        "--status",
        choices=sorted(ACK_STATUSES),
        default="needs_human_confirmation",
    )
    ack_parser.add_argument("--detail")
    ack_parser.add_argument("--intent", default="Acknowledge CAM/1 first contact")
    ack_parser.add_argument("--body")
    return parser


def _emit_error(error: Exception) -> None:
    if isinstance(error, CamValidationError):
        payload = {
            "valid": False,
            "problems": [problem.as_dict() for problem in error.problems],
        }
    elif isinstance(error, CliError):
        payload = {
            "valid": False,
            "problems": [Problem(error.code, "", error.detail[:240]).as_dict()],
        }
    else:
        payload = {
            "valid": False,
            "problems": [
                Problem("internal.error", "", "unexpected validator failure").as_dict()
            ],
        }
    sys.stderr.write(json.dumps(payload, separators=(",", ":")) + "\n")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "validate":
            raw = _read_bounded(args.message)
            against_raw = _read_bounded(args.against) if args.against else None
            result = validate_exact_bytes(
                raw,
                against_raw=against_raw,
                allow_expired=args.allow_expired,
                max_ttl_seconds=args.max_ttl_seconds,
                clock_skew_seconds=args.clock_skew_seconds,
            )
            sys.stdout.write(json.dumps(result.summary(), separators=(",", ":")) + "\n")
            return 0

        if args.command == "build-hello":
            keyword_args: dict[str, Any] = {
                "sender_vendor": args.sender_vendor,
                "sender_name": args.sender_name,
                "sender_session": args.sender_session,
                "sender_host_id": args.sender_host_id,
                "recipient_vendor": args.recipient_vendor,
                "recipient_name": args.recipient_name,
                "recipient_session": args.recipient_session,
                "recipient_host_id": args.recipient_host_id,
                "reply_transport": args.reply_transport,
                "reply_address": args.reply_address,
                "intent": args.intent,
                "expires_in": args.expires_in,
            }
            if args.body is not None:
                keyword_args["body"] = args.body
            raw = build_hello(**keyword_args)
            _write_output(raw, args.output)
            return 0

        if args.command == "build-ack":
            request_raw = _read_bounded(args.request)
            raw = build_ack(
                request_raw,
                sender_vendor=args.sender_vendor,
                sender_name=args.sender_name,
                sender_session=args.sender_session,
                sender_host_id=args.sender_host_id,
                reply_transport=args.reply_transport,
                reply_address=args.reply_address,
                status_value=args.status,
                detail=args.detail,
                intent=args.intent,
                body=args.body,
                expires_in=args.expires_in,
            )
            _write_output(raw, args.output)
            return 0
    except (CamValidationError, CliError) as error:
        _emit_error(error)
        return 2
    except Exception as error:  # noqa: BLE001 - keep envelope failures redacted
        _emit_error(error)
        return 3
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
