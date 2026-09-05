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
from pathlib import Path
from typing import Any

from tools.cam1lib import (
    compatibility_cli,
    inbound,
    journal,
    onboarding,
    onboarding_cli,
    product_approvals,
    project,
    state,
)
from tools.cam1lib.protocol import CamUsageError, CamValidationError
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
        description="Manage one Git-bound CAM project and its required local journal.",
        allow_abbrev=False,
    )
    parser.add_argument(
        "--project-root",
        default=".",
        help="target Git worktree; defaults to the current working directory",
    )
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
    participant_add.add_argument("--role")
    participant_add.add_argument(
        "--vendor", choices=("codex", "claude-code"), required=True
    )
    participant_add.add_argument("--participant-id")
    participant_add.add_argument("--product-bin")

    participant_bind = participant_commands.add_parser(
        "bind", help="bind a participant to an operator-confirmed product session"
    )
    participant_bind.add_argument("--participant", required=True)
    participant_bind.add_argument("--session-id", required=True)
    participant_bind.add_argument("--session-label")
    participant_bind.add_argument(
        "--session-kind",
        help="required for Claude Code bindings; optional for Codex",
    )
    participant_bind.add_argument("--operator-reference", required=True)

    participant_confirm = participant_commands.add_parser(
        "confirm-route",
        help=(
            "record an optional operator guard for the currently observed route; "
            "not required for a uniquely tool-correlated Claude route"
        ),
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

    participant_update = participant_commands.add_parser(
        "update-metadata",
        help="update descriptive metadata without changing participant identity",
    )
    participant_update.add_argument("--participant", required=True)
    participant_update.add_argument("--expected-revision", type=int, required=True)
    participant_update.add_argument("--display-name")
    role_update = participant_update.add_mutually_exclusive_group()
    role_update.add_argument("--role")
    role_update.add_argument("--clear-role", action="store_true")
    executable_update = participant_update.add_mutually_exclusive_group()
    executable_update.add_argument("--product-bin")
    executable_update.add_argument("--clear-product-bin", action="store_true")
    participant_update.add_argument("--operator-reference", required=True)

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

    compatibility_cli.register_parser(domains)
    onboarding_cli.add_parser(domains)

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
        "--stdin-byte-count",
        type=int,
        help="capture exactly this many binary stdin bytes without waiting for EOF",
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
    if (
        args.domain == "participant"
        and args.participant_command == "update-metadata"
        and args.display_name is None
        and args.role is None
        and not args.clear_role
        and args.product_bin is None
        and not args.clear_product_bin
    ):
        parser.error("update-metadata requires at least one metadata change")
    if args.domain != "message" or args.message_command != "ingest":
        return
    if args.stdin:
        if args.stdin_byte_count is not None and not (
            1 <= args.stdin_byte_count <= journal.MAX_EXACT_MESSAGE_BYTES
        ):
            parser.error(
                f"--stdin-byte-count must be between 1 and {journal.MAX_EXACT_MESSAGE_BYTES}"
            )
        if args.capture_to is None:
            parser.error("--capture-to is required with --stdin")
        if not Path(args.capture_to).is_absolute():
            parser.error("--capture-to must be an absolute new file path")
        return
    if args.capture_to is not None:
        parser.error("--capture-to is valid only with --stdin")
    if args.stdin_byte_count is not None:
        parser.error("--stdin-byte-count is valid only with --stdin")


def _read_exact_stdin(*, max_bytes: int, byte_count: int | None = None) -> bytes:
    """Read one bounded binary stdin payload without text transcoding."""

    stream = getattr(sys.stdin, "buffer", None)
    if stream is None:
        raise project.ProjectError(
            "message.stdin_binary", "binary stdin is unavailable"
        )
    if byte_count is not None:
        chunks: list[bytes] = []
        remaining = byte_count
        while remaining:
            chunk = stream.read(remaining)
            if not chunk:
                raise project.ProjectError(
                    "message.stdin_short",
                    f"stdin ended after {byte_count - remaining} of {byte_count} bytes",
                )
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
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
    return inbound.record_summary(record)


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
        "enrollment_proposal_count": len(snapshot.enrollment.proposals),
        "pending_enrollment_count": sum(
            proposal.status.value == "pending"
            for proposal in snapshot.enrollment.proposals.values()
        ),
        "lifecycle_count": len(snapshot.lifecycle.entries),
        "lifecycle_states": dict(sorted(lifecycle_states.items())),
    }


