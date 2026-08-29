# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Project binding and required append-only journal commands for CAM/1."""

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
            [sys.executable, "-I", "-B", _entry, "cam1_project", *sys.argv[1:]],
        )
    except OSError:
        sys.stderr.write(
            '{"error":{"code":"bootstrap.isolation_failed",'
            '"detail":"could not enter isolated Python mode"},"ok":false}\n'
        )
        raise SystemExit(2) from None

import argparse
import datetime as dt
import json
import uuid
from pathlib import Path
from typing import Any

from tools.cam1lib import journal, lifecycle, participants, profile, project, state
from tools.cam1lib.protocol import CamUsageError, CamValidationError, parse_exact_bytes
from tools.cam1lib.state import StateStore


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        _emit(
            {
                "ok": False,
                "error": {"code": "argument.invalid", "detail": message[:300]},
            },
            stream=sys.stderr,
        )
        raise SystemExit(2)


def _emit(payload: dict[str, Any], *, stream: Any = sys.stdout) -> None:
    stream.write(json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n")


def _parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(
        description="Manage one Git-bound CAM project and its required local journal."
    )
    parser.add_argument("--project-root", default=".")
    parser.add_argument(
        "--state-root",
        help="absolute journal root override; defaults to the account home at ~/CAM/Journals",
    )
    parser.add_argument("--git-bin", default=project.DEFAULT_GIT_BIN)
    domains = parser.add_subparsers(dest="domain", required=True)

    project_parser = domains.add_parser("project")
    project_commands = project_parser.add_subparsers(
        dest="project_command", required=True
    )
    project_commands.add_parser("init", help="create a new binding without overwriting")
    project_commands.add_parser(
        "status", help="validate and display an existing binding"
    )

    journal_parser = domains.add_parser("journal")
    journal_commands = journal_parser.add_subparsers(
        dest="journal_command", required=True
    )
    append_parser = journal_commands.add_parser(
        "append", help="append one canonical record after verifying the full chain"
    )
    append_parser.add_argument("--event-type", required=True)
    append_parser.add_argument(
        "--message",
        help="optional owner-only regular file whose exact bytes will be preserved",
    )
    append_parser.add_argument(
        "--attributes-file",
        help="optional owner-only JSON object containing bounded event attributes",
    )
    journal_commands.add_parser("verify", help="verify the complete journal")
    journal_commands.add_parser(
        "recovery-status",
        help="inspect an incomplete EOF record without modifying the journal",
    )
    recover_parser = journal_commands.add_parser(
        "recover-partial-tail",
        help="archive one incomplete EOF record and append a recovery event",
    )
    recover_parser.add_argument("--expected-journal-sha256", required=True)
    recover_parser.add_argument("--confirm-project-id", required=True)
    recover_parser.add_argument("--reason", required=True)
    recover_parser.add_argument("--operator-reference", required=True)
    tail_parser = journal_commands.add_parser(
        "tail", help="show a bounded tail with messages and attributes redacted"
    )
    tail_parser.add_argument("--limit", type=int, default=20)
    tail_parser.add_argument(
        "--show-content",
        action="store_true",
        help="explicitly include decoded exact message content and attributes",
    )

    participant_parser = domains.add_parser("participant")
    participant_commands = participant_parser.add_subparsers(
        dest="participant_command", required=True
    )
    participant_add = participant_commands.add_parser(
        "add", help="add one project-local participant identity"
    )
    participant_add.add_argument("--common-name", required=True)
    participant_add.add_argument("--display-name", required=True)
    participant_add.add_argument("--role", required=True)
    participant_add.add_argument(
        "--vendor", choices=("codex", "claude-code"), required=True
    )
    participant_add.add_argument("--participant-id")

    participant_bind = participant_commands.add_parser(
        "bind", help="bind a participant to an operator-confirmed product session"
    )
    participant_bind.add_argument("--participant", required=True)
    participant_bind.add_argument("--session-id", required=True)
    participant_bind.add_argument("--session-label", required=True)
    participant_bind.add_argument("--session-kind")
    participant_bind.add_argument("--operator-reference", required=True)

    participant_confirm = participant_commands.add_parser(
        "confirm-route", help="confirm the currently observed route"
    )
    participant_confirm.add_argument("--participant", required=True)
    participant_confirm.add_argument("--expected-address", required=True)
    participant_confirm.add_argument("--operator-reference", required=True)

    participant_list = participant_commands.add_parser(
        "list", help="list the project roster with routing identifiers redacted"
    )
    participant_list.add_argument(
        "--show-identifiers",
        action="store_true",
        help="explicitly reveal session and route identifiers",
    )

    for command, help_text in (
        ("invalidate", "mark a participant binding and route stale"),
        ("retire", "retire a participant without deleting its history"),
    ):
        status_parser = participant_commands.add_parser(command, help=help_text)
        status_parser.add_argument("--participant", required=True)
        status_parser.add_argument("--reason", required=True)

    state_parser = domains.add_parser("state")
    state_commands = state_parser.add_subparsers(dest="state_command", required=True)
    state_commands.add_parser(
        "status", help="replay and summarize canonical journal-backed state"
    )
    state_commands.add_parser(
        "rebuild", help="rebuild disposable state projections from the journal"
    )

    message_parser = domains.add_parser("message")
    message_commands = message_parser.add_subparsers(
        dest="message_command", required=True
    )
    ingest_parser = message_commands.add_parser(
        "ingest",
        help="retain then validate one owner-only inbound envelope",
    )
    ingest_source = ingest_parser.add_mutually_exclusive_group(required=True)
    ingest_source.add_argument(
        "--message",
        help="owner-only regular file containing the exact inbound bytes",
    )
    ingest_source.add_argument(
        "--stdin",
        action="store_true",
        help="read bounded exact inbound bytes from binary stdin",
    )
    ingest_parser.add_argument(
        "--capture-to",
        help="absolute new owner-only file required with --stdin",
    )
    ingest_parser.add_argument(
        "--as-participant",
        required=True,
        help="active local roster participant that received these exact bytes",
    )
    ingest_parser.add_argument(
        "--renewal-of",
        help="prior root message UUID when this root is an explicit renewal",
    )
    return parser


def _validate_arguments(
    parser: argparse.ArgumentParser, args: argparse.Namespace
) -> None:
    if args.domain != "message" or args.message_command != "ingest":
        return
    if args.stdin:
        if args.capture_to is None:
            parser.error("--capture-to is required with --stdin")
        if not Path(args.capture_to).is_absolute():
            parser.error("--capture-to must be an absolute new file path")
        return
    if args.capture_to is not None:
        parser.error("--capture-to is valid only with --stdin")


def _read_exact_stdin(*, max_bytes: int) -> bytes:
    """Read one bounded binary stdin payload without text transcoding."""

    stream = getattr(sys.stdin, "buffer", None)
    if stream is None:
        raise project.ProjectError(
            "message.stdin_binary", "binary stdin is unavailable"
        )
    raw = stream.read(max_bytes + 1)
    if not raw:
        raise project.ProjectError(
            "message.stdin_empty", "stdin did not contain an inbound envelope"
        )
    if len(raw) > max_bytes:
        raise project.ProjectError(
            "message.stdin_size",
            f"stdin exceeds {max_bytes} bytes",
        )
    return raw


def _resolve(args: argparse.Namespace) -> project.ProjectBinding:
    return project.resolve_project(
        args.project_root,
        state_root=args.state_root,
        git_bin=args.git_bin,
    )


def _record_summary(record: dict[str, Any]) -> dict[str, Any]:
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


def _record_with_content(record: dict[str, Any]) -> dict[str, Any]:
    shown = dict(record)
    message = record.get("message")
    if not isinstance(message, dict):
        return shown
    raw = journal.decode_exact_message(record)
    rendered = dict(message)
    try:
        rendered["decoded_utf8"] = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        rendered["decoded_utf8"] = None
    shown["message"] = rendered
    return shown


def _utc_now() -> tuple[dt.datetime, str]:
    observed = dt.datetime.now(dt.UTC)
    timespec = "microseconds" if observed.microsecond else "seconds"
    return observed, observed.isoformat(timespec=timespec).replace("+00:00", "Z")


def _state_summary(snapshot: state.StateSnapshot) -> dict[str, Any]:
    lifecycle_states: dict[str, int] = {}
    for entry in snapshot.lifecycle.entries.values():
        lifecycle_states[entry.state.value] = (
            lifecycle_states.get(entry.state.value, 0) + 1
        )
    return {
        "journal_sequence": snapshot.journal_sequence,
        "journal_record_sha256": snapshot.journal_record_sha256,
        "participant_count": len(snapshot.roster.participants),
        "lifecycle_count": len(snapshot.lifecycle.entries),
        "lifecycle_states": dict(sorted(lifecycle_states.items())),
    }


def _rejection_codes(
    error: CamUsageError | CamValidationError | state.StateError,
) -> tuple[str, list[str]]:
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
) -> tuple[state.Participant, state.Participant]:
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
    return local, sender_matches[0]


