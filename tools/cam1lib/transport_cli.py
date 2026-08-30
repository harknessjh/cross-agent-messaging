# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Argument parsing and dispatch for the CAM/1 local transport command."""

from __future__ import annotations

import argparse
import asyncio
import functools
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class TransportCliApi:
    """Patch-friendly facade supplied by the public transport module."""

    cam1: Any
    project: Any
    transport_error: type[Exception]
    default_timeout_seconds: float
    emit: Callable[..., None]
    with_validation_profile: Callable[..., dict[str, Any]]
    bounded_timeout: Callable[[float], float]
    doctor: Callable[..., dict[str, Any]]
    require_live_validation_profile: Callable[..., tuple[dict[str, Any], bool]]
    resolve_binary: Callable[..., str]
    resolve_project: Callable[[argparse.Namespace], Any]
    list_local_peers: Callable[..., Any]
    preflight_project_claude: Callable[..., Any]
    send_project_claude: Callable[..., Any]
    send_project_codex: Callable[..., dict[str, Any]]


class JsonArgumentParser(argparse.ArgumentParser):
    """Keep command-line failures on the documented JSON error channel."""

    def __init__(self, api: TransportCliApi, *args: Any, **kwargs: Any) -> None:
        self._api = api
        super().__init__(*args, **kwargs)

    def error(self, message: str) -> None:
        self._api.emit(
            {
                "ok": False,
                "error": {"code": "argument.invalid", "detail": message[:500]},
            },
            stream=sys.stderr,
        )
        raise SystemExit(2)


