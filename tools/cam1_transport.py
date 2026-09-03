# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Audited project orchestration for one-shot local CAM/1 transports."""

from __future__ import annotations

import posix as _posix
import sys

if __name__ == "__main__":
    _entry = f"{__file__.rsplit('/', 1)[0]}/_cam1_entry.py"
    if not _entry.startswith("/"):
        _entry = f"{_posix.getcwd()}/{_entry}"
    try:
        _posix.execv(
            sys.executable,
            [sys.executable, "-I", "-B", _entry, "cam1_transport", *sys.argv[1:]],
        )
    except OSError:
        sys.stderr.write(
            '{"error":{"code":"bootstrap.isolation_failed",'
            '"detail":"could not enter isolated Python mode"},"ok":false}\n'
        )
        raise SystemExit(2) from None

import hashlib
import shlex
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from tools import cam1
from tools import cam1_transport_native as _native
from tools import cam1_transport_retry as _retry
from tools.cam1lib import (
    causal,
    journal,
    lifecycle,
    participants,
    product_approvals,
    product_executables,
    project,
    routing,
    state,
)
from tools.cam1lib import transport_cli as _transport_cli

TransportError = _native.TransportError
ValidatedEnvelope = _native.ValidatedEnvelope
_canonical_uuid = _native._canonical_uuid
_delivery_state = _native._delivery_state
_domain_transport_error = _native._domain_transport_error
_preflight_claude_session = _native._preflight_claude_session
_record_summary = _native._record_summary
_resolve_project = _native._resolve_project
_require_project_session_cwd = _native._require_project_session_cwd
_require_live_validation_profile = _native._require_live_validation_profile
_require_session_guard = _native._require_session_guard
_send_to_claude = _native._send_to_claude
_send_to_codex_queue = _native._send_to_codex_queue
_utc_now = _native._utc_now
_uuid_values_equal = _native._uuid_values_equal
_transport_outcomes = _retry._transport_outcomes

# Clean, source-profiled readers released before account executable approval
# existed.  Automatic grandfathering is deliberately limited to enrollment
# proposals produced by these exact historical profiles.  Later enrollment or
# unversioned metadata records must use the ordinary candidate-card approval.
LEGACY_PRODUCT_APPROVAL_PROFILES = frozenset(
    {
        # 22d49669337792cdad9631b7928eaa72a1164a35
        "c1752841229245561fcbd34570749978d1229098f8b09861aae44305b666c06d",
        # 71a69f7558d88192fc48ace124e019003e40179b
        "1f6864186c9a9ca763b5cef949dd4319b59b8b154be281ca5b2665c8ae997fd8",
        # 9710ff4e74baaf3416acaf324dee1308ae338b0c
        "324c2a06e6a909eb15722ebc3a8b086e77decee08860200b74f85d4a37914f0e",
    }
)


def doctor(
    *,
    claude_bin: str,
    codex_bin: str,
    timeout_seconds: float,
    prevalidated: bool = False,
) -> dict[str, Any]:
    """Run native diagnostics through this module's patchable helper seams."""

    return _native.doctor(
        claude_bin=claude_bin,
        codex_bin=codex_bin,
        timeout_seconds=timeout_seconds,
        prevalidated=prevalidated,
        _facade=sys.modules[__name__],
    )


def __getattr__(name: str) -> Any:
    if name == "JsonArgumentParser":
        return _transport_cli.JsonArgumentParser
    return getattr(_native, name)


@dataclass(slots=True)
class _SendAttempt:
    """Mutable audit context shared with an immediately-before-send hook."""

    participant_id: str
    transport: str
    route_address: str
    message_id: str | None = None
    intent_record: dict[str, Any] | None = None
    lifecycle_plan: state.LifecyclePlan | None = None
    lifecycle_committed: bool = False
    projection_error: state.ProjectionRefreshError | None = None
    dispatch_started: bool = False


def _require_bound_participant(
    store: state.StateStore,
    selector: str,
    *,
    vendor: str,
    transaction: project.ProjectTransaction,
) -> participants.Participant:
    participant = store.snapshot(transaction=transaction).roster.select(selector)
    if participant.vendor != vendor:
        raise TransportError(
            "roster.vendor_mismatch",
            f"participant is not a {vendor} session",
        )
    if participant.binding is None:
        raise TransportError(
            "roster.participant_unbound",
            "participant has no operator-correlated full session binding",
        )
    if participant.status == participants.ParticipantStatus.RETIRED:
        raise TransportError(
            "roster.participant_retired",
            "retired participant cannot be used for live transport",
        )
    if participant.status != participants.ParticipantStatus.BOUND:
        raise TransportError(
            "roster.participant_stale",
            "stale participant must be explicitly rebound before live transport",
        )
    return participant


def _require_complete_claude_binding(
    participant: participants.Participant,
) -> participants.SessionBinding:
    """Require the operator-visible Claude metadata used by live discovery."""

    binding = participant.binding
    assert binding is not None
    if binding.session_label is None or binding.session_kind is None:
        raise TransportError(
            "claude.binding_incomplete",
            "Claude binding lacks an operator-confirmed session label or kind; "
            "rebind the participant before discovery or sending",
        )
    return binding


def _require_approved_product_executable(
    participant: participants.Participant,
    supplied_path: str,
) -> None:
    """Fail before product I/O unless the exact rostered executable is used."""

    approved = participant.approved_product_executable
    if approved is None:
        raise TransportError(
            "roster.product_executable_missing",
            "participant has no operator-approved product executable; update its "
            "metadata before live transport",
        )
    if supplied_path != approved:
        raise TransportError(
            "roster.product_executable_mismatch",
            "live transport executable does not match the operator-approved roster "
            "path",
        )