def _prior_inbound_validation(
    binding: project.ProjectBinding,
    *,
    message_id: str,
    recipient_participant_id: str,
) -> dict[str, Any] | None:
    """Return the prior recipient-specific validation for one exact message."""

    for record in reversed(journal.replay_records(binding)):
        if record["event_type"] != "message.inbound.validated":
            continue
        attributes = record.get("attributes")
        if (
            isinstance(attributes, dict)
            and _uuid_values_equal(attributes.get("message_id"), message_id)
            and attributes.get("recipient_participant_id") == recipient_participant_id
        ):
            return record
    return None


def _record_inbound_duplicate(
    binding: project.ProjectBinding,
    transaction: project.ProjectTransaction,
    *,
    observed_record: dict[str, Any],
    message_id: str,
    prior_validation: dict[str, Any],
    local_participant: state.Participant,
    sender_participant: state.Participant,
    lifecycle_entry: lifecycle.LifecycleEntry,
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
            "sender_participant_id": sender_participant.participant_id,
            "recipient_participant_id": local_participant.participant_id,
            "authorization_evaluated": False,
            "action_authorized": False,
            "validation_profile": validation_profile,
            "observed_at": duplicate_at,
        },
        now=duplicate_now,
        transaction=transaction,
    )
    return 0, {
        "ok": True,
        "status": "duplicate",
        "duplicate": True,
        "authorization_evaluated": False,
        "action_authorized": False,
        "validation_profile": validation_profile,
        "observed_record": _record_summary(observed_record),
        "duplicate_record": _record_summary(duplicate_record),
        "as_participant": {
            "participant_id": local_participant.participant_id,
            "common_name": local_participant.common_name,
        },
        "lifecycle": lifecycle_entry.as_dict(),
    }


