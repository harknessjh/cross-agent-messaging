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
from typing import Any

from .builders import build_ack, build_hello
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
from .validation import validate_exact_bytes


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


def _write_stdout(raw: bytes) -> None:
    sys.stdout.buffer.write(raw)
    sys.stdout.buffer.flush()


def _write_output(raw: bytes, path_text: str) -> None:
    if path_text == "-":
        raise CliError("output.path", "use --stdout to write an envelope to stdout")
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
    _add_output_arguments(hello_parser)
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
    sys.stderr.write(json.dumps(payload, separators=(",", ":")) + "\n")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
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
            if args.stdout:
                _write_stdout(raw)
            else:
                _write_output(raw, args.output)
            return 0

        if args.command == "build-ack":
            request_raw = read_envelope_file(args.request)
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
            if args.stdout:
                _write_stdout(raw)
            else:
                _write_output(raw, args.output)
            return 0
    except (CamValidationError, CamUsageError, CliError) as error:
        _emit_error(error)
        return 2
    except Exception as error:  # noqa: BLE001 - keep envelope failures redacted
        _emit_error(error)
        return 3
    return 3