def resolve_product_binary(
    value: str,
    *,
    vendor: str,
    binding: project.ProjectBinding | None = None,
) -> str:
    """Resolve one account-approved product, migrating a legacy roster once."""

    try:
        resolved, _approval = product_approvals.require_approved_executable(
            vendor=vendor,
            product_bin=value,
            allow_path_lookup=False,
        )
        return resolved
    except product_approvals.ProductApprovalError as error:
        if (
            error.code
            not in {
                "product_approval.required",
                "product_approval.registry_missing",
                "path.missing",
            }
            or binding is None
        ):
            raise TransportError(error.code, error.detail) from error

    try:
        candidate_path, _candidate_source = product_executables.resolve_candidate_path(
            vendor, value, allow_path_lookup=False
        )
    except product_approvals.ProductApprovalError as error:
        raise TransportError(error.code, error.detail) from error

    try:
        snapshot = state.StateStore(binding).snapshot()
    except (cam1.CamUsageError, project.ProjectError) as error:
        raise _domain_transport_error(error) from error
    eligible = sorted(
        (
            participant
            for participant in snapshot.roster.participants.values()
            if participant.vendor == vendor
            and participant.status == participants.ParticipantStatus.BOUND
            and participant.binding is not None
            and participant.approved_product_executable == candidate_path
        ),
        key=lambda participant: participant.participant_id,
    )
    evidenced = [
        (participant, _legacy_product_confirmation(snapshot, participant))
        for participant in eligible
    ]
    evidenced = [item for item in evidenced if item[1] is not None]
    if not evidenced:
        recovery = product_executables.discovery_command(vendor, candidate_path)
        raise TransportError(
            "product_approval.required",
            "product executable has no account approval; run "
            f"{shlex.join(recovery)}, review its card, then run the exact "
            "approval_command it returns after replacing DIRECT_OPERATOR_REFERENCE",
        )
    legacy, confirmation = evidenced[0]
    assert confirmation is not None
    assert legacy.binding is not None
    try:
        approved = product_approvals.grandfather_candidate(
            vendor=vendor,
            product_bin=candidate_path,
            operator_reference=confirmation[0],
            migration={
                "project_id": binding.project_id,
                "participant_id": legacy.participant_id,
                "binding_generation": legacy.binding.generation,
                "source": confirmation[1],
                "source_reference": confirmation[2],
            },
        )
        return approved["candidate"]["canonical_path"]
    except product_approvals.ProductApprovalError as error:
        raise TransportError(error.code, error.detail) from error


def _legacy_product_confirmation(
    snapshot: state.StateSnapshot,
    participant: participants.Participant,
) -> tuple[str, str, str] | None:
    """Find direct historical operator evidence for the current roster path."""

    executable = participant.approved_product_executable
    confirmed_proposals = sorted(
        (
            proposal
            for proposal in snapshot.enrollment.proposals.values()
            if proposal.participant_id == participant.participant_id
            and proposal.status.value == "confirmed"
            and proposal.operator_reference is not None
            and proposal.execution_context.product_executable == executable
            and proposal.execution_context.validation_profile_sha256
            in LEGACY_PRODUCT_APPROVAL_PROFILES
        ),
        key=lambda proposal: (proposal.confirmed_at or "", proposal.proposal_id),
        reverse=True,
    )
    if confirmed_proposals:
        proposal = confirmed_proposals[0]
        assert proposal.operator_reference is not None
        return (
            proposal.operator_reference,
            "confirmed_enrollment",
            proposal.proposal_id,
        )
    return None


def discover_product_executable(
    *, vendor: str, product_bin: str | None
) -> dict[str, Any]:
    try:
        card = product_executables.candidate_card(
            product_executables.discover_candidate(vendor, product_bin)
        )
        candidate = card["candidate"]
        status = product_approvals.approval_status(
            vendor=vendor,
            product_bin=candidate["canonical_path"],
        )
        active = status["active"]
        if not active:
            return card

        current = active[0]
        attributes = current["attributes"]
        card["existing_approval"] = {
            "record_id": current["record_id"],
            "fingerprint_sha256": attributes["fingerprint_sha256"],
            "recorded_at": current["recorded_at"],
        }
        if attributes["fingerprint_sha256"] == candidate["fingerprint_sha256"]:
            card["status"] = "already_approved"
            card["next_step"] = (
                "This exact executable fingerprint already has an active account "
                "approval; no new approval is required."
            )
            return card

        revocation_arguments = (
            "product-revoke",
            "--vendor",
            vendor,
            "--product-bin",
            candidate["canonical_path"],
            "--approval-record-id",
            current["record_id"],
            "--expected-fingerprint-sha256",
            attributes["fingerprint_sha256"],
            "--operator-reference",
            "DIRECT_OPERATOR_REFERENCE",
        )
        command_prefix = tuple(card["approval_command"][:2])
        revocation_command = (*command_prefix, *revocation_arguments)
        card.update(
            {
                "status": "replacement_approval_required",
                "revocation_arguments": list(revocation_arguments),
                "revocation_command": list(revocation_command),
                "revocation_command_text": shlex.join(revocation_command),
                "next_step": (
                    "The canonical path has a different active fingerprint. Review "
                    "product-status, directly confirm and run the returned guarded "
                    "product-revoke command, then run product-discover again and "
                    "directly approve the new candidate."
                ),
            }
        )
        return card
    except product_approvals.ProductApprovalError as error:
        raise TransportError(error.code, error.detail) from error


def approve_product_executable(**kwargs: Any) -> dict[str, Any]:
    try:
        return product_approvals.approve_candidate(**kwargs)
    except product_approvals.ProductApprovalError as error:
        raise TransportError(error.code, error.detail) from error


def product_executable_status(**kwargs: Any) -> dict[str, Any]:
    try:
        return product_approvals.approval_status(**kwargs)
    except product_approvals.ProductApprovalError as error:
        raise TransportError(error.code, error.detail) from error


