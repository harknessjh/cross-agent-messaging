# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Inbound message validation, lifecycle, causal, and audit orchestration."""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import (
    causal,
    journal,
    lifecycle,
    participants,
    profile,
    project,
    state,
    validation,
)
from .protocol import (
    CamUsageError,
    CamValidationError,
    ValidationPolicy,
    parse_exact_bytes,
)
from .state import StateStore

InboundError = CamUsageError | CamValidationError | state.StateError


@dataclass(frozen=True, slots=True)
class InboundParties:
    """The two roster participants correlated to one inbound envelope."""

    local: participants.Participant
    sender: participants.Participant


def record_summary(record: dict[str, Any]) -> dict[str, Any]:
    """Return the public redacted summary for one journal record."""

    message = record["message"]
    return {
        "sequence": record["sequence"],
        "record_id": record["record_id"],
        "project_id": record["project_id"],
        "recorded_at": record["recorded_at"],
        "event_type": record["event_type"],
        "previous_record_sha256": record["previous_record_sha256"],
        "record_sha256": record["record_sha256"],
        "message": (
            None
            if message is None
            else {
                "encoding": message["encoding"],
                "byte_length": message["byte_length"],
                "sha256": message["sha256"],
                "content": "<redacted>",
            }
        ),
        "attributes": "<redacted>",
    }


def _utc_now() -> tuple[dt.datetime, str]:
    observed = dt.datetime.now(dt.UTC)
    timespec = "microseconds" if observed.microsecond else "seconds"
    return observed, observed.isoformat(timespec=timespec).replace("+00:00", "Z")


def _rejection_codes(error: InboundError) -> tuple[str, list[str]]:
    if isinstance(error, CamValidationError):
        problem_codes = list(
            dict.fromkeys(problem.code[:80] for problem in error.problems)
        )
        return "validation.failed", problem_codes[:16]
    code = error.code[:80]
    return code, [code]


def _uuid_values_equal(left: Any, right: Any) -> bool:
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    try:
        return uuid.UUID(left) == uuid.UUID(right)
    except (ValueError, AttributeError):
        return False


def _require_inbound_roster_endpoints(
    store: StateStore,
    transaction: project.ProjectTransaction,
    raw: bytes,
    *,
    local_selector: str,
) -> InboundParties:
    envelope = parse_exact_bytes(raw)
    snapshot = store.snapshot(transaction=transaction)
    local = snapshot.roster.select(local_selector)
    if local.status != participants.ParticipantStatus.BOUND or local.binding is None:
        raise CamUsageError(
            "roster.recipient_unavailable",
            "local receiving participant must have an active roster binding",
        )
    recipient = envelope.get("recipient")
    if not isinstance(recipient, dict) or (
        recipient.get("vendor") != local.vendor
        or recipient.get("agent_name") != local.common_name
        or not _uuid_values_equal(recipient.get("session_id"), local.binding.session_id)
    ):
        raise CamUsageError(
            "roster.recipient_mismatch",
            "envelope recipient does not match the selected local participant",
        )

    claimed_sender = envelope.get("claimed_sender")
    sender_matches = [
        candidate
        for candidate in snapshot.roster.participants.values()
        if candidate.status == participants.ParticipantStatus.BOUND
        and candidate.binding is not None
        and isinstance(claimed_sender, dict)
        and claimed_sender.get("vendor") == candidate.vendor
        and claimed_sender.get("agent_name") == candidate.common_name
        and _uuid_values_equal(
            claimed_sender.get("session_id"), candidate.binding.session_id
        )
    ]
    if len(sender_matches) != 1:
        raise CamUsageError(
            "roster.sender_unknown",
            "envelope claimed_sender must match one active project participant",
        )
    return InboundParties(local=local, sender=sender_matches[0])