def _record_inbound_rejection(
    binding: project.ProjectBinding,
    transaction: project.ProjectTransaction,
    *,
    observed_record: dict[str, Any],
    error: CamUsageError | CamValidationError | state.StateError,
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
        "observed_record": _record_summary(observed_record),
        "rejected_record": _record_summary(rejected_record),
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
) -> dict[str, Any]:
    observed_now, observed_at = _utc_now()
    return journal.append_record(
        binding,
        event_type="message.inbound.observed",
        exact_message=raw,
        attributes={"source": source, "observed_at": observed_at},
        now=observed_now,
        transaction=transaction,
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
    as_participant: str,
    observed_record: dict[str, Any],
    validation_profile: dict[str, Any],
) -> tuple[int, dict[str, Any]] | None:
    envelope = parse_exact_bytes(raw)
    message_id = _candidate_message_id(envelope)
    if message_id is None:
        return None
    if store.preserved_message(message_id, transaction=transaction) != raw:
        return None
    local_participant, sender_participant = _require_inbound_roster_endpoints(
        store,
        transaction,
        raw,
        local_selector=as_participant,
    )
    prior_validation = _prior_inbound_validation(
        binding,
        message_id=message_id,
        recipient_participant_id=local_participant.participant_id,
    )
    if prior_validation is None:
        return None
    entry = _duplicate_lifecycle_entry(store, transaction, envelope, message_id)
    return _record_inbound_duplicate(
        binding,
        transaction,
        observed_record=observed_record,
        message_id=message_id,
        prior_validation=prior_validation,
        local_participant=local_participant,
        sender_participant=sender_participant,
        lifecycle_entry=entry,
        validation_profile=validation_profile,
    )