def revoke_product_executable(**kwargs: Any) -> dict[str, Any]:
    try:
        return product_approvals.revoke_approval(**kwargs)
    except product_approvals.ProductApprovalError as error:
        raise TransportError(error.code, error.detail) from error


def _require_current_product_approval(vendor: str, path: str) -> None:
    """Recheck an operation-local approval immediately before product I/O."""

    try:
        product_approvals.require_approved_metadata(
            vendor=vendor,
            product_bin=path,
        )
    except product_approvals.ProductApprovalError as error:
        raise TransportError(error.code, error.detail) from error


async def list_local_peers(*, claude_bin: str, timeout_seconds: float) -> Any:
    """Run native discovery with an account-approval recheck at product I/O."""

    return await _native.list_local_peers(
        claude_bin=claude_bin,
        timeout_seconds=timeout_seconds,
    )


def _require_bound_claude_metadata(
    participant: participants.Participant,
    session: routing.AgentViewSession,
) -> None:
    """Keep mutable product metadata inside the operator-confirmed binding."""

    binding = _require_complete_claude_binding(participant)
    if (
        binding.session_label is not None
        and session.product_name != binding.session_label
    ):
        raise TransportError(
            "claude.session_label_mismatch",
            "fresh Claude product name does not match the operator-confirmed "
            "session label; update the stable binding before sending",
        )
    if session.kind.casefold() != binding.session_kind.casefold():
        raise TransportError(
            "claude.session_kind_mismatch",
            "fresh Claude session kind does not match the operator-confirmed binding",
        )


def _require_safe_retry(
    binding: project.ProjectBinding,
    validated: ValidatedEnvelope,
    *,
    retry_after_intent: str | None,
    known_renewal_roots: frozenset[str],
    records: tuple[dict[str, Any], ...] | None = None,
) -> str | None:
    """Apply journal-backed retry policy through the stable facade seam."""

    if records is not None:
        return _retry._require_safe_retry_from_records(
            records,
            validated,
            retry_after_intent=retry_after_intent,
            known_renewal_roots=known_renewal_roots,
        )
    return _retry.require_safe_retry(
        binding,
        validated,
        retry_after_intent=retry_after_intent,
        known_renewal_roots=known_renewal_roots,
    )


def _intent_attributes(
    validated: ValidatedEnvelope,
    attempt: _SendAttempt,
    *,
    sender_participant_id: str,
    recipient_participant_id: str,
    recipient_session_id: str,
    renewal_of: str | None,
    retry_after_intent: str | None,
    validation_profile: dict[str, Any],
    dirty_validator_override: bool,
    observed_at: str,
    causal_context: causal.CausalContext | None,
) -> dict[str, Any]:
    action = validated.envelope["action"]
    return {
        "participant_id": attempt.participant_id,
        "sender_participant_id": sender_participant_id,
        "recipient_participant_id": recipient_participant_id,
        "message_id": validated.envelope["message_id"],
        "message_type": validated.envelope["type"],
        "idempotency_key": _canonical_uuid(
            action["idempotency_key"], label="idempotency_key"
        ),
        "semantic_operation_sha256": lifecycle.semantic_operation_digest(
            validated.envelope
        ),
        "transport": attempt.transport,
        "route_address": attempt.route_address,
        "recipient_session_id": recipient_session_id,
        "renewal_of": renewal_of,
        "retry_after_intent": retry_after_intent,
        "against_sha256": (
            hashlib.sha256(validated.original_raw).hexdigest()
            if validated.original_raw is not None
            else None
        ),
        "validation_profile": validation_profile,
        "dirty_validator_override": dirty_validator_override,
        "causal_context": (
            causal_context.as_dict() if causal_context is not None else None
        ),
        "observed_at": observed_at,
    }


def _require_roster_endpoints(
    store: state.StateStore,
    transaction: project.ProjectTransaction,
    validated: ValidatedEnvelope,
    recipient_participant: participants.Participant,
) -> participants.Participant:
    """Bind claimed wire endpoints to active project-roster identities."""

    recipient_binding = recipient_participant.binding
    if recipient_binding is None:
        raise cam1.CamUsageError(
            "roster.participant_unbound",
            "recipient participant has no active session binding",
        )
    wire_recipient = validated.envelope.get("recipient")
    if not isinstance(wire_recipient, dict) or (
        wire_recipient.get("vendor") != recipient_participant.vendor
        or wire_recipient.get("agent_name") != recipient_participant.common_name
        or not _uuid_values_equal(
            wire_recipient.get("session_id"), recipient_binding.session_id
        )
    ):
        raise cam1.CamUsageError(
            "roster.recipient_mismatch",
            "envelope recipient does not match the selected project participant",
        )

    wire_sender = validated.envelope.get("claimed_sender")
    if not isinstance(wire_sender, dict):
        raise cam1.CamUsageError(
            "roster.sender_unknown",
            "envelope claimed_sender does not identify a project participant",
        )
    snapshot = store.snapshot(transaction=transaction)
    matches = [
        candidate
        for candidate in snapshot.roster.participants.values()
        if candidate.binding is not None
        and candidate.status == participants.ParticipantStatus.BOUND
        and candidate.vendor == wire_sender.get("vendor")
        and candidate.common_name == wire_sender.get("agent_name")
        and _uuid_values_equal(
            candidate.binding.session_id,
            wire_sender.get("session_id"),
        )
    ]
    if len(matches) != 1:
        raise cam1.CamUsageError(
            "roster.sender_unknown",
            "envelope claimed_sender must match one active project participant",
        )
    sender = matches[0]
    assert sender.binding is not None
    reply_to = validated.envelope.get("reply_to")
    expected_transport = participants.VENDOR_ROUTE_TRANSPORT[sender.vendor]
    if not isinstance(reply_to, dict) or (
        reply_to.get("transport") != expected_transport
        or not _uuid_values_equal(reply_to.get("address"), sender.binding.session_id)
    ):
        raise cam1.CamUsageError(
            "roster.callback_unusable",
            "live messages require the bound sender's supported return transport",
        )
    return sender