def prior_inbound_validation(
    binding: project.ProjectBinding,
    *,
    raw: bytes,
    message_id: str,
    recipient_participant_id: str,
) -> dict[str, Any] | None:
    """Return the prior recipient-specific validation for one exact message."""

    validations = journal.replay_records(
        binding,
        event_types={"message.inbound.validated"},
    )
    candidates: list[dict[str, Any]] = []
    observed_record_ids: set[str] = set()
    for record in reversed(validations):
        attributes = record.get("attributes")
        if not isinstance(attributes, dict) or not (
            _uuid_values_equal(attributes.get("message_id"), message_id)
            and attributes.get("recipient_participant_id") == recipient_participant_id
        ):
            continue
        observed_record_id = attributes.get("observed_record_id")
        if not isinstance(observed_record_id, str):
            continue
        try:
            if str(uuid.UUID(observed_record_id)) != observed_record_id:
                continue
        except ValueError:
            continue
        candidates.append(record)
        observed_record_ids.add(observed_record_id)
    if not candidates:
        return None

    observations = {
        record["record_id"]: record
        for record in journal.replay_records(
            binding,
            event_types={"message.inbound.observed"},
            record_ids=observed_record_ids,
        )
    }
    expected_digest = hashlib.sha256(raw).hexdigest()
    for record in candidates:
        attributes = record["attributes"]
        observation = observations.get(attributes["observed_record_id"])
        encoded = observation.get("message") if observation is not None else None
        if not isinstance(encoded, dict) or (
            encoded.get("byte_length") != len(raw)
            or encoded.get("sha256") != expected_digest
        ):
            continue
        if journal.decode_exact_message(observation) == raw:
            return record
    return None


