# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Journal-backed retry safety policy for local CAM/1 transports."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

if __package__:
    from . import cam1
    from . import cam1_transport_native as _native
    from .cam1lib import journal, lifecycle, project
else:  # Direct execution adds tools/ rather than the repo to sys.path.
    import cam1  # type: ignore[no-redef]
    import cam1_transport_native as _native  # type: ignore[no-redef]
    from cam1lib import journal, lifecycle, project  # type: ignore[no-redef]

TransportError = _native.TransportError
ValidatedEnvelope = _native.ValidatedEnvelope
_canonical_uuid = _native._canonical_uuid


@dataclass(frozen=True, slots=True)
class _RetryFingerprint:
    """Fields that determine whether two outbound intents are safe retries."""

    raw: bytes
    message_id: str
    idempotency_key: str
    semantic_digest: str


def _retry_fingerprint(raw: bytes, envelope: dict[str, Any]) -> _RetryFingerprint:
    action = envelope.get("action")
    idempotency_key = (
        action.get("idempotency_key") if isinstance(action, dict) else None
    )
    return _RetryFingerprint(
        raw=raw,
        message_id=_canonical_uuid(envelope.get("message_id"), label="message_id"),
        idempotency_key=_canonical_uuid(idempotency_key, label="idempotency_key"),
        semantic_digest=lifecycle.semantic_operation_digest(envelope),
    )


def _prior_retry_fingerprint(record: dict[str, Any]) -> _RetryFingerprint:
    prior_raw = journal.decode_exact_message(record)
    if prior_raw is None:
        raise TransportError(
            "transport.intent_invalid",
            "a prior outbound intent has no preserved envelope; do not retry",
        )
    try:
        return _retry_fingerprint(prior_raw, cam1.parse_exact_bytes(prior_raw))
    except (cam1.CamUsageError, cam1.CamValidationError) as error:
        raise TransportError(
            "transport.intent_invalid",
            "a prior outbound intent cannot be safely interpreted; do not retry",
        ) from error


def _is_matching_retry_intent(
    prior: _RetryFingerprint,
    requested: _RetryFingerprint,
    known_renewal_roots: frozenset[str],
) -> bool:
    if prior.message_id == requested.message_id and prior.raw != requested.raw:
        raise TransportError(
            "transport.message_id_conflict",
            "message_id already appears in an outbound intent with different bytes",
        )
    if prior.idempotency_key != requested.idempotency_key:
        return False
    if prior.semantic_digest != requested.semantic_digest:
        raise TransportError(
            "transport.idempotency_conflict",
            "idempotency key already appears with different semantic content",
        )
    # A prospectively validated renewal may supersede known, journaled roots in
    # its unbranched semantic chain. An intent without a root remains blocking.
    if (
        prior.message_id != requested.message_id
        and prior.message_id in known_renewal_roots
    ):
        return False
    if prior.raw != requested.raw:
        raise TransportError(
            "transport.retry_requires_identical_envelope",
            "a transport retry must use the identical preserved envelope",
        )
    return True


def _matching_retry_intents(
    records: tuple[dict[str, Any], ...],
    requested: _RetryFingerprint,
    known_renewal_roots: frozenset[str],
) -> list[dict[str, Any]]:
    matching: list[dict[str, Any]] = []
    for record in records:
        if record["event_type"] != "message.outbound.intent":
            continue
        prior = _prior_retry_fingerprint(record)
        if _is_matching_retry_intent(prior, requested, known_renewal_roots):
            matching.append(record)
    return matching


def _transport_outcomes(
    records: tuple[dict[str, Any], ...],
) -> dict[str, list[dict[str, Any]]]:
    """Index conclusive and ambiguous transport outcomes by intent record."""

    outcomes: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        if record["event_type"] not in {
            "transport.accepted",
            "transport.not_accepted",
        }:
            continue
        attributes = record.get("attributes")
        if not isinstance(attributes, dict):
            continue
        intent_id = attributes.get("intent_record_id")
        if isinstance(intent_id, str):
            outcomes.setdefault(intent_id, []).append(record)
    return outcomes


def _conclusive_retry_intent_id(
    intent: dict[str, Any],
    outcomes: dict[str, list[dict[str, Any]]],
) -> str:
    linked = [
        outcome
        for outcome in outcomes.get(intent["record_id"], [])
        if outcome["sequence"] > intent["sequence"]
    ]
    if len(linked) != 1:
        raise TransportError(
            "transport.retry_unsafe",
            "a prior matching send has no single conclusive outcome; do not retry",
        )
    outcome = linked[0]
    if outcome["event_type"] == "transport.accepted":
        raise TransportError(
            "transport.already_accepted",
            "the same exact message already has transport acceptance",
        )
    if outcome["attributes"].get("delivery_state") != "not_attempted":
        raise TransportError(
            "transport.retry_unsafe",
            "a prior matching send has unknown delivery state; do not retry",
        )
    return intent["record_id"]


def _confirmed_retry_intent(
    matching: list[dict[str, Any]],
    outcomes: dict[str, list[dict[str, Any]]],
    retry_after_intent: str | None,
) -> str | None:
    if not matching:
        if retry_after_intent is not None:
            raise TransportError(
                "argument.retry_intent",
                "retry-after-intent does not identify a prior matching attempt",
            )
        return None
    safe_retry_ids = [
        _conclusive_retry_intent_id(intent, outcomes) for intent in matching
    ]
    latest_id = safe_retry_ids[-1]
    if retry_after_intent is None:
        raise TransportError(
            "transport.retry_confirmation_required",
            "the prior send did not start; retry requires its exact intent record ID",
        )
    if _canonical_uuid(retry_after_intent, label="retry_after_intent") != latest_id:
        raise TransportError(
            "argument.retry_intent",
            "retry-after-intent must equal the latest conclusively rejected attempt",
        )
    return latest_id


def require_safe_retry(
    binding: project.ProjectBinding,
    validated: ValidatedEnvelope,
    *,
    retry_after_intent: str | None,
    known_renewal_roots: frozenset[str],
) -> str | None:
    """Refuse blind semantic repeats after accepted, unknown, or orphaned sends."""

    records = journal.replay_records(binding)
    requested = _retry_fingerprint(validated.raw, validated.envelope)
    matching = _matching_retry_intents(records, requested, known_renewal_roots)
    return _confirmed_retry_intent(
        matching,
        _transport_outcomes(records),
        retry_after_intent,
    )
