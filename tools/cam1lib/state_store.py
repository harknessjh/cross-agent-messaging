# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Journal-backed state mutation and lifecycle planning APIs."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any, cast

from .journal import (
    _verified_records_for_transaction,
    append_record,
    decode_exact_message,
)
from .lifecycle import ROOT_TYPES, LifecycleEntry, LifecycleState
from .participants import Participant
from .project import (
    ProjectBinding,
    ProjectError,
    ProjectTransaction,
    project_transaction,
    require_project_transaction,
)
from .protocol import REPLY_TYPES, CamUsageError, parse_exact_bytes
from .state_projection import (
    LIFECYCLE_EXPIRED_UNCONFIRMED,
    LIFECYCLE_REPLY_APPLIED,
    LIFECYCLE_ROOT_REGISTERED,
    PARTICIPANT_ADDED,
    PARTICIPANT_BOUND,
    PARTICIPANT_INVALIDATED,
    PARTICIPANT_RETIRED,
    PARTICIPANT_ROUTE_CONFIRMED,
    PARTICIPANT_ROUTE_OBSERVED,
    LifecyclePlan,
    ProjectionRefreshError,
    StateSnapshot,
    _apply_event,
    _cache_snapshot,
    _canonical_uuid,
    _event_time,
    _replay_locked,
    _required_text,
    _select_participant,
    _transaction_snapshot_locked,
    _uuid_values_equal,
    _validate_cancel_against_request,
    _validate_message,
    _validation_time,
    _write_projections,
    require_plan_freshness,
)


@contextmanager
def _transaction(
    project: ProjectBinding,
    supplied: ProjectTransaction | None,
) -> Iterator[ProjectTransaction]:
    if supplied is not None:
        require_project_transaction(project, supplied)
        yield supplied
        return
    with project_transaction(project) as acquired:
        yield acquired


