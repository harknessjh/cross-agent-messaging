# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""CAM/1 wire limits, data types, schema validation, and exact-byte codec."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[2]
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

RECEIPT_TYPES = {"ack", "status", "result", "error"}
REPLY_TYPES = RECEIPT_TYPES | {"verify"}
ACK_STATUSES = {
    "needs_human_confirmation",
    "received",
    "accepted",
    "rejected",
}
STATELESS_REPLY_TRANSITIONS = frozenset(
    {
        ("hello", "ack", "received"),
        ("hello", "ack", "needs_human_confirmation"),
        ("hello", "ack", "rejected"),
        ("challenge", "ack", "needs_human_confirmation"),
        ("challenge", "ack", "rejected"),
        ("challenge", "verify", None),
        ("request", "ack", "received"),
        ("request", "ack", "needs_human_confirmation"),
        ("request", "ack", "accepted"),
        ("request", "ack", "rejected"),
        ("request", "status", "accepted"),
        ("request", "status", "started"),
        ("request", "result", "completed"),
        ("request", "error", "failed"),
        ("cancel", "ack", "received"),
        ("cancel", "ack", "accepted"),
        ("cancel", "ack", "rejected"),
        ("cancel", "error", "failed"),
    }
)
UUID_POINTERS = (
    ("message_id",),
    ("action", "idempotency_key"),
    ("in_reply_to",),
    ("receipt", "for_message_id"),
)


class CamUsageError(ValueError):
    """Invalid use of the validation or builder API, independent of the CLI."""

    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
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


@dataclass(frozen=True, slots=True)
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


@dataclass(frozen=True, slots=True)
class ValidationPolicy:
    """Caller-selected validation limits that are independent of wire framing."""

    allow_expired: bool = False
    max_ttl_seconds: int = DEFAULT_MAX_TTL_SECONDS
    clock_skew_seconds: int = DEFAULT_CLOCK_SKEW_SECONDS

    def __post_init__(self) -> None:
        if type(self.allow_expired) is not bool:
            raise CamUsageError(
                "argument.allow_expired",
                "allow_expired must be a boolean",
            )
        if type(self.max_ttl_seconds) is not int or self.max_ttl_seconds <= 0:
            raise CamUsageError(
                "argument.max_ttl",
                "maximum TTL must be a positive integer number of seconds",
            )
        if type(self.clock_skew_seconds) is not int or self.clock_skew_seconds < 0:
            raise CamUsageError(
                "argument.clock_skew",
                "clock skew must be a non-negative integer number of seconds",
            )


DEFAULT_VALIDATION_POLICY = ValidationPolicy()


@dataclass(frozen=True, slots=True)
class SemanticOutcome:
    """Semantic validation state before it is promoted to a public result."""

    problems: tuple[Problem, ...]
    fresh: bool
    body_hash_valid: bool
    correlated: bool | None


class CamValidationError(Exception):
    def __init__(self, problems: Sequence[Problem]):
        self.problems = tuple(problems[:MAX_PROBLEMS])
        super().__init__("CAM/1 validation failed")


class DuplicateKeyError(ValueError):
    pass


class CliError(Exception):
    """Failure while reading or writing a CLI-owned local resource."""

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


def _finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("non-finite JSON number")
    return parsed


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
            parse_float=_finite_float,
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


def serialize_envelope(envelope: dict[str, Any]) -> bytes:
    return json.dumps(
        envelope,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