def _require_reply_slot_available(
    binding: project.ProjectBinding,
    validated: ValidatedEnvelope,
    *,
    records: tuple[dict[str, Any], ...] | None = None,
) -> None:
    """Reserve one reply transition until its prior transport outcome is known."""

    envelope = validated.envelope
    if envelope.get("type") not in cam1.REPLY_TYPES:
        return
    root_id = _canonical_uuid(envelope.get("in_reply_to"), label="in_reply_to")
    if records is None:
        records = journal.replay_records(
            binding,
            event_types=_retry.RETRY_JOURNAL_EVENT_TYPES,
        )
    outcomes = _transport_outcomes(records)
    for record in records:
        if record["event_type"] != "message.outbound.intent":
            continue
        prior_raw = journal.decode_exact_message(record)
        if prior_raw is None:
            raise TransportError(
                "transport.intent_invalid",
                "a prior outbound reply intent has no preserved envelope",
            )
        try:
            prior_envelope = cam1.parse_exact_bytes(prior_raw)
        except (cam1.CamUsageError, cam1.CamValidationError) as error:
            raise TransportError(
                "transport.intent_invalid",
                "a prior outbound reply intent cannot be safely interpreted",
            ) from error
        if prior_envelope.get("type") not in cam1.REPLY_TYPES or not _uuid_values_equal(
            prior_envelope.get("in_reply_to"), root_id
        ):
            continue
        linked = [
            outcome
            for outcome in outcomes.get(record["record_id"], [])
            if outcome["sequence"] > record["sequence"]
        ]
        if len(linked) == 1:
            outcome = linked[0]
            attributes = outcome.get("attributes")
            if (
                outcome["event_type"] == "transport.accepted"
                and isinstance(attributes, dict)
                and attributes.get("lifecycle_state_committed") is True
            ):
                continue
            if (
                outcome["event_type"] == "transport.not_accepted"
                and isinstance(attributes, dict)
                and attributes.get("delivery_state") == "not_attempted"
            ):
                continue
        raise TransportError(
            "transport.reply_transition_reserved",
            "an earlier reply for this lifecycle root has unresolved delivery; "
            "do not dispatch a competing reply",
        )


def _prepare_and_journal_intent(
    binding: project.ProjectBinding,
    store: state.StateStore,
    transaction: project.ProjectTransaction,
    validated: ValidatedEnvelope,
    attempt: _SendAttempt,
    *,
    recipient_participant: participants.Participant,
    renewal_of: str | None,
    retry_after_intent: str | None,
    validation_profile: dict[str, Any],
    dirty_validator_override: bool,
) -> None:
    event_now, observed_at = _utc_now()
    try:
        sender_participant = _require_roster_endpoints(
            store,
            transaction,
            validated,
            recipient_participant,
        )
        assert recipient_participant.binding is not None
        plan = store.prepare_lifecycle(
            validated.raw,
            renewal_of=renewal_of,
            preserved_against=validated.original_raw,
            require_preserved_against=True,
            now=event_now,
            transaction=transaction,
        )
        history_records = journal.replay_records(
            binding,
            event_types=causal.CAUSAL_JOURNAL_EVENT_TYPES,
        )
        _require_reply_slot_available(
            binding,
            validated,
            records=history_records,
        )
        known_renewal_roots: frozenset[str] = frozenset()
        if plan.preview.renewal_of is not None:
            snapshot = store.snapshot(transaction=transaction)
            known_renewal_roots = frozenset(
                entry.root_message_id
                for entry in snapshot.lifecycle.entries.values()
                if entry.idempotency_key == plan.preview.idempotency_key
                and entry.semantic_request_sha256
                == plan.preview.semantic_request_sha256
            )
        retry_intent_id = _require_safe_retry(
            binding,
            validated,
            retry_after_intent=retry_after_intent,
            known_renewal_roots=known_renewal_roots,
            records=history_records,
        )
        causal_context = causal.build_outbound_context(
            history_records,
            validated.envelope,
            sender_participant_id=sender_participant.participant_id,
            recipient_participant_id=recipient_participant.participant_id,
            renewal_of=plan.preview.renewal_of,
            retry_after_intent=retry_intent_id,
        )
        if validated.envelope["type"] in lifecycle.ROOT_TYPES and (
            plan.preview.state != lifecycle.LifecycleState.PENDING
        ):
            raise cam1.CamUsageError(
                "state.root_not_sendable",
                "outbound root is expired or already present in lifecycle state",
            )
        intent_record = journal.append_record(
            binding,
            event_type="message.outbound.intent",
            exact_message=validated.raw,
            attributes=_intent_attributes(
                validated,
                attempt,
                sender_participant_id=sender_participant.participant_id,
                recipient_participant_id=recipient_participant.participant_id,
                recipient_session_id=recipient_participant.binding.session_id,
                renewal_of=renewal_of,
                retry_after_intent=retry_intent_id,
                validation_profile=validation_profile,
                dirty_validator_override=dirty_validator_override,
                observed_at=observed_at,
                causal_context=causal_context,
            ),
            now=event_now,
            transaction=transaction,
        )
        attempt.lifecycle_plan = plan
        attempt.message_id = validated.envelope["message_id"]
        attempt.intent_record = intent_record
        if validated.envelope["type"] in lifecycle.ROOT_TYPES:
            if plan.duplicate:
                attempt.lifecycle_committed = True
            else:
                try:
                    store.commit_lifecycle(
                        plan,
                        transaction=transaction,
                        now=event_now,
                    )
                    attempt.lifecycle_committed = True
                except state.ProjectionRefreshError as error:
                    # The canonical root event is present. A later rebuild can
                    # refresh the disposable projection without resending.
                    attempt.lifecycle_committed = True
                    attempt.projection_error = error
        dispatch_now, _ = _utc_now()
        attempt.lifecycle_plan = store.prepare_lifecycle(
            validated.raw,
            renewal_of=renewal_of,
            preserved_against=validated.original_raw,
            require_preserved_against=True,
            now=dispatch_now,
            transaction=transaction,
        )
        final_dispatch_now, _ = _utc_now()
        state.require_plan_freshness(attempt.lifecycle_plan, now=final_dispatch_now)
    except (cam1.CamUsageError, cam1.CamValidationError, project.ProjectError) as error:
        raise _domain_transport_error(error) from error