class StateStore:
    """Mutate journal-backed projections under one project-wide transaction."""

    def __init__(self, project: ProjectBinding):
        self.project = project

    def rebuild(
        self, *, transaction: ProjectTransaction | None = None
    ) -> StateSnapshot:
        with _transaction(self.project, transaction) as active:
            snapshot = _replay_locked(self.project, active)
            _write_projections(self.project, snapshot)
            return snapshot

    def snapshot(
        self, *, transaction: ProjectTransaction | None = None
    ) -> StateSnapshot:
        """Replay current canonical state without refreshing disposable files."""

        with _transaction(self.project, transaction) as active:
            return _replay_locked(self.project, active)

    def preserved_message(
        self,
        message_id: str,
        *,
        transaction: ProjectTransaction | None = None,
    ) -> bytes | None:
        """Return the exact journaled bytes for one lifecycle message, if known."""

        canonical = _canonical_uuid(message_id, field_name="message_id")
        with _transaction(self.project, transaction) as active:
            return _transaction_snapshot_locked(
                self.project, active
            )._message_bytes.get(canonical)

    def _mutate(
        self,
        *,
        event_type: str,
        attributes: Mapping[str, Any],
        exact_message: bytes | None = None,
        now: dt.datetime | None = None,
        transaction: ProjectTransaction | None = None,
    ) -> Participant | LifecycleEntry:
        with _transaction(self.project, transaction) as active:
            snapshot = _replay_locked(self.project, active)
            return self._commit_locked(
                snapshot,
                active,
                event_type=event_type,
                attributes=attributes,
                exact_message=exact_message,
                now=now,
            )

    def _commit_locked(
        self,
        snapshot: StateSnapshot,
        transaction: ProjectTransaction,
        *,
        event_type: str,
        attributes: Mapping[str, Any],
        exact_message: bytes | None,
        now: dt.datetime | None,
    ) -> Participant | LifecycleEntry:
        result = _apply_event(
            snapshot,
            event_type=event_type,
            attributes=attributes,
            exact_message=exact_message,
        )
        record = append_record(
            self.project,
            event_type=event_type,
            exact_message=exact_message,
            attributes=attributes,
            now=now,
            transaction=transaction,
        )
        snapshot.journal_sequence = cast(int, record["sequence"])
        snapshot.journal_record_sha256 = cast(str, record["record_sha256"])
        _cache_snapshot(self.project, transaction, snapshot)
        try:
            _write_projections(self.project, snapshot)
        except ProjectError:
            raise ProjectionRefreshError(
                record_id=cast(str, record["record_id"]),
                sequence=cast(int, record["sequence"]),
            ) from None
        return result

    def participant_add(
        self,
        *,
        common_name: str,
        display_name: str,
        role: str,
        vendor: str,
        participant_id: str | None = None,
        now: dt.datetime | None = None,
        transaction: ProjectTransaction | None = None,
    ) -> Participant:
        identifier = participant_id or str(uuid.uuid4())
        result = self._mutate(
            event_type=PARTICIPANT_ADDED,
            attributes={
                "participant_id": identifier,
                "common_name": common_name,
                "display_name": display_name,
                "role": role,
                "vendor": vendor,
            },
            now=now,
            transaction=transaction,
        )
        return cast(Participant, result)

    def participant_bind(
        self,
        selector: str,
        *,
        session_id: str,
        session_label: str,
        session_kind: str | None,
        operator_reference: str,
        bound_at: str,
        now: dt.datetime | None = None,
        transaction: ProjectTransaction | None = None,
    ) -> Participant:
        with _transaction(self.project, transaction) as active:
            snapshot = _replay_locked(self.project, active)
            current = _select_participant(snapshot.roster, selector)
            return cast(
                Participant,
                self._commit_locked(
                    snapshot,
                    active,
                    event_type=PARTICIPANT_BOUND,
                    attributes={
                        "participant_id": current.participant_id,
                        "session_id": session_id,
                        "session_label": session_label,
                        "session_kind": session_kind,
                        "operator_reference": operator_reference,
                        "bound_at": bound_at,
                    },
                    exact_message=None,
                    now=now,
                ),
            )

    def participant_observe_route(
        self,
        selector: str,
        *,
        transport: str,
        address: str,
        source: str,
        observed_at: str,
        agent_view_id: str | None = None,
        list_agents_name: str | None = None,
        list_agents_ref: str | None = None,
        product_state: str | None = None,
        agent_view_kind: str | None = None,
        agent_view_started_at_ms: int | None = None,
        session_git_top_level: str | None = None,
        session_git_common_dir: str | None = None,
        now: dt.datetime | None = None,
        transaction: ProjectTransaction | None = None,
    ) -> Participant:
        with _transaction(self.project, transaction) as active:
            snapshot = _replay_locked(self.project, active)
            current = _select_participant(snapshot.roster, selector)
            return cast(
                Participant,
                self._commit_locked(
                    snapshot,
                    active,
                    event_type=PARTICIPANT_ROUTE_OBSERVED,
                    attributes={
                        "participant_id": current.participant_id,
                        "transport": transport,
                        "address": address,
                        "source": source,
                        "observed_at": observed_at,
                        "agent_view_id": agent_view_id,
                        "list_agents_name": list_agents_name,
                        "list_agents_ref": list_agents_ref,
                        "product_state": product_state,
                        "agent_view_kind": agent_view_kind,
                        "agent_view_started_at_ms": agent_view_started_at_ms,
                        "session_git_top_level": session_git_top_level,
                        "session_git_common_dir": session_git_common_dir,
                    },
                    exact_message=None,
                    now=now,
                ),
            )

    def participant_confirm_route(
        self,
        selector: str,
        *,
        expected_address: str,
        operator_reference: str,
        confirmed_at: str,
        now: dt.datetime | None = None,
        transaction: ProjectTransaction | None = None,
    ) -> Participant:
        with _transaction(self.project, transaction) as active:
            snapshot = _replay_locked(self.project, active)
            current = _select_participant(snapshot.roster, selector)
            return cast(
                Participant,
                self._commit_locked(
                    snapshot,
                    active,
                    event_type=PARTICIPANT_ROUTE_CONFIRMED,
                    attributes={
                        "participant_id": current.participant_id,
                        "expected_address": expected_address,
                        "operator_reference": operator_reference,
                        "confirmed_at": confirmed_at,
                    },
                    exact_message=None,
                    now=now,
                ),
            )

    def participant_invalidate(
        self,
        selector: str,
        *,
        reason: str,
        now: dt.datetime | None = None,
        transaction: ProjectTransaction | None = None,
    ) -> Participant:
        return self._participant_status_event(
            selector,
            event_type=PARTICIPANT_INVALIDATED,
            reason=reason,
            now=now,
            transaction=transaction,
        )

    def participant_retire(
        self,
        selector: str,
        *,
        reason: str,
        now: dt.datetime | None = None,
        transaction: ProjectTransaction | None = None,
    ) -> Participant:
        return self._participant_status_event(
            selector,
            event_type=PARTICIPANT_RETIRED,
            reason=reason,
            now=now,
            transaction=transaction,
        )

    def _participant_status_event(
        self,
        selector: str,
        *,
        event_type: str,
        reason: str,
        now: dt.datetime | None,
        transaction: ProjectTransaction | None,
    ) -> Participant:
        with _transaction(self.project, transaction) as active:
            snapshot = _replay_locked(self.project, active)
            current = _select_participant(snapshot.roster, selector)
            return cast(
                Participant,
                self._commit_locked(
                    snapshot,
                    active,
                    event_type=event_type,
                    attributes={
                        "participant_id": current.participant_id,
                        "reason": reason,
                    },
                    exact_message=None,
                    now=now,
                ),
            )

    def lifecycle_root(
        self,
        exact_message: bytes,
        *,
        renewal_of: str | None = None,
        now: dt.datetime | None = None,
        transaction: ProjectTransaction | None = None,
    ) -> LifecycleEntry:
        with _transaction(self.project, transaction) as active:
            plan = self.prepare_lifecycle(
                exact_message,
                renewal_of=renewal_of,
                now=now,
                transaction=active,
            )
            return self.commit_lifecycle(plan, transaction=active, now=now)

    def lifecycle_reply(
        self,
        exact_message: bytes,
        *,
        now: dt.datetime | None = None,
        transaction: ProjectTransaction | None = None,
    ) -> LifecycleEntry:
        with _transaction(self.project, transaction) as active:
            plan = self.prepare_lifecycle(
                exact_message,
                now=now,
                transaction=active,
            )
            if plan.event_type != LIFECYCLE_REPLY_APPLIED:
                raise CamUsageError(
                    "state.lifecycle_type",
                    "lifecycle_reply requires a reply envelope",
                )
            return self.commit_lifecycle(plan, transaction=active, now=now)

    def prepare_lifecycle(
        self,
        exact_message: bytes,
        *,
        renewal_of: str | None = None,
        preserved_against: bytes | None = None,
        require_preserved_against: bool = False,
        now: dt.datetime | None = None,
        transaction: ProjectTransaction,
    ) -> LifecyclePlan:
        """Validate one lifecycle candidate against the complete journal history."""

        require_project_transaction(self.project, transaction)
        event_now, observed_at = _event_time(now)
        envelope = parse_exact_bytes(exact_message)
        message_type = envelope.get("type")
        if message_type in ROOT_TYPES:
            event_type = LIFECYCLE_ROOT_REGISTERED
            attributes: dict[str, Any] = {
                "root_message_id": _canonical_uuid(
                    envelope.get("message_id"), field_name="message_id"
                ),
                "root_type": message_type,
                "renewal_of": (
                    _canonical_uuid(renewal_of, field_name="renewal_of")
                    if renewal_of is not None
                    else None
                ),
                "observed_at": observed_at,
            }
            correlated_root = message_type == "cancel"
        elif message_type in REPLY_TYPES:
            if renewal_of is not None:
                raise CamUsageError(
                    "state.renewal_type",
                    "renewal metadata is valid only for lifecycle roots",
                )
            event_type = LIFECYCLE_REPLY_APPLIED
            attributes = {
                "message_id": _canonical_uuid(
                    envelope.get("message_id"), field_name="message_id"
                ),
                "root_message_id": _canonical_uuid(
                    envelope.get("in_reply_to"), field_name="in_reply_to"
                ),
                "message_type": message_type,
                "observed_at": observed_at,
            }
            correlated_root = True
        else:
            raise CamUsageError(
                "state.lifecycle_type",
                "message type cannot participate in a lifecycle",
            )

        snapshot = _replay_locked(self.project, transaction)
        _validate_message(
            exact_message,
            observed_at=observed_at,
            allow_expired=message_type in ROOT_TYPES,
        )
        candidate_message_id = _canonical_uuid(
            envelope.get("message_id"), field_name="message_id"
        )
        duplicate = candidate_message_id in snapshot._message_bytes
        if correlated_root:
            root_id = _canonical_uuid(
                envelope.get("in_reply_to"), field_name="in_reply_to"
            )
            stored_root = snapshot._message_bytes.get(root_id)
            if stored_root is None:
                raise CamUsageError(
                    "state.root_missing",
                    "correlated root is not present in the project journal",
                )
            if require_preserved_against and preserved_against is None:
                raise CamUsageError(
                    "state.against_required",
                    "live correlated sends require the preserved root envelope",
                )
            if preserved_against is not None and preserved_against != stored_root:
                raise CamUsageError(
                    "state.against_mismatch",
                    "supplied root bytes do not equal the journaled root envelope",
                )
        elif preserved_against is not None:
            raise CamUsageError(
                "state.against_unexpected",
                "uncorrelated lifecycle roots do not accept preserved root bytes",
            )

        freshness_candidates = [
            _required_text(envelope, "expires_at"),
        ]
        if message_type in REPLY_TYPES:
            root_id = _canonical_uuid(
                envelope.get("in_reply_to"), field_name="in_reply_to"
            )
            prior = snapshot.lifecycle.reply_observation_basis(
                candidate_message_id,
                root_id,
            )
            receipt = envelope.get("receipt")
            status = receipt.get("status") if isinstance(receipt, dict) else None
            late_rejection = message_type == "ack" and status == "rejected"
            if (
                prior.state
                in {
                    LifecycleState.PENDING,
                    LifecycleState.HELD,
                    LifecycleState.EXPIRED_UNCONFIRMED,
                }
                and not late_rejection
            ):
                freshness_candidates.append(prior.root_expires_at)
        freshness_deadline = min(
            freshness_candidates,
            key=_validation_time,
        )

        preview = cast(
            LifecycleEntry,
            _apply_event(
                snapshot,
                event_type=event_type,
                attributes=attributes,
                exact_message=exact_message,
            ),
        )
        return LifecyclePlan(
            project_id=self.project.project_id,
            event_type=event_type,
            attributes=attributes,
            exact_message=exact_message,
            recorded_at=event_now,
            preview=preview,
            duplicate=duplicate,
            freshness_deadline=freshness_deadline,
        )

    def prepare_inbound_lifecycle(
        self,
        exact_message: bytes,
        *,
        renewal_of: str | None = None,
        now: dt.datetime | None = None,
        transaction: ProjectTransaction,
    ) -> LifecyclePlan:
        """Prepare inbound state, honoring prior accepted local reply delivery."""

        try:
            return self.prepare_lifecycle(
                exact_message,
                renewal_of=renewal_of,
                now=now,
                transaction=transaction,
            )
        except CamUsageError as error:
            if error.code != "lifecycle.root_expired_before_reply":
                raise
            accepted = self._prepare_accepted_outbound_reply(
                exact_message,
                now=now,
                transaction=transaction,
            )
            if accepted is None:
                raise
            return accepted

    def _prepare_accepted_outbound_reply(
        self,
        exact_message: bytes,
        *,
        now: dt.datetime | None,
        transaction: ProjectTransaction,
    ) -> LifecyclePlan | None:
        """Recognize a fresh callback for a reply this project already delivered."""

        require_project_transaction(self.project, transaction)
        event_now, observed_at = _event_time(now)
        envelope = parse_exact_bytes(exact_message)
        if envelope.get("type") not in REPLY_TYPES:
            return None
        message_id = _canonical_uuid(
            envelope.get("message_id"), field_name="message_id"
        )
        root_id = _canonical_uuid(envelope.get("in_reply_to"), field_name="in_reply_to")
        snapshot = _replay_locked(self.project, transaction)
        if snapshot._message_bytes.get(message_id) != exact_message:
            return None
        root_raw = snapshot._message_bytes.get(root_id)
        current = snapshot.lifecycle.entries.get(root_id)
        if root_raw is None or current is None:
            return None

        records = _verified_records_for_transaction(self.project, transaction)
        intent_ids = {
            record["record_id"]
            for record in records
            if record["event_type"] == "message.outbound.intent"
            and decode_exact_message(record) == exact_message
        }
        accepted_delivery = any(
            record["event_type"] == "transport.accepted"
            and isinstance(record.get("attributes"), dict)
            and record["attributes"].get("intent_record_id") in intent_ids
            and _uuid_values_equal(
                record["attributes"].get("message_id"),
                message_id,
            )
            and record["attributes"].get("lifecycle_state_committed") is True
            for record in records
        )
        if not accepted_delivery:
            return None

        _validate_message(
            exact_message,
            observed_at=observed_at,
            against_raw=root_raw,
        )
        return LifecyclePlan(
            project_id=self.project.project_id,
            event_type=LIFECYCLE_REPLY_APPLIED,
            attributes={
                "message_id": message_id,
                "root_message_id": root_id,
                "message_type": envelope["type"],
                "observed_at": observed_at,
            },
            exact_message=exact_message,
            recorded_at=event_now,
            preview=current,
            duplicate=True,
            freshness_deadline=_required_text(envelope, "expires_at"),
        )

    def commit_lifecycle(
        self,
        plan: LifecyclePlan,
        *,
        transaction: ProjectTransaction,
        now: dt.datetime | None = None,
        preserve_prepared_observation: bool = False,
    ) -> LifecycleEntry:
        """Commit a prepared lifecycle event while its project lock is held."""

        require_project_transaction(self.project, transaction)
        if plan.project_id != self.project.project_id:
            raise CamUsageError(
                "state.plan_project",
                "lifecycle plan belongs to another project",
            )
        if preserve_prepared_observation and now is not None:
            raise CamUsageError(
                "state.commit_time",
                "preserved observation cannot be combined with an override time",
            )
        snapshot = _replay_locked(self.project, transaction)
        if preserve_prepared_observation:
            observation_now = plan.recorded_at
            commit_now, _ = _event_time(None)
            attributes = dict(plan.attributes)
        else:
            commit_now, commit_observed_at = _event_time(now)
            observation_now = commit_now
            attributes = dict(plan.attributes)
            attributes["observed_at"] = commit_observed_at
        if plan.preview.state != LifecycleState.EXPIRED_UNCONFIRMED:
            require_plan_freshness(plan, now=observation_now)
        message_id = _canonical_uuid(
            plan.attributes.get("message_id", plan.attributes["root_message_id"]),
            field_name="message_id",
        )
        current_exact = snapshot._message_bytes.get(message_id)
        if current_exact is not None:
            if current_exact != plan.exact_message:
                raise CamUsageError(
                    "state.duplicate_changed",
                    "duplicate lifecycle message changed before commit",
                )
            root_id = _canonical_uuid(
                plan.attributes["root_message_id"], field_name="root_message_id"
            )
            current = snapshot.lifecycle.entries.get(root_id)
            if current is None:
                raise CamUsageError(
                    "state.duplicate_missing",
                    "duplicate lifecycle root is no longer present",
                )
            return current
        return cast(
            LifecycleEntry,
            self._commit_locked(
                snapshot,
                transaction,
                event_type=plan.event_type,
                attributes=attributes,
                exact_message=plan.exact_message,
                now=commit_now,
            ),
        )

    def lifecycle_expired(
        self,
        root_message_id: str,
        *,
        now: dt.datetime | None = None,
        transaction: ProjectTransaction | None = None,
    ) -> LifecycleEntry:
        event_now, observed_at = _event_time(now)
        result = self._mutate(
            event_type=LIFECYCLE_EXPIRED_UNCONFIRMED,
            attributes={
                "root_message_id": root_message_id,
                "observed_at": observed_at,
            },
            now=event_now,
            transaction=transaction,
        )
        return cast(LifecycleEntry, result)


def rebuild_state(
    project: ProjectBinding,
    *,
    transaction: ProjectTransaction | None = None,
) -> StateSnapshot:
    """Rebuild both disposable projections from the sole source of truth."""

    return StateStore(project).rebuild(transaction=transaction)


def validate_cancel_exact_bytes(
    cancel_raw: bytes,
    request_raw: bytes,
    *,
    now: dt.datetime | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate a cancel against the exact preserved request outside a project."""

    event_now, observed_at = _event_time(now)
    del event_now
    cancel = _validate_message(cancel_raw, observed_at=observed_at)
    request = _validate_message(
        request_raw,
        observed_at=observed_at,
        allow_expired=True,
    )
    if cancel.get("type") != "cancel":
        raise CamUsageError(
            "lifecycle.cancel_type",
            "candidate is not a cancel envelope",
        )
    _validate_cancel_against_request(cancel, request)
    return cancel, request