def _prepare_initial_inbound(
    store: StateStore,
    transaction: project.ProjectTransaction,
    *,
    raw: bytes,
    renewal_of: str | None,
    as_participant: str,
) -> tuple[state.LifecyclePlan, state.Participant, state.Participant]:
    validation_now, _ = _utc_now()
    plan = store.prepare_inbound_lifecycle(
        raw,
        renewal_of=renewal_of,
        now=validation_now,
        transaction=transaction,
    )
    local_participant, sender_participant = _require_inbound_roster_endpoints(
        store,
        transaction,
        raw,
        local_selector=as_participant,
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
    return plan, local_participant, sender_participant


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
    local_participant: state.Participant,
    sender_participant: state.Participant,
    entry: lifecycle.LifecycleEntry,
    projection_error: state.ProjectionRefreshError | None,
    validation_profile: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    validated_now, validated_at = _utc_now()
    validated_record = journal.append_record(
        binding,
        event_type="message.inbound.validated",
        attributes={
            "observed_record_id": observed_record["record_id"],
            "message_id": plan.attributes.get(
                "message_id", plan.attributes["root_message_id"]
            ),
            "sender_participant_id": sender_participant.participant_id,
            "recipient_participant_id": local_participant.participant_id,
            "authorization_evaluated": False,
            "action_authorized": False,
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
    return 0, {
        "ok": True,
        "status": "validated",
        "duplicate": False,
        "authorization_evaluated": False,
        "action_authorized": False,
        "validation_profile": validation_profile,
        "state_projection": {
            "current": projection_error is None,
            "rebuild_required": projection_error is not None,
            "last_committed_record": last_committed_record,
        },
        "observed_record": _record_summary(observed_record),
        "validated_record": _record_summary(validated_record),
        "as_participant": {
            "participant_id": local_participant.participant_id,
            "common_name": local_participant.common_name,
        },
        "lifecycle": entry.as_dict(),
    }


def _ingest_message(
    binding: project.ProjectBinding,
    *,
    message_path: str | None,
    as_participant: str,
    renewal_of: str | None,
    exact_message: bytes | None = None,
    observed_source: str = "owner_only_file",
) -> tuple[int, dict[str, Any]]:
    validation_profile = profile.validation_profile_report()
    raw = _exact_ingest_source(
        message_path=message_path,
        exact_message=exact_message,
    )
    store = StateStore(binding)
    with project.project_transaction(binding) as transaction:
        observed_record = _observe_inbound(
            binding,
            transaction,
            raw,
            source=observed_source,
        )
        try:
            duplicate_result = _early_exact_duplicate(
                binding,
                transaction,
                store,
                raw=raw,
                as_participant=as_participant,
                observed_record=observed_record,
                validation_profile=validation_profile,
            )
            if duplicate_result is not None:
                return duplicate_result
            plan, local_participant, sender_participant = _prepare_initial_inbound(
                store,
                transaction,
                raw=raw,
                renewal_of=renewal_of,
                as_participant=as_participant,
            )
        except (CamUsageError, CamValidationError, state.StateError) as error:
            return _record_inbound_rejection(
                binding,
                transaction,
                observed_record=observed_record,
                error=error,
                validation_profile=validation_profile,
            )
        message_id = plan.attributes.get(
            "message_id", plan.attributes["root_message_id"]
        )
        prior_validation = _prior_inbound_validation(
            binding,
            message_id=message_id,
            recipient_participant_id=local_participant.participant_id,
        )
        if prior_validation is not None:
            return _record_inbound_duplicate(
                binding,
                transaction,
                observed_record=observed_record,
                message_id=message_id,
                prior_validation=prior_validation,
                local_participant=local_participant,
                sender_participant=sender_participant,
                lifecycle_entry=plan.preview,
                validation_profile=validation_profile,
            )
        try:
            plan = _refresh_inbound_plan(
                store,
                transaction,
                raw=raw,
                renewal_of=renewal_of,
            )
        except (CamUsageError, CamValidationError, state.StateError) as error:
            return _record_inbound_rejection(
                binding,
                transaction,
                observed_record=observed_record,
                error=error,
                validation_profile=validation_profile,
            )
        try:
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
            local_participant=local_participant,
            sender_participant=sender_participant,
            entry=entry,
            projection_error=projection_error,
            validation_profile=validation_profile,
        )


def _handle_project(args: argparse.Namespace) -> int:
    binding = (
        project.initialize_project(
            args.project_root,
            state_root=args.state_root,
            git_bin=args.git_bin,
        )
        if args.project_command == "init"
        else _resolve(args)
    )
    verification = journal.verify_journal(binding)
    _emit(
        {
            "ok": True,
            "status": "initialized" if args.project_command == "init" else "ready",
            "project": binding.summary(),
            "journal": verification.summary(),
        }
    )
    return 0


def _handle_journal(args: argparse.Namespace, binding: project.ProjectBinding) -> int:
    if args.journal_command == "append":
        if args.event_type.startswith(("state.", "message.", "transport.", "journal.")):
            raise project.ProjectError(
                "journal.event_reserved",
                "state.*, message.*, transport.*, and journal.* event names are "
                "reserved for validated internal operations; use note.* for "
                "operator notes",
            )
        exact_message = (
            project.read_private_bytes(
                Path(args.message), max_bytes=journal.MAX_EXACT_MESSAGE_BYTES
            )
            if args.message
            else None
        )
        attributes = (
            project.read_private_json(
                Path(args.attributes_file), max_bytes=journal.MAX_ATTRIBUTES_BYTES
            )
            if args.attributes_file
            else None
        )
        record = journal.append_record(
            binding,
            event_type=args.event_type,
            exact_message=exact_message,
            attributes=attributes,
        )
        _emit({"ok": True, "status": "appended", "record": _record_summary(record)})
        return 0
    if args.journal_command == "verify":
        _emit(
            {
                "ok": True,
                "status": "verified",
                "journal": journal.verify_journal(binding).summary(),
            }
        )
        return 0
    if args.journal_command == "recovery-status":
        _emit(
            {
                "ok": True,
                "status": "recoverable_partial_tail",
                "recovery": journal.inspect_partial_tail(binding).summary(),
            }
        )
        return 0
    if args.journal_command == "recover-partial-tail":
        recovery = journal.recover_partial_tail(
            binding,
            expected_journal_sha256=args.expected_journal_sha256,
            confirm_project_id=args.confirm_project_id,
            reason=args.reason,
            operator_reference=args.operator_reference,
        )
        _emit(
            {
                "ok": True,
                "status": "recovered_partial_tail",
                "recovery": recovery.summary(),
                "record": _record_summary(recovery.recovered_record),
            }
        )
        return 0
    records = journal.tail_records(
        binding,
        limit=args.limit,
        redact=not args.show_content,
    )
    _emit(
        {
            "ok": True,
            "status": "verified",
            "records": (
                [_record_with_content(record) for record in records]
                if args.show_content
                else list(records)
            ),
        }
    )
    return 0


def _bind_participant(
    args: argparse.Namespace,
    binding: project.ProjectBinding,
    store: StateStore,
) -> state.Participant:
    event_now, bound_at = _utc_now()
    with project.project_transaction(binding) as transaction:
        participant = store.participant_bind(
            args.participant,
            session_id=args.session_id,
            session_label=args.session_label,
            session_kind=args.session_kind,
            operator_reference=args.operator_reference,
            bound_at=bound_at,
            now=event_now,
            transaction=transaction,
        )
        if participant.vendor == "codex":
            if participant.binding is None:
                raise state.StateError(
                    "state.binding_missing", "bound participant has no session"
                )
            session_id = participant.binding.session_id
            participant = store.participant_observe_route(
                participant.participant_id,
                transport="codex_queue",
                address=session_id,
                source="operator_confirmed_binding",
                observed_at=bound_at,
                now=event_now,
                transaction=transaction,
            )
            participant = store.participant_confirm_route(
                participant.participant_id,
                expected_address=session_id,
                operator_reference=args.operator_reference,
                confirmed_at=bound_at,
                now=event_now,
                transaction=transaction,
            )
    return participant


def _handle_participant(
    args: argparse.Namespace,
    binding: project.ProjectBinding,
    store: StateStore,
) -> int:
    if args.participant_command == "add":
        event_now, _ = _utc_now()
        participant = store.participant_add(
            common_name=args.common_name,
            display_name=args.display_name,
            role=args.role,
            vendor=args.vendor,
            participant_id=args.participant_id,
            now=event_now,
        )
        status = "added"
    elif args.participant_command == "bind":
        participant = _bind_participant(args, binding, store)
        status = "bound"
    elif args.participant_command == "confirm-route":
        event_now, confirmed_at = _utc_now()
        participant = store.participant_confirm_route(
            args.participant,
            expected_address=args.expected_address,
            operator_reference=args.operator_reference,
            confirmed_at=confirmed_at,
            now=event_now,
        )
        status = "route_confirmed"
    elif args.participant_command == "list":
        snapshot = store.snapshot()
        _emit(
            {
                "ok": True,
                "status": "listed",
                "roster": snapshot.roster.as_dict(redact=not args.show_identifiers),
            }
        )
        return 0
    else:
        event_now, _ = _utc_now()
        operation = (
            store.participant_invalidate
            if args.participant_command == "invalidate"
            else store.participant_retire
        )
        participant = operation(
            args.participant,
            reason=args.reason,
            now=event_now,
        )
        status = (
            "invalidated" if args.participant_command == "invalidate" else "retired"
        )
    _emit(
        {
            "ok": True,
            "status": status,
            "participant": participant.as_dict(redact=True),
        }
    )
    return 0


def _handle_state(args: argparse.Namespace, store: StateStore) -> int:
    snapshot = store.snapshot() if args.state_command == "status" else store.rebuild()
    _emit(
        {
            "ok": True,
            "status": "ready" if args.state_command == "status" else "rebuilt",
            "state": _state_summary(snapshot),
        }
    )
    return 0


def _handle_message(args: argparse.Namespace, binding: project.ProjectBinding) -> int:
    exact_message: bytes | None = None
    message_path = args.message
    observed_source = "owner_only_file"
    if args.stdin:
        exact_message = _read_exact_stdin(max_bytes=journal.MAX_EXACT_MESSAGE_BYTES)
        project.create_private_bytes(Path(args.capture_to), exact_message)
        message_path = None
        observed_source = "binary_stdin_capture"
    return_code, payload = _ingest_message(
        binding,
        message_path=message_path,
        as_participant=args.as_participant,
        renewal_of=args.renewal_of,
        exact_message=exact_message,
        observed_source=observed_source,
    )
    _emit(payload, stream=sys.stderr if return_code else sys.stdout)
    return return_code


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    _validate_arguments(parser, args)
    try:
        if args.domain == "project":
            return _handle_project(args)
        binding = _resolve(args)
        if args.domain == "journal":
            return _handle_journal(args, binding)
        store = StateStore(binding)
        if args.domain == "participant":
            return _handle_participant(args, binding, store)
        if args.domain == "state":
            return _handle_state(args, store)
        if args.domain == "message":
            return _handle_message(args, binding)
    except (project.ProjectError, CamUsageError) as error:
        _emit(
            {"ok": False, "error": {"code": error.code, "detail": error.detail}},
            stream=sys.stderr,
        )
        return 2
    except CamValidationError as error:
        _emit(
            {
                "ok": False,
                "error": {
                    "code": "validation.failed",
                    "problem_codes": [problem.code for problem in error.problems[:16]],
                },
            },
            stream=sys.stderr,
        )
        return 2
    except Exception as error:  # noqa: BLE001 - do not expose raw local state details
        _emit(
            {
                "ok": False,
                "error": {
                    "code": "project.internal",
                    "detail": f"unexpected project-state failure ({type(error).__name__})",
                },
            },
            stream=sys.stderr,
        )
        return 3
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