def _record_inbound_duplicate(
    binding: project.ProjectBinding,
    transaction: project.ProjectTransaction,
    *,
    observed_record: dict[str, Any],
    message_id: str,
    prior_validation: dict[str, Any],
    parties: InboundParties,
    lifecycle_summary: dict[str, Any] | None,
    lifecycle_committed: bool,
    validation_profile: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    """Journal and describe one recipient-specific exact retransmission."""

    duplicate_now, duplicate_at = _utc_now()
    duplicate_record = journal.append_record(
        binding,
        event_type="message.inbound.duplicate",
        attributes={
            "observed_record_id": observed_record["record_id"],
            "message_id": message_id,
            "prior_validated_record_id": prior_validation["record_id"],
            "sender_participant_id": parties.sender.participant_id,
            "recipient_participant_id": parties.local.participant_id,
            "authorization_evaluated": False,
            "action_authorized": False,
            "validation_profile": validation_profile,
            "observed_at": duplicate_at,
        },
        now=duplicate_now,
        transaction=transaction,
    )
    prior_attributes = prior_validation.get("attributes")
    held = isinstance(prior_attributes, dict) and (
        prior_attributes.get("assessment") == "held_for_clarification"
    )
    return_code = 4 if held else 0
    payload = {
        "ok": not held,
        "status": "held_for_clarification" if held else "duplicate",
        "duplicate": True,
        "authorization_evaluated": False,
        "action_authorized": False,
        "validation_profile": validation_profile,
        "observed_record": record_summary(observed_record),
        "duplicate_record": record_summary(duplicate_record),
        "as_participant": {
            "participant_id": parties.local.participant_id,
            "common_name": parties.local.common_name,
        },
        "lifecycle_committed": lifecycle_committed,
        "lifecycle": lifecycle_summary,
    }
    if held:
        causal_assessment = (
            prior_attributes.get("causal_assessment")
            if isinstance(prior_attributes, dict)
            else None
        )
        reason_code = (
            causal_assessment.get("reason_code")
            if isinstance(causal_assessment, dict)
            else None
        )
        reason_detail = (
            causal_assessment.get("reason_detail")
            if isinstance(causal_assessment, dict)
            else None
        )
        payload["error"] = {
            "code": reason_code or "causal.stale_instruction",
            "detail": reason_detail
            or "exact retransmission remains held; send a fresh corrected envelope",
        }
        if isinstance(prior_attributes, dict):
            payload["causal"] = causal_assessment
    return return_code, payload


def _record_inbound_rejection(
    binding: project.ProjectBinding,
    transaction: project.ProjectTransaction,
    *,
    observed_record: dict[str, Any],
    error: InboundError,
    validation_profile: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    """Append one bounded rejection correlated to the preserved observation."""

    error_code, problem_codes = _rejection_codes(error)
    rejected_now, _ = _utc_now()
    rejected_record = journal.append_record(
        binding,
        event_type="message.inbound.rejected",
        attributes={
            "error_code": error_code,
            "problem_codes": problem_codes,
            "observed_record_id": observed_record["record_id"],
            "validation_profile": validation_profile,
        },
        now=rejected_now,
        transaction=transaction,
    )
    return 2, {
        "ok": False,
        "status": "rejected",
        "error": {
            "code": error_code,
            "problem_codes": problem_codes,
        },
        "observed_record": record_summary(observed_record),
        "rejected_record": record_summary(rejected_record),
        "validation_profile": validation_profile,
    }


def _exact_ingest_source(
    *, message_path: str | None, exact_message: bytes | None
) -> bytes:
    if exact_message is None:
        if message_path is None:
            raise project.ProjectError(
                "message.source", "inbound message source is missing"
            )
        return project.read_private_bytes(
            Path(message_path), max_bytes=journal.MAX_EXACT_MESSAGE_BYTES
        )
    if message_path is not None:
        raise project.ProjectError(
            "message.source", "inbound message sources are mutually exclusive"
        )
    return exact_message


def _observe_inbound(
    binding: project.ProjectBinding,
    transaction: project.ProjectTransaction,
    raw: bytes,
    *,
    source: str,
) -> tuple[dict[str, Any], dt.datetime]:
    observed_now, observed_at = _utc_now()
    return (
        journal.append_record(
            binding,
            event_type="message.inbound.observed",
            exact_message=raw,
            attributes={"source": source, "observed_at": observed_at},
            now=observed_now,
            transaction=transaction,
        ),
        observed_now,
    )


def _candidate_message_id(envelope: dict[str, Any]) -> str | None:
    candidate = envelope.get("message_id")
    if not isinstance(candidate, str):
        return None
    try:
        return str(uuid.UUID(candidate))
    except ValueError:
        return None


def _duplicate_lifecycle_entry(
    store: StateStore,
    transaction: project.ProjectTransaction,
    envelope: dict[str, Any],
    message_id: str,
) -> lifecycle.LifecycleEntry:
    root_value = (
        message_id
        if envelope.get("type") in lifecycle.ROOT_TYPES
        else envelope.get("in_reply_to")
    )
    root_id = str(uuid.UUID(root_value)) if isinstance(root_value, str) else ""
    entry = store.snapshot(transaction=transaction).lifecycle.entries.get(root_id)
    if entry is None:
        raise CamUsageError(
            "state.duplicate_missing",
            "validated duplicate has no lifecycle root",
        )
    return entry


def _early_exact_duplicate(
    binding: project.ProjectBinding,
    transaction: project.ProjectTransaction,
    store: StateStore,
    *,
    raw: bytes,
    envelope: dict[str, Any],
    parties: InboundParties,
    observed_record: dict[str, Any],
    validation_profile: dict[str, Any],
) -> tuple[int, dict[str, Any]] | None:
    message_id = _candidate_message_id(envelope)
    if message_id is None:
        return None
    prior_validation = prior_inbound_validation(
        binding,
        raw=raw,
        message_id=message_id,
        recipient_participant_id=parties.local.participant_id,
    )
    if prior_validation is None:
        return None
    prior_attributes = prior_validation.get("attributes")
    commitment_marker = (
        prior_attributes.get("lifecycle_committed")
        if isinstance(prior_attributes, dict)
        else None
    )
    lifecycle_committed = commitment_marker is True or (
        commitment_marker is None
        and store.preserved_message(message_id, transaction=transaction) == raw
    )
    if lifecycle_committed:
        lifecycle_summary = _duplicate_lifecycle_entry(
            store, transaction, envelope, message_id
        ).as_dict()
    else:
        candidate = (
            prior_attributes.get("lifecycle_candidate")
            if isinstance(prior_attributes, dict)
            else None
        )
        if candidate is not None and not isinstance(candidate, dict):
            raise CamUsageError(
                "state.duplicate_missing",
                "held validation has no preserved lifecycle candidate",
            )
        lifecycle_summary = candidate
    return _record_inbound_duplicate(
        binding,
        transaction,
        observed_record=observed_record,
        message_id=message_id,
        prior_validation=prior_validation,
        parties=parties,
        lifecycle_summary=lifecycle_summary,
        lifecycle_committed=lifecycle_committed,
        validation_profile=validation_profile,
    )


def _prepare_initial_inbound(
    store: StateStore,
    transaction: project.ProjectTransaction,
    *,
    raw: bytes,
    renewal_of: str | None,
) -> state.LifecyclePlan:
    validation_now, _ = _utc_now()
    plan = store.prepare_inbound_lifecycle(
        raw,
        renewal_of=renewal_of,
        now=validation_now,
        transaction=transaction,
    )
    if plan.preview.state == lifecycle.LifecycleState.EXPIRED_UNCONFIRMED:
        expired_commit_now, _ = _utc_now()
        store.commit_lifecycle(
            plan,
            transaction=transaction,
            now=expired_commit_now,
        )
        raise CamUsageError(
            "state.root_expired",
            "root expired before application handling and was not accepted",
        )
    return plan


def _validate_initial_inbound(
    store: StateStore,
    transaction: project.ProjectTransaction,
    *,
    raw: bytes,
    as_participant: str,
    observed_at: dt.datetime,
) -> tuple[dict[str, Any], InboundParties]:
    """Validate wire bytes and roster endpoints without interpreting lifecycle."""

    envelope = validation.validate_exact_bytes(
        raw,
        now=observed_at,
        policy=ValidationPolicy(allow_expired=True),
    ).envelope
    if envelope.get("type") == "cancel":
        target = envelope.get("in_reply_to")
        if not isinstance(target, str):
            raise CamUsageError(
                "lifecycle.cancel_target", "cancel target identifier is invalid"
            )
        preserved = store.preserved_message(target, transaction=transaction)
        if preserved is None:
            raise CamUsageError(
                "state.root_missing",
                "cancel target is not present in project lifecycle state",
            )
        state.validate_cancel_exact_bytes(
            raw,
            preserved,
            now=observed_at,
            allow_expired=True,
        )
    parties = _require_inbound_roster_endpoints(
        store,
        transaction,
        raw,
        local_selector=as_participant,
    )
    return envelope, parties


def _refresh_inbound_plan(
    store: StateStore,
    transaction: project.ProjectTransaction,
    *,
    raw: bytes,
    renewal_of: str | None,
) -> state.LifecyclePlan:
    commit_check_now, _ = _utc_now()
    plan = store.prepare_inbound_lifecycle(
        raw,
        renewal_of=renewal_of,
        now=commit_check_now,
        transaction=transaction,
    )
    if plan.preview.state == lifecycle.LifecycleState.EXPIRED_UNCONFIRMED:
        store.commit_lifecycle(
            plan,
            transaction=transaction,
            now=commit_check_now,
        )
        raise CamUsageError(
            "state.root_expired",
            "root expired before application handling and was not accepted",
        )
    final_check_now, _ = _utc_now()
    state.require_plan_freshness(plan, now=final_check_now)
    return plan


def _commit_inbound_plan(
    store: StateStore,
    transaction: project.ProjectTransaction,
    plan: state.LifecyclePlan,
) -> tuple[lifecycle.LifecycleEntry, state.ProjectionRefreshError | None]:
    try:
        return store.commit_lifecycle(plan, transaction=transaction), None
    except state.ProjectionRefreshError as error:
        # The canonical lifecycle event is already journaled. Continue the
        # ingest audit and report that only the rebuildable cache is stale.
        return plan.preview, error


def _record_validated_inbound(
    binding: project.ProjectBinding,
    transaction: project.ProjectTransaction,
    *,
    observed_record: dict[str, Any],
    plan: state.LifecyclePlan,
    parties: InboundParties,
    entry: lifecycle.LifecycleEntry,
    projection_error: state.ProjectionRefreshError | None,
    validation_profile: dict[str, Any],
    causal_assessment: causal.CausalAssessment,
) -> tuple[int, dict[str, Any]]:
    """Record one non-held inbound message whose lifecycle was committed."""

    validated_now, validated_at = _utc_now()
    validated_record = journal.append_record(
        binding,
        event_type="message.inbound.validated",
        attributes={
            "observed_record_id": observed_record["record_id"],
            "message_id": plan.attributes.get(
                "message_id", plan.attributes["root_message_id"]
            ),
            "sender_participant_id": parties.sender.participant_id,
            "recipient_participant_id": parties.local.participant_id,
            "authorization_evaluated": False,
            "action_authorized": False,
            "assessment": "validated",
            "causal_assessment": (
                causal_assessment.as_dict() if causal_assessment.enforced else None
            ),
            "lifecycle_committed": True,
            "lifecycle_candidate": entry.as_dict(),
            "state_projection_current": projection_error is None,
            "validation_profile": validation_profile,
            "observed_at": validated_at,
        },
        now=validated_now,
        transaction=transaction,
    )
    last_committed_record = None
    if projection_error is not None:
        last_committed_record = {
            "record_id": projection_error.record_id,
            "sequence": projection_error.sequence,
        }
    payload = {
        "ok": True,
        "status": "validated",
        "duplicate": False,
        "authorization_evaluated": False,
        "action_authorized": False,
        "lifecycle_committed": True,
        "validation_profile": validation_profile,
        "state_projection": {
            "current": projection_error is None,
            "rebuild_required": projection_error is not None,
            "last_committed_record": last_committed_record,
        },
        "observed_record": record_summary(observed_record),
        "validated_record": record_summary(validated_record),
        "as_participant": {
            "participant_id": parties.local.participant_id,
            "common_name": parties.local.common_name,
        },
        "lifecycle": entry.as_dict(),
    }
    if causal_assessment.enforced:
        payload["causal"] = causal_assessment.as_dict()
    return 0, payload


def _record_held_inbound(
    binding: project.ProjectBinding,
    transaction: project.ProjectTransaction,
    *,
    observed_record: dict[str, Any],
    envelope: dict[str, Any],
    parties: InboundParties,
    validation_profile: dict[str, Any],
    causal_assessment: causal.CausalAssessment,
) -> tuple[int, dict[str, Any]]:
    """Record a validated causal hold without applying lifecycle or action state."""

    validated_now, validated_at = _utc_now()
    validated_record = journal.append_record(
        binding,
        event_type="message.inbound.validated",
        attributes={
            "observed_record_id": observed_record["record_id"],
            "message_id": envelope["message_id"],
            "sender_participant_id": parties.sender.participant_id,
            "recipient_participant_id": parties.local.participant_id,
            "authorization_evaluated": False,
            "action_authorized": False,
            "assessment": "held_for_clarification",
            "causal_assessment": causal_assessment.as_dict(),
            "lifecycle_committed": False,
            "lifecycle_candidate": None,
            "state_projection_current": True,
            "validation_profile": validation_profile,
            "observed_at": validated_at,
        },
        now=validated_now,
        transaction=transaction,
    )
    return 4, {
        "ok": False,
        "status": "held_for_clarification",
        "duplicate": False,
        "authorization_evaluated": False,
        "action_authorized": False,
        "lifecycle_committed": False,
        "lifecycle": None,
        "validation_profile": validation_profile,
        "state_projection": {
            "current": True,
            "rebuild_required": False,
            "last_committed_record": None,
        },
        "observed_record": record_summary(observed_record),
        "validated_record": record_summary(validated_record),
        "as_participant": {
            "participant_id": parties.local.participant_id,
            "common_name": parties.local.common_name,
        },
        "causal": causal_assessment.as_dict(),
        "error": {
            "code": causal_assessment.reason_code or "causal.stale_instruction",
            "detail": causal_assessment.reason_detail
            or (
                "request does not cover the receiver's current project-journal "
                "frontier; clarify with a fresh envelope"
            ),
        },
    }


def ingest_message(
    binding: project.ProjectBinding,
    *,
    message_path: str | None,
    as_participant: str,
    renewal_of: str | None,
    exact_message: bytes | None = None,
    observed_source: str = "owner_only_file",
) -> tuple[int, dict[str, Any]]:
    """Preserve, validate, correlate, and journal one inbound CAM message."""

    validation_profile = profile.validation_profile_report()
    raw = _exact_ingest_source(
        message_path=message_path,
        exact_message=exact_message,
    )
    store = StateStore(binding)
    with project.project_transaction(binding) as transaction:
        observed_record, observed_at = _observe_inbound(
            binding,
            transaction,
            raw,
            source=observed_source,
        )
        try:
            envelope, parties = _validate_initial_inbound(
                store,
                transaction,
                raw=raw,
                as_participant=as_participant,
                observed_at=observed_at,
            )
            duplicate_result = _early_exact_duplicate(
                binding,
                transaction,
                store,
                raw=raw,
                envelope=envelope,
                parties=parties,
                observed_record=observed_record,
                validation_profile=validation_profile,
            )
            if duplicate_result is not None:
                return duplicate_result
        except (CamUsageError, CamValidationError, state.StateError) as error:
            return _record_inbound_rejection(
                binding,
                transaction,
                observed_record=observed_record,
                error=error,
                validation_profile=validation_profile,
            )

        try:
            lifecycle_candidate: state.LifecyclePlan | InboundError = (
                _prepare_initial_inbound(
                    store,
                    transaction,
                    raw=raw,
                    renewal_of=renewal_of,
                )
            )
        except (CamUsageError, CamValidationError, state.StateError) as error:
            if (
                isinstance(error, CamUsageError) and error.code == "state.root_expired"
            ) or isinstance(error, state.ProjectionRefreshError):
                return _record_inbound_rejection(
                    binding,
                    transaction,
                    observed_record=observed_record,
                    error=error,
                    validation_profile=validation_profile,
                )
            lifecycle_candidate = error

        try:
            causal_assessment = causal.assess_inbound_order(
                journal.replay_records(
                    binding,
                    event_types=causal.CAUSAL_JOURNAL_EVENT_TYPES,
                ),
                raw,
                local_participant_id=parties.local.participant_id,
                sender_participant_id=parties.sender.participant_id,
            )
        except causal.CausalError as error:
            causal_assessment = causal.CausalAssessment(
                enforced=True,
                held=True,
                conversation_id=None,
                reason_code=error.code,
                reason_detail=error.detail,
            )
        if causal_assessment.held:
            return _record_held_inbound(
                binding,
                transaction,
                observed_record=observed_record,
                envelope=envelope,
                parties=parties,
                validation_profile=validation_profile,
                causal_assessment=causal_assessment,
            )
        if isinstance(
            lifecycle_candidate,
            (CamUsageError, CamValidationError, state.StateError),
        ):
            return _record_inbound_rejection(
                binding,
                transaction,
                observed_record=observed_record,
                error=lifecycle_candidate,
                validation_profile=validation_profile,
            )

        try:
            plan = _refresh_inbound_plan(
                store,
                transaction,
                raw=raw,
                renewal_of=renewal_of,
            )
            entry, projection_error = _commit_inbound_plan(store, transaction, plan)
        except (CamUsageError, CamValidationError, state.StateError) as error:
            return _record_inbound_rejection(
                binding,
                transaction,
                observed_record=observed_record,
                error=error,
                validation_profile=validation_profile,
            )
        return _record_validated_inbound(
            binding,
            transaction,
            observed_record=observed_record,
            plan=plan,
            parties=parties,
            entry=entry,
            projection_error=projection_error,
            validation_profile=validation_profile,
            causal_assessment=causal_assessment,
        )