def _journal_failed_attempt(
    binding: project.ProjectBinding,
    transaction: project.ProjectTransaction,
    attempt: _SendAttempt,
    error: TransportError,
) -> TransportError:
    if attempt.intent_record is None:
        return error
    event_now, observed_at = _utc_now()
    delivery_state = _delivery_state(error, attempt)
    try:
        outcome = journal.append_record(
            binding,
            event_type="transport.not_accepted",
            attributes={
                "intent_record_id": attempt.intent_record["record_id"],
                "participant_id": attempt.participant_id,
                "message_id": attempt.message_id,
                "transport": attempt.transport,
                "route_address": attempt.route_address,
                "delivery_state": delivery_state,
                "error_code": error.code,
                "observed_at": observed_at,
            },
            now=event_now,
            transaction=transaction,
        )
    except project.ProjectError as journal_error:
        raise TransportError(
            "transport.outcome_unjournaled",
            "a send was attempted but its outcome could not be journaled; inspect "
            "the project journal and do not retry automatically",
            audit={"intent_record": _record_summary(attempt.intent_record)},
        ) from journal_error
    error.audit = {
        "delivery_state": delivery_state,
        "intent_record": _record_summary(attempt.intent_record),
        "outcome_record": _record_summary(outcome),
    }
    return error


def _post_attempt_lock_failure(
    attempt: _SendAttempt,
    *,
    accepted: bool,
    result: dict[str, Any] | None = None,
    original_error: TransportError | None = None,
) -> TransportError:
    """Preserve a bounded do-not-retry verdict when outcome journaling is blocked."""

    audit: dict[str, Any] = {
        "delivery_state": "accepted" if accepted else "unknown",
        "intent_record": (
            _record_summary(attempt.intent_record)
            if attempt.intent_record is not None
            else None
        ),
    }
    if result is not None:
        audit["transport_receipt_id"] = _transport_receipt_identifier(result)
    if original_error is not None:
        audit["transport_error_code"] = original_error.code
    if accepted:
        return TransportError(
            "transport.acceptance_unjournaled",
            "transport acceptance is known but the project outcome could not be "
            "journaled; inspect the intent and do not retry automatically",
            audit=audit,
        )
    return TransportError(
        "transport.outcome_unjournaled",
        "a send was attempted but its outcome could not be journaled; inspect the "
        "intent and do not retry automatically",
        audit=audit,
    )


def _transport_receipt_identifier(result: dict[str, Any]) -> Any:
    receipt_identifier = result.get("transport_message_id")
    receipt = result.get("transport_receipt")
    if receipt_identifier is None and isinstance(receipt, dict):
        return receipt.get("queue_id")
    return receipt_identifier


def _require_complete_attempt(
    attempt: _SendAttempt,
) -> tuple[dict[str, Any], state.LifecyclePlan]:
    if attempt.intent_record is None or attempt.lifecycle_plan is None:
        raise TransportError(
            "transport.audit_incomplete",
            "transport accepted a message without a complete outbound audit plan; "
            "do not retry automatically",
        )
    return attempt.intent_record, attempt.lifecycle_plan


def _settle_accepted_lifecycle(
    store: state.StateStore,
    transaction: project.ProjectTransaction,
    attempt: _SendAttempt,
    plan: state.LifecyclePlan,
) -> tuple[lifecycle.LifecycleEntry, state.ProjectionRefreshError | None]:
    projection_error = attempt.projection_error
    try:
        if attempt.lifecycle_committed:
            snapshot = store.snapshot(transaction=transaction)
            lifecycle_entry = snapshot.lifecycle.entries.get(
                plan.preview.root_message_id
            )
            if lifecycle_entry is None:
                raise cam1.CamUsageError(
                    "state.committed_root_missing",
                    "journaled outbound root is missing from lifecycle state",
                )
        else:
            lifecycle_entry = store.commit_lifecycle(
                plan,
                transaction=transaction,
                preserve_prepared_observation=True,
            )
    except state.ProjectionRefreshError as error:
        # The canonical event exists; only its disposable projection is stale.
        return plan.preview, error
    return lifecycle_entry, projection_error


def _acceptance_attributes(
    attempt: _SendAttempt,
    intent_record: dict[str, Any],
    result: dict[str, Any],
    receipt_identifier: Any,
    *,
    lifecycle_state_committed: bool,
    observed_at: str,
) -> dict[str, Any]:
    return {
        "intent_record_id": intent_record["record_id"],
        "participant_id": attempt.participant_id,
        "message_id": result["message_id"],
        "transport": attempt.transport,
        "route_address": attempt.route_address,
        "transport_receipt_id": receipt_identifier,
        "lifecycle_state_committed": lifecycle_state_committed,
        "observed_at": observed_at,
    }