def build_parser(api: TransportCliApi) -> argparse.ArgumentParser:
    parser = JsonArgumentParser(
        api,
        description="Use Claude Code's local messaging transport for CAM/1 envelopes.",
    )
    parser.add_argument("--claude-bin", default="claude")
    parser.add_argument("--codex-bin", default="codex")
    parser.add_argument(
        "--project-root",
        default=".",
        help="Git worktree whose CAM project, roster, and journal must be used",
    )
    parser.add_argument(
        "--state-root",
        help="absolute owner-private journal root override (primarily for tests)",
    )
    parser.add_argument(
        "--git-bin",
        default=api.project.DEFAULT_GIT_BIN,
        help="operator-approved absolute Git executable path",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=api.default_timeout_seconds,
        help="overall product-operation deadline after local envelope preflight",
    )
    parser.add_argument(
        "--allow-dirty-validator",
        action="store_true",
        help=(
            "explicitly allow a live product operation with modified "
            "non-executable profile data; sends journal the exact profile"
        ),
    )
    parser.add_argument(
        "--expected-validation-profile-sha256",
        help=("exact running profile digest required with a dirty-source override"),
    )
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
        parser_class=functools.partial(JsonArgumentParser, api),
    )

    subparsers.add_parser("doctor", help="check local transport prerequisites")
    subparsers.add_parser("claude-list", help="list eligible local Claude sessions")

    preflight_parser = subparsers.add_parser(
        "claude-preflight",
        help="map one full Claude sessionId to a fresh local ListAgents route",
    )
    preflight_parser.add_argument(
        "--participant",
        required=True,
        help="stable project-local roster name or participant UUID",
    )
    preflight_parser.add_argument(
        "--session-id",
        help="optional exact guard; the roster binding remains authoritative",
    )
    preflight_parser.add_argument(
        "--to", help="optional exact fresh name and ref to verify without retargeting"
    )

    send_parser = subparsers.add_parser(
        "claude-send", help="send one validated envelope to a local Claude session"
    )
    send_parser.add_argument(
        "--participant",
        required=True,
        help="stable project-local roster name or participant UUID",
    )
    send_parser.add_argument(
        "--to",
        help="optional exact fresh name and ref guard; never used as stable identity",
    )
    send_parser.add_argument(
        "--session-id",
        help="optional exact guard; the roster binding remains authoritative",
    )
    send_parser.add_argument(
        "--envelope",
        required=True,
        help="regular envelope file in the operator-approved private directory",
    )
    send_parser.add_argument(
        "--against",
        help="preserved root envelope file required for a reply",
    )
    send_parser.add_argument(
        "--renewal-of",
        help="expired unconfirmed root superseded by this fresh semantic renewal",
    )
    send_parser.add_argument(
        "--retry-after-intent",
        help="exact journal intent UUID for a proven not-attempted prior send",
    )
    send_parser.add_argument("--summary")

    reply_parser = subparsers.add_parser(
        "codex-send",
        aliases=["codex-reply"],
        help="queue one validated envelope to a roster-bound local Codex session",
    )
    reply_parser.add_argument(
        "--participant",
        required=True,
        help="stable project-local roster name or participant UUID",
    )
    reply_parser.add_argument(
        "--thread",
        help="optional exact guard; the roster binding remains authoritative",
    )
    reply_parser.add_argument(
        "--envelope",
        required=True,
        help="regular envelope file in the operator-approved private directory",
    )
    reply_parser.add_argument(
        "--against",
        help="preserved root envelope file required for a reply",
    )
    reply_parser.add_argument(
        "--renewal-of",
        help="expired unconfirmed root superseded by this fresh semantic renewal",
    )
    reply_parser.add_argument(
        "--retry-after-intent",
        help="exact journal intent UUID for a proven not-attempted prior send",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    api: TransportCliApi,
) -> int:
    args = build_parser(api).parse_args(argv)
    try:
        timeout_seconds = api.bounded_timeout(args.timeout_seconds)
        if args.command == "doctor":
            try:
                api.require_live_validation_profile(
                    allow_dirty=args.allow_dirty_validator,
                    expected_sha256=args.expected_validation_profile_sha256,
                )
            except api.transport_error as error:
                api.emit(
                    api.with_validation_profile(
                        {
                            "ok": False,
                            "status": "validation_profile_blocked",
                            "checks": {
                                "validation_profile": {
                                    "ok": False,
                                    "code": error.code,
                                    "detail": error.detail,
                                }
                            },
                            "live_path_configuration": {"ready": False},
                        }
                    )
                )
                return 2
            result = api.doctor(
                claude_bin=args.claude_bin,
                codex_bin=args.codex_bin,
                timeout_seconds=timeout_seconds,
            )
            result["checks"]["validation_profile"] = {"ok": True}
            api.emit(api.with_validation_profile(result))
            return 0 if result["ok"] else 2

        if args.command == "claude-list":
            api.require_live_validation_profile(
                allow_dirty=args.allow_dirty_validator,
                expected_sha256=args.expected_validation_profile_sha256,
            )
            claude_bin = api.resolve_binary(args.claude_bin, label="claude")
            protocol, local_peers, unavailable, excluded = asyncio.run(
                api.list_local_peers(
                    claude_bin=claude_bin, timeout_seconds=timeout_seconds
                )
            )
            api.emit(
                api.with_validation_profile(
                    {
                        "ok": True,
                        "local_only": True,
                        "mcp_protocol": protocol,
                        "agents": [peer.as_dict() for peer in local_peers],
                        "excluded_local_unavailable": [
                            peer.as_dict() for peer in unavailable
                        ],
                        "excluded_nonlocal_or_unknown": [
                            peer.as_dict() for peer in excluded
                        ],
                    }
                )
            )
            return 0

        if args.command == "claude-preflight":
            api.require_live_validation_profile(
                allow_dirty=args.allow_dirty_validator,
                expected_sha256=args.expected_validation_profile_sha256,
            )
            claude_bin = api.resolve_binary(args.claude_bin, label="claude")
            binding = api.resolve_project(args)
            result = asyncio.run(
                api.preflight_project_claude(
                    binding,
                    claude_bin=claude_bin,
                    participant_selector=args.participant,
                    session_id_guard=args.session_id,
                    target_guard=args.to,
                    timeout_seconds=timeout_seconds,
                )
            )
            api.emit(api.with_validation_profile(result))
            return 0

        if args.command == "claude-send":
            api.require_live_validation_profile(
                allow_dirty=args.allow_dirty_validator,
                expected_sha256=args.expected_validation_profile_sha256,
            )
            claude_bin = api.resolve_binary(args.claude_bin, label="claude")
            binding = api.resolve_project(args)
            result = asyncio.run(
                api.send_project_claude(
                    binding,
                    claude_bin=claude_bin,
                    participant_selector=args.participant,
                    session_id_guard=args.session_id,
                    target_guard=args.to,
                    envelope_path=args.envelope,
                    against_path=args.against,
                    renewal_of=args.renewal_of,
                    retry_after_intent=args.retry_after_intent,
                    summary=args.summary,
                    timeout_seconds=timeout_seconds,
                    allow_dirty_validator=args.allow_dirty_validator,
                    expected_validation_profile_sha256=(
                        args.expected_validation_profile_sha256
                    ),
                )
            )
            api.emit(api.with_validation_profile(result))
            return 0

        if args.command in {"codex-send", "codex-reply"}:
            api.require_live_validation_profile(
                allow_dirty=args.allow_dirty_validator,
                expected_sha256=args.expected_validation_profile_sha256,
            )
            codex_bin = api.resolve_binary(args.codex_bin, label="codex")
            binding = api.resolve_project(args)
            result = api.send_project_codex(
                binding,
                codex_bin=codex_bin,
                participant_selector=args.participant,
                thread_guard=args.thread,
                envelope_path=args.envelope,
                against_path=args.against,
                renewal_of=args.renewal_of,
                retry_after_intent=args.retry_after_intent,
                timeout_seconds=timeout_seconds,
                allow_dirty_validator=args.allow_dirty_validator,
                expected_validation_profile_sha256=(
                    args.expected_validation_profile_sha256
                ),
            )
            api.emit(api.with_validation_profile(result))
            return 0
    except (
        api.cam1.CamValidationError,
        api.cam1.CamUsageError,
        api.cam1.CliError,
        api.project.ProjectError,
    ) as error:
        if isinstance(error, api.cam1.CamValidationError):
            detail = [problem.as_dict() for problem in error.problems]
            api.emit(
                api.with_validation_profile(
                    {
                        "ok": False,
                        "error": {"code": "envelope.invalid", "problems": detail},
                    }
                ),
                stream=sys.stderr,
            )
        else:
            api.emit(
                api.with_validation_profile(
                    {
                        "ok": False,
                        "error": {"code": error.code, "detail": error.detail},
                    }
                ),
                stream=sys.stderr,
            )
        return 2
    except api.transport_error as error:
        payload: dict[str, Any] = {
            "ok": False,
            "error": {"code": error.code, "detail": error.detail},
        }
        if error.audit is not None:
            payload["audit"] = error.audit
        api.emit(api.with_validation_profile(payload), stream=sys.stderr)
        return 2
    except Exception as error:  # noqa: BLE001 - suppress raw transport internals
        api.emit(
            api.with_validation_profile(
                {
                    "ok": False,
                    "error": {
                        "code": "transport.internal",
                        "detail": f"unexpected transport failure ({type(error).__name__})",
                    },
                }
            ),
            stream=sys.stderr,
        )
        return 3
    return 3
