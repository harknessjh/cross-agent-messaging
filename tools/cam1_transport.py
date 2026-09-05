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

from collections.abc import Sequence
from typing import Any

from tools import cam1
from tools import cam1_transport_native as _native
from tools import cam1_transport_products as _products
from tools.cam1lib import (
    participants,
    project,
    routing,
    state,
)
from tools.cam1lib import transport_audit as _audit
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

LEGACY_PRODUCT_APPROVAL_PROFILES = _products.LEGACY_PRODUCT_APPROVAL_PROFILES


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


_SendAttempt = _audit._SendAttempt


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


_require_approved_product_executable = _products._require_approved_product_executable
resolve_product_binary = _products.resolve_product_binary
_legacy_product_confirmation = _products._legacy_product_confirmation
discover_product_executable = _products.discover_product_executable
approve_product_executable = _products.approve_product_executable
product_executable_status = _products.product_executable_status
product_recovery_status = _products.product_recovery_status
recover_product_partial_tail = _products.recover_product_partial_tail
revoke_product_executable = _products.revoke_product_executable
_require_current_product_approval = _products._require_current_product_approval

# Compatibility import surfaces retained for callers that used the facade.
product_approvals = _products.product_approvals
product_executables = _products.product_executables


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


_require_safe_retry = _audit._require_safe_retry
_intent_attributes = _audit._intent_attributes
_require_roster_endpoints = _audit._require_roster_endpoints
_require_reply_slot_available = _audit._require_reply_slot_available
_prepare_and_journal_intent = _audit._prepare_and_journal_intent
_journal_failed_attempt = _audit._journal_failed_attempt
_post_attempt_lock_failure = _audit._post_attempt_lock_failure
_transport_receipt_identifier = _audit._transport_receipt_identifier
_require_complete_attempt = _audit._require_complete_attempt
_settle_accepted_lifecycle = _audit._settle_accepted_lifecycle
_acceptance_attributes = _audit._acceptance_attributes
_accepted_state_incomplete_error = _audit._accepted_state_incomplete_error
_journal_committed_acceptance = _audit._journal_committed_acceptance
_accepted_result = _audit._accepted_result
_finalize_accepted_attempt = _audit._finalize_accepted_attempt
_transport_outcomes = _audit._transport_outcomes

# Compatibility import surfaces retained for callers that used the facade.
causal = _audit.causal
journal = _audit.journal
lifecycle = _audit.lifecycle


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
    continues_message: str | None = None,
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
                    continues_message=continues_message,
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
    continues_message: str | None = None,
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
                continues_message=continues_message,
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
        product_recovery_status=module.product_recovery_status,
        recover_product_partial_tail=module.recover_product_partial_tail,
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
    _products.begin_operation()
    return _transport_cli.main(argv, api=_cli_api())


if __name__ == "__main__":
    raise SystemExit(main())
