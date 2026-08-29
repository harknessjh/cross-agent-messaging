# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Local file I/O and command-line orchestration for the CAM/1 facade."""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .builders import (
    AUTHORIZATION_BASES,
    RISK_CLASSES,
    STATUS_VALUES,
    build_ack,
    build_cancel,
    build_challenge,
    build_error,
    build_hello,
    build_late_rejection,
    build_request,
    build_result,
    build_status,
    build_status_inquiry,
    build_verify,
    renew_request,
)
from .profile import validation_profile_report
from .project import (
    ProjectError,
    create_private_bytes,
)
from .protocol import (
    ACK_STATUSES,
    DEFAULT_CLOCK_SKEW_SECONDS,
    DEFAULT_MAX_TTL_SECONDS,
    DEFAULT_TTL_SECONDS,
    MAX_ENVELOPE_BYTES,
    CamUsageError,
    CamValidationError,
    CliError,
    Problem,
    ValidationPolicy,
)
from .secure_fs import (
    _open_directory_fd,
    _open_private_directory,
    _split_local_file_path,
    _validate_private_file,
)
from .validation import validate_exact_bytes


def _split_local_path(path_text: str) -> tuple[Path, str]:
    try:
        return _split_local_file_path(path_text)
    except ProjectError:
        raise CliError("path.invalid", "path must name one local file") from None