def _accepted_state_incomplete_error(
    binding: project.ProjectBinding,
    transaction: project.ProjectTransaction,
    attempt: _SendAttempt,
    intent_record: dict[str, Any],
    plan: state.LifecyclePlan,
    result: dict[str, Any],
    receipt_identifier: Any,
) -> TransportError:
    event_now, observed_at = _utc_now()
    try:
        accepted_record = journal.append_record(
            binding,
            event_type="transport.accepted",
            exact_message=plan.exact_message,
            attributes=_acceptance_attributes(
                attempt,
                intent_record,
                result,
                receipt_identifier,
                lifecycle_state_committed=False,
                observed_at=observed_at,
            ),
            now=event_now,
            transaction=transaction,
        )
    except project.ProjectError:
        accepted_record = None
    return TransportError(
        "transport.accepted_state_incomplete",
        "transport accepted the message but canonical lifecycle state could not "
        "be committed; inspect the journal and do not retry automatically",
        audit={
            "intent_record": _record_summary(intent_record),
            "transport_receipt_id": receipt_identifier,
            "accepted_record": (
                _record_summary(accepted_record)
                if accepted_record is not None
                else None
            ),
        },
    )


def _journal_committed_acceptance(
    binding: project.ProjectBinding,
    transaction: project.ProjectTransaction,
    attempt: _SendAttempt,
    intent_record: dict[str, Any],
    result: dict[str, Any],
    receipt_identifier: Any,
    projection_error: state.ProjectionRefreshError | None,
) -> dict[str, Any]:
    event_now, observed_at = _utc_now()
    try:
        return journal.append_record(
            binding,
            event_type="transport.accepted",
            attributes=_acceptance_attributes(
                attempt,
                intent_record,
                result,
                receipt_identifier,
                lifecycle_state_committed=True,
                observed_at=observed_at,
            ),
            now=event_now,
            transaction=transaction,
        )
    except project.ProjectError as error:
        audit: dict[str, Any] = {
            "intent_record": _record_summary(intent_record),
            "lifecycle_state_committed": True,
        }
        if projection_error is not None:
            audit["lifecycle_record"] = {
                "record_id": projection_error.record_id,
                "sequence": projection_error.sequence,
            }
        raise TransportError(
            "transport.acceptance_unjournaled",
            "transport and lifecycle acceptance were recorded, but the separate "
            "transport receipt record failed; do not retry automatically",
            audit=audit,
        ) from error


def _accepted_result(
    result: dict[str, Any],
    intent_record: dict[str, Any],
    accepted_record: dict[str, Any],
    lifecycle_entry: lifecycle.LifecycleEntry,
    projection_error: state.ProjectionRefreshError | None,
) -> dict[str, Any]:
    result["journal"] = {
        "intent_record": _record_summary(intent_record),
        "accepted_record": _record_summary(accepted_record),
    }
    result["lifecycle"] = lifecycle_entry.as_dict()
    if projection_error is not None:
        result["state_projection"] = {
            "current": False,
            "journal_record_id": projection_error.record_id,
            "journal_sequence": projection_error.sequence,
            "action": "run cam1_project.py state rebuild; do not resend",
        }
    return result


def _finalize_accepted_attempt(
    binding: project.ProjectBinding,
    store: state.StateStore,
    transaction: project.ProjectTransaction,
    attempt: _SendAttempt,
    result: dict[str, Any],
) -> dict[str, Any]:
    intent_record, plan = _require_complete_attempt(attempt)
    receipt_identifier = _transport_receipt_identifier(result)
    try:
        lifecycle_entry, projection_error = _settle_accepted_lifecycle(
            store, transaction, attempt, plan
        )
    except (cam1.CamUsageError, cam1.CamValidationError, project.ProjectError) as error:
        raise _accepted_state_incomplete_error(
            binding,
            transaction,
            attempt,
            intent_record,
            plan,
            result,
            receipt_identifier,
        ) from error
    accepted_record = _journal_committed_acceptance(
        binding,
        transaction,
        attempt,
        intent_record,
        result,
        receipt_identifier,
        projection_error,
    )
    return _accepted_result(
        result,
        intent_record,
        accepted_record,
        lifecycle_entry,
        projection_error,
    )