_prior_inbound_validation = inbound.prior_inbound_validation
_ingest_message = inbound.ingest_message


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
        if args.event_type.startswith(
            ("state.", "message.", "transport.", "journal.", "compatibility.")
        ):
            raise project.ProjectError(
                "journal.event_reserved",
                "state.*, message.*, transport.*, journal.*, and compatibility.* "
                "event names are reserved for validated internal operations; use "
                "note.* for operator notes",
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
        existing = store.snapshot(transaction=transaction).roster.select(
            args.participant
        )
        if existing.vendor == "claude-code" and args.session_kind is None:
            raise CamUsageError(
                "roster.session_kind_required",
                "Claude bindings require the session kind shown by /status",
            )
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
        product_executable = (
            onboarding.resolve_product_executable(args.product_bin, vendor=args.vendor)
            if args.product_bin is not None
            else None
        )
        participant = store.participant_add(
            common_name=args.common_name,
            display_name=args.display_name,
            role=args.role,
            vendor=args.vendor,
            approved_product_executable=product_executable,
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
    elif args.participant_command == "update-metadata":
        event_now, updated_at = _utc_now()
        existing = store.snapshot().roster.select(args.participant)
        display_name = args.display_name or existing.display_name
        role = (
            None
            if args.clear_role
            else args.role
            if args.role is not None
            else existing.role
        )
        product_executable = (
            None
            if args.clear_product_bin
            else (
                onboarding.resolve_product_executable(
                    args.product_bin, vendor=existing.vendor
                )
                if args.product_bin is not None
                else existing.approved_product_executable
            )
        )
        participant = store.participant_update_metadata(
            args.participant,
            display_name=display_name,
            role=role,
            approved_product_executable=product_executable,
            expected_revision=args.expected_revision,
            operator_reference=args.operator_reference,
            updated_at=updated_at,
            now=event_now,
        )
        status = "metadata_updated"
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
        exact_message = _read_exact_stdin(
            max_bytes=journal.MAX_EXACT_MESSAGE_BYTES,
            byte_count=args.stdin_byte_count,
        )
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
    product_approvals.begin_operation()
    parser = _parser()
    args = parser.parse_args(argv)
    _validate_arguments(parser, args)
    try:
        if args.domain == "project":
            return _handle_project(args)
        if args.domain == "onboarding" and args.onboarding_command != "status":
            onboarding.require_trusted_source()
        binding = (
            project.initialize_project(
                args.project_root,
                state_root=args.state_root,
                git_bin=args.git_bin,
            )
            if args.domain == "onboarding" and args.onboarding_command == "prepare"
            else _resolve(args)
        )
        if args.domain == "journal":
            return _handle_journal(args, binding)
        store = StateStore(binding)
        if args.domain == "participant":
            return _handle_participant(args, binding, store)
        if args.domain == "state":
            return _handle_state(args, store)
        if args.domain == "compatibility":
            return_code, payload = compatibility_cli.handle(args, binding, store)
            _emit(payload, stream=sys.stderr if return_code else sys.stdout)
            return return_code
        if args.domain == "onboarding":
            _emit(onboarding_cli.handle(args, binding, store))
            return 0
        if args.domain == "message":
            return _handle_message(args, binding)
    except state.CompatibilityUpgradeRequired as error:
        _emit(
            {
                "ok": False,
                "status": "upgrade_required",
                "error": error.as_dict(),
                "recovery": {
                    "command": "compatibility status",
                    "detail": "run with the same global project and state-root options",
                },
            },
            stream=sys.stderr,
        )
        return 2
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