def read_envelope_file(path: str) -> bytes:
    """Read one bounded envelope from a regular file, or from stdin for ``-``."""

    path_text = os.fspath(path)
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

    parent, name = _split_local_path(path_text)
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        parent_descriptor = _open_directory_fd(parent)
    except ProjectError:
        raise CliError("input.open", "input must be a readable regular file") from None
    try:
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    except OSError:
        os.close(parent_descriptor)
        raise CliError("input.open", "input must be a readable regular file") from None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise CliError("input.type", "input must be a regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            raw = handle.read(MAX_ENVELOPE_BYTES + 1)
    finally:
        os.close(descriptor)
        os.close(parent_descriptor)
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


def read_private_envelope_file(path: str) -> bytes:
    """Read an owner-only live envelope without following any path component."""

    path_text = os.fspath(path)
    if path_text == "-":
        raise CliError("input.open", "live envelope must be a private regular file")
    parent, name = _split_local_path(path_text)
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        parent_descriptor = _open_private_directory(parent, label="input.directory")
    except ProjectError:
        raise CliError(
            "input.private",
            "live envelope must be under an owner-only, non-symlinked directory",
        ) from None
    try:
        try:
            descriptor = os.open(name, flags, dir_fd=parent_descriptor)
        except OSError:
            raise CliError(
                "input.private",
                "live envelope must be owner-owned, single-link, and mode 0600",
            ) from None
        try:
            try:
                _validate_private_file(descriptor, label="input.file")
            except ProjectError:
                raise CliError(
                    "input.private",
                    "live envelope must be owner-owned, single-link, mode 0600, "
                    "and free of extended ACLs",
                ) from None
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                raw = handle.read(MAX_ENVELOPE_BYTES + 1)
        finally:
            os.close(descriptor)
    finally:
        os.close(parent_descriptor)
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


def _write_stdout(raw: bytes) -> None:
    sys.stdout.buffer.write(raw)
    sys.stdout.buffer.flush()


def _write_output(raw: bytes, path_text: str) -> None:
    if path_text == "-":
        raise CliError("output.path", "use --stdout to write an envelope to stdout")
    parent, name = _split_local_path(path_text)
    try:
        create_private_bytes(parent / name, raw)
    except ProjectError:
        raise CliError(
            "output.create",
            "output could not be atomically published as a new private file",
        ) from None


def _add_endpoint_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--sender-vendor", required=True)
    parser.add_argument("--sender-name", required=True)
    parser.add_argument("--sender-session", required=True)
    parser.add_argument("--sender-host-id")
    parser.add_argument("--reply-transport", required=True)
    parser.add_argument("--reply-address", required=True)
    parser.add_argument("--expires-in", type=int, default=DEFAULT_TTL_SECONDS)


def _add_output_arguments(parser: argparse.ArgumentParser) -> None:
    output = parser.add_mutually_exclusive_group(required=True)
    output.add_argument(
        "--output",
        metavar="PATH",
        help="create a new mode-0600 envelope file",
    )
    output.add_argument(
        "--stdout",
        action="store_true",
        help="write exact envelope bytes to stdout",
    )


def _add_recipient_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--recipient-vendor", required=True)
    parser.add_argument("--recipient-name", required=True)
    parser.add_argument("--recipient-session")
    parser.add_argument("--recipient-host-id")


def _add_authorization_arguments(
    parser: argparse.ArgumentParser, *, include_none: bool = True
) -> None:
    bases = sorted(AUTHORIZATION_BASES - {"first_contact"})
    if not include_none:
        bases.remove("none")
    parser.add_argument("--authorization-basis", required=True, choices=bases)
    parser.add_argument("--authority")
    parser.add_argument("--authorization-reference")
    parser.add_argument("--authorization-verified-at")
    parser.add_argument("--authorization-expires-at")


def _add_scope_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--scope-repository", action="append", default=[])
    parser.add_argument("--scope-path", action="append", default=[])
    parser.add_argument("--scope-host", action="append", default=[])
    parser.add_argument("--scope-external-recipient", action="append", default=[])


def _add_correlated_reply_arguments(
    parser: argparse.ArgumentParser, *, body_required: bool
) -> None:
    _add_endpoint_arguments(parser)
    _add_output_arguments(parser)
    parser.add_argument("--request", required=True)
    parser.add_argument("--detail")
    parser.add_argument("--intent")
    parser.add_argument("--body", required=body_required)
    parser.add_argument("--previous-response", action="append", default=[])


def _write_built_envelope(args: argparse.Namespace, raw: bytes) -> None:
    if args.stdout:
        _write_stdout(raw)
    else:
        _write_output(raw, args.output)


def _scope_from_args(args: argparse.Namespace) -> dict[str, list[str]]:
    return {
        "repositories": args.scope_repository,
        "paths": args.scope_path,
        "hosts": args.scope_host,
        "external_recipients": args.scope_external_recipient,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build and validate CAM/1 envelopes without sending them."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser(
        "validation-profile",
        help="show the exact local validation-tool profile and source state",
    )

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
    _add_output_arguments(hello_parser)
    _add_recipient_arguments(hello_parser)

    challenge_parser = subparsers.add_parser(
        "build-challenge", help="build one harmless peer-correlation challenge"
    )
    _add_endpoint_arguments(challenge_parser)
    _add_output_arguments(challenge_parser)
    _add_recipient_arguments(challenge_parser)

    verify_parser = subparsers.add_parser(
        "build-verify", help="build a complete response to an exact challenge"
    )
    _add_endpoint_arguments(verify_parser)
    _add_output_arguments(verify_parser)
    verify_parser.add_argument("--challenge", required=True)

    request_parser = subparsers.add_parser(
        "build-request", help="build a typed work or information request"
    )
    _add_endpoint_arguments(request_parser)
    _add_output_arguments(request_parser)
    _add_recipient_arguments(request_parser)
    _add_authorization_arguments(request_parser)
    _add_scope_arguments(request_parser)
    request_parser.add_argument(
        "--risk-class", required=True, choices=sorted(RISK_CLASSES)
    )
    request_parser.add_argument("--operation", required=True)
    request_parser.add_argument("--intent", required=True)
    request_parser.add_argument("--body", required=True)
    request_parser.add_argument("--idempotency-key")
    request_parser.add_argument("--allow-repository-changes", action="store_true")
    request_parser.add_argument("--allow-external-side-effects", action="store_true")

    ack_parser = subparsers.add_parser(
        "build-ack", help="build a complete acknowledgment from an exact request"
    )
    _add_endpoint_arguments(ack_parser)
    _add_output_arguments(ack_parser)
    ack_parser.add_argument("--request", required=True)
    ack_parser.add_argument(
        "--status",
        choices=sorted(ACK_STATUSES),
        default="needs_human_confirmation",
    )
    ack_parser.add_argument("--detail")
    ack_parser.add_argument("--intent", default="Acknowledge CAM/1 first contact")
    ack_parser.add_argument("--body")

    status_parser = subparsers.add_parser(
        "build-status", help="build an accepted or started request status"
    )
    _add_correlated_reply_arguments(status_parser, body_required=True)
    status_parser.add_argument("--status", required=True, choices=sorted(STATUS_VALUES))

    result_parser = subparsers.add_parser(
        "build-result", help="build one completed result for an exact request"
    )
    _add_correlated_reply_arguments(result_parser, body_required=True)

    error_parser = subparsers.add_parser(
        "build-error", help="build a failed result for an exact request"
    )
    _add_correlated_reply_arguments(error_parser, body_required=True)

    cancel_parser = subparsers.add_parser(
        "build-cancel", help="build an operator-confirmed request cancellation"
    )
    _add_endpoint_arguments(cancel_parser)
    _add_output_arguments(cancel_parser)
    cancel_parser.add_argument("--request", required=True)
    cancel_parser.add_argument("--authority", required=True)
    cancel_parser.add_argument("--authorization-reference", required=True)
    cancel_parser.add_argument("--authorization-verified-at", required=True)
    cancel_parser.add_argument("--authorization-expires-at", required=True)
    cancel_parser.add_argument("--body")

    inquiry_parser = subparsers.add_parser(
        "build-status-inquiry",
        help="build a new informational request about an existing request",
    )
    _add_output_arguments(inquiry_parser)
    inquiry_parser.add_argument("--request", required=True)
    inquiry_parser.add_argument("--expires-in", type=int, default=DEFAULT_TTL_SECONDS)

    renew_parser = subparsers.add_parser(
        "renew-request",
        help="safely renew an expired request without changing its action",
    )
    _add_output_arguments(renew_parser)
    renew_parser.add_argument("--request", required=True)
    renew_parser.add_argument("--expires-in", type=int, default=DEFAULT_TTL_SECONDS)
    _add_authorization_arguments(renew_parser)
    renew_parser.add_argument("--confirm-no-known-pending", action="store_true")

    late_parser = subparsers.add_parser(
        "build-late-rejection",
        help="reject an expired root without acting or echoing its nonce",
    )
    _add_endpoint_arguments(late_parser)
    _add_output_arguments(late_parser)
    late_parser.add_argument("--request", required=True)
    return parser


def _emit_error(error: Exception) -> None:
    if isinstance(error, CamValidationError):
        payload = {
            "valid": False,
            "problems": [problem.as_dict() for problem in error.problems],
        }
    elif isinstance(error, (CamUsageError, CliError)):
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
    payload["validation_profile"] = validation_profile_report()
    sys.stderr.write(json.dumps(payload, separators=(",", ":")) + "\n")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "validation-profile":
            sys.stdout.write(
                json.dumps(
                    validation_profile_report(),
                    separators=(",", ":"),
                )
                + "\n"
            )
            return 0

        if args.command == "validate":
            raw = read_envelope_file(args.message)
            against_raw = read_envelope_file(args.against) if args.against else None
            result = validate_exact_bytes(
                raw,
                against_raw=against_raw,
                policy=ValidationPolicy(
                    allow_expired=args.allow_expired,
                    max_ttl_seconds=args.max_ttl_seconds,
                    clock_skew_seconds=args.clock_skew_seconds,
                ),
            )
            sys.stdout.write(json.dumps(result.summary(), separators=(",", ":")) + "\n")
            return 0

        endpoint_args: dict[str, Any] = {}
        if hasattr(args, "sender_vendor"):
            endpoint_args = {
                "sender_vendor": args.sender_vendor,
                "sender_name": args.sender_name,
                "sender_session": args.sender_session,
                "sender_host_id": args.sender_host_id,
                "reply_transport": args.reply_transport,
                "reply_address": args.reply_address,
                "expires_in": args.expires_in,
            }

        if args.command in {"build-hello", "build-challenge"}:
            builder = build_hello if args.command == "build-hello" else build_challenge
            raw = builder(
                **endpoint_args,
                recipient_vendor=args.recipient_vendor,
                recipient_name=args.recipient_name,
                recipient_session=args.recipient_session,
                recipient_host_id=args.recipient_host_id,
            )
            _write_built_envelope(args, raw)
            return 0

        if args.command == "build-verify":
            raw = build_verify(
                read_envelope_file(args.challenge),
                **endpoint_args,
            )
            _write_built_envelope(args, raw)
            return 0

        if args.command == "build-request":
            raw = build_request(
                **endpoint_args,
                recipient_vendor=args.recipient_vendor,
                recipient_name=args.recipient_name,
                recipient_session=args.recipient_session,
                recipient_host_id=args.recipient_host_id,
                risk_class=args.risk_class,
                operation=args.operation,
                intent=args.intent,
                body=args.body,
                authorization_basis=args.authorization_basis,
                authority=args.authority,
                authorization_reference=args.authorization_reference,
                authorization_verified_at=args.authorization_verified_at,
                authorization_expires_at=args.authorization_expires_at,
                scope=_scope_from_args(args),
                idempotency_key=args.idempotency_key,
                allow_repository_changes=args.allow_repository_changes,
                allow_external_side_effects=args.allow_external_side_effects,
            )
            _write_built_envelope(args, raw)
            return 0

        if args.command == "build-ack":
            request_raw = read_envelope_file(args.request)
            raw = build_ack(
                request_raw,
                **endpoint_args,
                status_value=args.status,
                detail=args.detail,
                intent=args.intent,
                body=args.body,
            )
            _write_built_envelope(args, raw)
            return 0

        if args.command in {"build-status", "build-result", "build-error"}:
            request_raw = read_envelope_file(args.request)
            previous = tuple(
                read_envelope_file(path) for path in args.previous_response
            )
            common: dict[str, Any] = {
                **endpoint_args,
                "body": args.body,
                "detail": args.detail,
                "previous_responses": previous,
            }
            if args.intent is not None:
                common["intent"] = args.intent
            if args.command == "build-status":
                raw = build_status(request_raw, status_value=args.status, **common)
            elif args.command == "build-result":
                raw = build_result(request_raw, **common)
            else:
                raw = build_error(request_raw, **common)
            _write_built_envelope(args, raw)
            return 0

        if args.command == "build-cancel":
            keyword_args = {
                **endpoint_args,
                "authority": args.authority,
                "authorization_reference": args.authorization_reference,
                "authorization_verified_at": args.authorization_verified_at,
                "authorization_expires_at": args.authorization_expires_at,
            }
            if args.body is not None:
                keyword_args["body"] = args.body
            raw = build_cancel(read_envelope_file(args.request), **keyword_args)
            _write_built_envelope(args, raw)
            return 0

        if args.command == "build-status-inquiry":
            raw = build_status_inquiry(
                read_envelope_file(args.request), expires_in=args.expires_in
            )
            _write_built_envelope(args, raw)
            return 0

        if args.command == "renew-request":
            raw = renew_request(
                read_envelope_file(args.request),
                authorization_basis=args.authorization_basis,
                authority=args.authority,
                authorization_reference=args.authorization_reference,
                authorization_verified_at=args.authorization_verified_at,
                authorization_expires_at=args.authorization_expires_at,
                confirm_no_known_pending=args.confirm_no_known_pending,
                expires_in=args.expires_in,
            )
            _write_built_envelope(args, raw)
            return 0

        if args.command == "build-late-rejection":
            raw = build_late_rejection(
                read_envelope_file(args.request), **endpoint_args
            )
            _write_built_envelope(args, raw)
            return 0
    except (CamValidationError, CamUsageError, CliError) as error:
        _emit_error(error)
        return 2
    except Exception as error:  # noqa: BLE001 - keep envelope failures redacted
        _emit_error(error)
        return 3
    return 3