async def preflight_project_claude(
    binding: project.ProjectBinding,
    *,
    claude_bin: str,
    participant_selector: str,
    session_id_guard: str | None,
    target_guard: str | None,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Discover and journal one roster-bound Claude route without sending."""

    store = state.StateStore(binding)
    with project.project_transaction(binding) as transaction:
        participant = _require_bound_participant(
            store,
            participant_selector,
            vendor="claude-code",
            transaction=transaction,
        )
        assert participant.binding is not None
        _require_session_guard(
            session_id_guard,
            participant.binding.session_id,
            label="session_id",
        )
        bound_session_id = participant.binding.session_id
        bound_generation = participant.binding.generation
        _require_complete_claude_binding(participant)
        _require_approved_product_executable(participant, claude_bin)

    result = await _preflight_claude_session(
        claude_bin=claude_bin,
        session_id=bound_session_id,
        target=target_guard,
        timeout_seconds=timeout_seconds,
    )
    identity = result["identity"]
    route_data = result["route"]
    route = routing.ClaudeRoute(
        session=routing.AgentViewSession(
            session_id=identity["session_id"],
            agent_view_id=identity["agent_view_id"],
            product_name=identity["product_name"],
            cwd=identity["cwd"],
            kind=identity["kind"],
            state=identity["state"],
            started_at_ms=identity["started_at_ms"],
        ),
        peer=routing.Peer(
            name=route_data["list_agents_name"],
            ref=route_data["list_agents_ref"],
            kind=route_data["kind"],
            state=route_data["state"],
            details=(),
            local=True,
            addressable=True,
        ),
    )
    session_context = _require_project_session_cwd(binding, route.session)
    with project.project_transaction(binding) as transaction:
        participant = _require_bound_participant(
            store,
            participant_selector,
            vendor="claude-code",
            transaction=transaction,
        )
        assert participant.binding is not None
        if (
            participant.binding.session_id != bound_session_id
            or participant.binding.generation != bound_generation
        ):
            raise TransportError(
                "claude.binding_changed",
                "participant binding changed during Claude route discovery",
            )
        _require_approved_product_executable(participant, claude_bin)
        _require_bound_claude_metadata(participant, route.session)
        event_now, observed_at = _utc_now()
        try:
            observed = store.participant_observe_route(
                participant.participant_id,
                transport="claude_send_message",
                address=route.peer.qualified_address,
                source="claude_agent_view_and_list_agents",
                observed_at=observed_at,
                agent_view_id=route.session.agent_view_id,
                list_agents_name=route.peer.name,
                list_agents_ref=route.peer.ref,
                product_state=route.peer.state,
                agent_view_kind=route.session.kind,
                agent_view_started_at_ms=route.session.started_at_ms,
                session_git_top_level=str(session_context.top_level),
                session_git_common_dir=str(session_context.common_dir),
                tool_correlated=True,
                now=event_now,
                transaction=transaction,
            )
        except (cam1.CamUsageError, project.ProjectError) as error:
            raise _domain_transport_error(error) from error
        assert observed.route is not None
        result["participant"] = {
            "participant_id": observed.participant_id,
            "common_name": observed.common_name,
            "display_name": observed.display_name,
            "route_status": observed.route.status.value,
        }
        result["operator_correlation_required"] = False
        result["operator_correlation_subject"] = None
        result["operator_identity_confirmation_required"] = False
        result["transient_route_confirmation_required"] = False
        return result


async def send_project_claude(
    binding: project.ProjectBinding,
    *,
    claude_bin: str,
    participant_selector: str,
    session_id_guard: str | None,
    target_guard: str | None,
    envelope_path: str,
    against_path: str | None,
    renewal_of: str | None,
    retry_after_intent: str | None,
    summary: str | None,
    timeout_seconds: float,
    allow_dirty_validator: bool = False,
    expected_validation_profile_sha256: str | None = None,
) -> dict[str, Any]:
    """Journal, send, and commit one roster-bound Claude lifecycle message."""

    validation_profile, dirty_validator_override = _require_live_validation_profile(
        allow_dirty=allow_dirty_validator,
        expected_sha256=expected_validation_profile_sha256,
    )
    store = state.StateStore(binding)
    with project.project_transaction(binding) as transaction:
        participant = _require_bound_participant(
            store,
            participant_selector,
            vendor="claude-code",
            transaction=transaction,
        )
        assert participant.binding is not None
        _require_session_guard(
            session_id_guard,
            participant.binding.session_id,
            label="session_id",
        )
        participant_id = participant.participant_id
        bound_session_id = participant.binding.session_id
        bound_generation = participant.binding.generation
        _require_complete_claude_binding(participant)
        _require_approved_product_executable(participant, claude_bin)

    attempt = _SendAttempt(
        participant_id=participant_id,
        transport="claude_send_message",
        route_address="pending_fresh_discovery",
    )

    def before_send(
        validated: ValidatedEnvelope,
        route: routing.ClaudeRoute,
    ) -> None:
        session_context = _require_project_session_cwd(binding, route.session)
        with project.project_transaction(binding) as transaction:
            current = _require_bound_participant(
                store,
                participant_id,
                vendor="claude-code",
                transaction=transaction,
            )
            assert current.binding is not None
            if (
                current.binding.session_id != bound_session_id
                or current.binding.generation != bound_generation
                or route.session.session_id != bound_session_id
            ):
                raise TransportError(
                    "claude.binding_changed",
                    "fresh Claude discovery no longer matches the participant binding",
                )
            _require_approved_product_executable(current, claude_bin)
            _require_bound_claude_metadata(current, route.session)
            event_now, observed_at = _utc_now()
            try:
                observed = store.participant_observe_route(
                    current.participant_id,
                    transport="claude_send_message",
                    address=route.peer.qualified_address,
                    source="claude_agent_view_and_list_agents",
                    observed_at=observed_at,
                    agent_view_id=route.session.agent_view_id,
                    list_agents_name=route.peer.name,
                    list_agents_ref=route.peer.ref,
                    product_state=route.peer.state,
                    agent_view_kind=route.session.kind,
                    agent_view_started_at_ms=route.session.started_at_ms,
                    session_git_top_level=str(session_context.top_level),
                    session_git_common_dir=str(session_context.common_dir),
                    tool_correlated=True,
                    now=event_now,
                    transaction=transaction,
                )
                if observed.route is None or observed.route.status not in {
                    participants.RouteStatus.TOOL_CORRELATED,
                    participants.RouteStatus.OPERATOR_CORRELATED,
                }:
                    raise cam1.CamUsageError(
                        "roster.route_not_ready",
                        "fresh Claude route was not uniquely correlated to the bound "
                        "session identity",
                    )
                attempt.route_address = route.peer.qualified_address
                _prepare_and_journal_intent(
                    binding,
                    store,
                    transaction,
                    validated,
                    attempt,
                    recipient_participant=current,
                    renewal_of=renewal_of,
                    retry_after_intent=retry_after_intent,
                    validation_profile=validation_profile,
                    dirty_validator_override=dirty_validator_override,
                )
            except (
                cam1.CamUsageError,
                cam1.CamValidationError,
                project.ProjectError,
            ) as error:
                raise _domain_transport_error(error) from error

    def before_dispatch() -> None:
        attempt.dispatch_started = True

    try:
        result = await _send_to_claude(
            claude_bin=claude_bin,
            target=target_guard,
            session_id=bound_session_id,
            envelope_path=envelope_path,
            against_path=against_path,
            summary=summary,
            timeout_seconds=timeout_seconds,
            before_send=before_send,
            before_dispatch=before_dispatch,
        )
    except TransportError as error:
        if attempt.intent_record is not None:
            try:
                with project.project_transaction(binding) as transaction:
                    _journal_failed_attempt(
                        binding,
                        transaction,
                        attempt,
                        error,
                    )
            except project.ProjectError as lock_error:
                raise _post_attempt_lock_failure(
                    attempt,
                    accepted=False,
                    original_error=error,
                ) from lock_error
        raise
    try:
        with project.project_transaction(binding) as transaction:
            return _finalize_accepted_attempt(
                binding,
                store,
                transaction,
                attempt,
                result,
            )
    except project.ProjectError as lock_error:
        raise _post_attempt_lock_failure(
            attempt,
            accepted=True,
            result=result,
        ) from lock_error


def send_project_codex(
    binding: project.ProjectBinding,
    *,
    codex_bin: str,
    participant_selector: str,
    thread_guard: str | None,
    envelope_path: str,
    against_path: str | None,
    renewal_of: str | None,
    retry_after_intent: str | None,
    timeout_seconds: float,
    allow_dirty_validator: bool = False,
    expected_validation_profile_sha256: str | None = None,
) -> dict[str, Any]:
    """Journal, queue, and commit one roster-bound Codex lifecycle message."""

    validation_profile, dirty_validator_override = _require_live_validation_profile(
        allow_dirty=allow_dirty_validator,
        expected_sha256=expected_validation_profile_sha256,
    )
    store = state.StateStore(binding)
    with project.project_transaction(binding) as transaction:
        participant = _require_bound_participant(
            store,
            participant_selector,
            vendor="codex",
            transaction=transaction,
        )
        assert participant.binding is not None
        _require_session_guard(
            thread_guard,
            participant.binding.session_id,
            label="thread",
        )
        _require_approved_product_executable(participant, codex_bin)
        try:
            route = store.snapshot(
                transaction=transaction
            ).roster.require_correlated_route(participant.participant_id)
        except cam1.CamUsageError as error:
            raise _domain_transport_error(error) from error
        participant_id = participant.participant_id
        bound_session_id = participant.binding.session_id
        route_address = route.address

    attempt = _SendAttempt(
        participant_id=participant_id,
        transport="codex_queue",
        route_address=route_address,
    )

    def before_send(validated: ValidatedEnvelope) -> None:
        with project.project_transaction(binding) as transaction:
            current = _require_bound_participant(
                store,
                participant_id,
                vendor="codex",
                transaction=transaction,
            )
            assert current.binding is not None
            if current.binding.session_id != bound_session_id:
                raise TransportError(
                    "codex.session_changed",
                    "participant binding changed before Codex queue dispatch",
                )
            _require_approved_product_executable(current, codex_bin)
            _require_current_product_approval("codex", codex_bin)
            try:
                current_route = store.snapshot(
                    transaction=transaction
                ).roster.require_correlated_route(current.participant_id)
            except cam1.CamUsageError as error:
                raise _domain_transport_error(error) from error
            if current_route.address != route_address:
                raise TransportError(
                    "codex.route_changed",
                    "participant route changed before Codex queue dispatch",
                )
            _prepare_and_journal_intent(
                binding,
                store,
                transaction,
                validated,
                attempt,
                recipient_participant=current,
                renewal_of=renewal_of,
                retry_after_intent=retry_after_intent,
                validation_profile=validation_profile,
                dirty_validator_override=dirty_validator_override,
            )

    def before_dispatch() -> None:
        attempt.dispatch_started = True

    try:
        result = _send_to_codex_queue(
            codex_bin=codex_bin,
            thread=bound_session_id,
            envelope_path=envelope_path,
            against_path=against_path,
            timeout_seconds=timeout_seconds,
            before_send=before_send,
            before_dispatch=before_dispatch,
        )
    except TransportError as error:
        if attempt.intent_record is not None:
            try:
                with project.project_transaction(binding) as transaction:
                    _journal_failed_attempt(
                        binding,
                        transaction,
                        attempt,
                        error,
                    )
            except project.ProjectError as lock_error:
                raise _post_attempt_lock_failure(
                    attempt,
                    accepted=False,
                    original_error=error,
                ) from lock_error
        raise
    try:
        with project.project_transaction(binding) as transaction:
            return _finalize_accepted_attempt(
                binding,
                store,
                transaction,
                attempt,
                result,
            )
    except project.ProjectError as lock_error:
        raise _post_attempt_lock_failure(
            attempt,
            accepted=True,
            result=result,
        ) from lock_error


def _cli_api() -> _transport_cli.TransportCliApi:
    module = sys.modules[__name__]
    return _transport_cli.TransportCliApi(
        cam1=cam1,
        project=project,
        transport_error=TransportError,
        default_timeout_seconds=_native.DEFAULT_TIMEOUT_SECONDS,
        emit=module._emit,
        with_validation_profile=module._with_validation_profile,
        bounded_timeout=module._bounded_timeout,
        doctor=module.doctor,
        require_live_validation_profile=module._require_live_validation_profile,
        resolve_binary=module._resolve_binary,
        resolve_product_binary=module.resolve_product_binary,
        discover_product_executable=module.discover_product_executable,
        approve_product_executable=module.approve_product_executable,
        product_executable_status=module.product_executable_status,
        revoke_product_executable=module.revoke_product_executable,
        resolve_project=module._resolve_project,
        list_local_peers=module.list_local_peers,
        preflight_project_claude=module.preflight_project_claude,
        send_project_claude=module.send_project_claude,
        send_project_codex=module.send_project_codex,
    )


def _parser() -> Any:
    """Compatibility seam for callers that inspect the command parser."""

    return _transport_cli.build_parser(_cli_api())


def main(argv: Sequence[str] | None = None) -> int:
    product_approvals.begin_operation()
    return _transport_cli.main(argv, api=_cli_api())


if __name__ == "__main__":
    raise SystemExit(main())
