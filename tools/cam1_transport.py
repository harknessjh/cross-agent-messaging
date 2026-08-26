"""One-shot local transports for validated CAM/1 envelopes.

This module deliberately has no inbox, daemon, retry loop, database, or remote
transport.  It starts Claude Code's MCP server for one operation or invokes one
Codex queue command, reports the transport result, and exits.
"""

from __future__ import annotations

import argparse
import asyncio
import errno
import hashlib
import importlib.metadata
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from . import cam1
except ImportError:  # Direct execution adds tools/ rather than the repo to sys.path.
    import cam1  # type: ignore[no-redef]

DEFAULT_TIMEOUT_SECONDS = 20.0
MAX_TIMEOUT_SECONDS = 120.0
MAX_RECEIPT_TEXT = 4_096
MAX_RECEIPT_BLOCKS = 8
# Keep the one-shot public transport below common per-argument limits.  This is
# deliberately narrower than the 1 MiB offline CAM/1 envelope limit because
# ``codex queue`` currently receives the envelope as one argv value.
MAX_TRANSPORT_ENVELOPE_BYTES = 64 * 1_024
MIN_MCP_SDK_VERSION = (2, 1)
LOCAL_SESSION_KINDS = frozenset(
    {"background", "headless", "interactive", "non-interactive", "print"}
)
NONLOCAL_MARKERS = ("cloud", "remote control", "other machine")
PEER_NAME_PATTERN = re.compile(r"^(?P<name>.+?) \[(?P<ref>[0-9a-fA-F]{6})\]$")
UUID_TEXT = (
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}"
)
CODEX_QUEUE_RECEIPT_PATTERN = re.compile(
    rf"\AQueued message (?P<queue_id>{UUID_TEXT}) "
    rf"for thread (?P<thread_id>{UUID_TEXT})\.?\Z"
)


class TransportError(Exception):
    """A bounded, user-actionable transport failure."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code[:80]
        self.detail = detail[:500]
        super().__init__(self.detail)


@dataclass(frozen=True)
class Peer:
    """One row returned by Claude Code's ListAgents tool."""

    name: str
    ref: str
    kind: str
    state: str
    details: tuple[str, ...]
    local: bool

    @property
    def qualified_address(self) -> str:
        return f"{self.name} [{self.ref}]"

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ref": self.ref,
            "kind": self.kind,
            "state": self.state,
            "details": list(self.details),
            "local": self.local,
        }


@dataclass(frozen=True)
class ClaudeToolResponse:
    """Stable subset of an MCP tool result used by this helper."""

    protocol_version: str | None
    is_error: bool
    structured_content: Any
    text_content: tuple[str, ...]

    def receipt(self) -> dict[str, Any]:
        result: dict[str, Any] = {"is_error": self.is_error}
        if self.structured_content is not None:
            result["structured_content"] = _bounded_json_value(self.structured_content)
        if self.text_content:
            result["text_content"] = [
                text[:MAX_RECEIPT_TEXT]
                for text in self.text_content[:MAX_RECEIPT_BLOCKS]
            ]
        return result


def _find_transport_error(error: BaseException) -> TransportError | None:
    if isinstance(error, TransportError):
        return error
    if isinstance(error, BaseExceptionGroup):
        for nested in error.exceptions:
            found = _find_transport_error(nested)
            if found is not None:
                return found
    return None


def _bounded_json_value(value: Any) -> Any:
    try:
        serialized = json.dumps(value, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError):
        return {"omitted": "non-json transport result"}
    encoded = serialized.encode("utf-8")
    if len(encoded) <= MAX_RECEIPT_TEXT:
        return value
    return {
        "omitted": "oversized transport result",
        "bytes": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


def _emit(payload: dict[str, Any], *, stream: Any = sys.stdout) -> None:
    stream.write(json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n")


def _resolve_binary(value: str, *, label: str) -> str:
    candidate = shutil.which(value) if os.path.sep not in value else value
    if candidate is None:
        raise TransportError(f"{label}.not_found", f"{label} executable was not found")
    path = Path(candidate).expanduser()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise TransportError(
            f"{label}.not_executable", f"{label} path is not an executable file"
        )
    return str(path.resolve())


def _bounded_timeout(value: float) -> float:
    if not math.isfinite(value) or value <= 0 or value > MAX_TIMEOUT_SECONDS:
        raise TransportError(
            "argument.timeout",
            f"timeout-seconds must be greater than 0 and at most {MAX_TIMEOUT_SECONDS:g}",
        )
    return value


def _run_probe(
    command: list[str], timeout_seconds: float, *, required_text: Sequence[str] = ()
) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            shell=False,
            text=True,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"ok": False, "detail": "probe did not complete"}
    text = (completed.stdout or completed.stderr).strip()
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    content_matches = all(fragment in text for fragment in required_text)
    return {
        "ok": completed.returncode == 0 and content_matches,
        "exit_code": completed.returncode,
        "output": first_line[:300],
    }


def _run_probe_before(
    command: list[str], deadline: float, *, required_text: Sequence[str] = ()
) -> dict[str, Any]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return {"ok": False, "detail": "overall doctor timeout was exhausted"}
    return _run_probe(command, remaining, required_text=required_text)


def _mcp_sdk_check() -> tuple[bool, str | None]:
    try:
        sdk_version = importlib.metadata.version("mcp")
        match = re.match(r"^(\d+)\.(\d+)(?:\.|$)", sdk_version)
        sdk_supported = bool(
            match
            and int(match.group(1)) == 2
            and (int(match.group(1)), int(match.group(2))) >= MIN_MCP_SDK_VERSION
        )
        from mcp import Client, StdioServerParameters  # noqa: F401
        from mcp.client.stdio import stdio_client  # noqa: F401
    except (ImportError, importlib.metadata.PackageNotFoundError):
        return False, None
    return sdk_supported, sdk_version


def doctor(
    *, claude_bin: str, codex_bin: str, timeout_seconds: float
) -> dict[str, Any]:
    """Check installed prerequisites without opening a messaging session."""

    timeout_seconds = _bounded_timeout(timeout_seconds)
    deadline = time.monotonic() + timeout_seconds
    checks: dict[str, Any] = {}
    try:
        resolved_claude = _resolve_binary(claude_bin, label="claude")
        checks["claude"] = {
            "path": resolved_claude,
            "version": _run_probe_before([resolved_claude, "--version"], deadline),
            "mcp_serve": _run_probe_before(
                [resolved_claude, "mcp", "serve", "--help"],
                deadline,
                required_text=("claude mcp serve",),
            ),
        }
    except TransportError as error:
        checks["claude"] = {"ok": False, "error": error.code}

    try:
        resolved_codex = _resolve_binary(codex_bin, label="codex")
        checks["codex"] = {
            "path": resolved_codex,
            "version": _run_probe_before([resolved_codex, "--version"], deadline),
            "queue": _run_probe_before(
                [resolved_codex, "queue", "--help"],
                deadline,
                required_text=("--thread", "--message"),
            ),
        }
    except TransportError as error:
        checks["codex"] = {"ok": False, "error": error.code}

    sdk_supported, sdk_version = _mcp_sdk_check()
    checks["mcp_sdk"] = {"ok": sdk_supported, "version": sdk_version}

    claude = checks["claude"]
    codex = checks["codex"]
    ok = bool(
        sdk_supported
        and claude.get("version", {}).get("ok")
        and claude.get("mcp_serve", {}).get("ok")
        and codex.get("version", {}).get("ok")
        and codex.get("queue", {}).get("ok")
    )
    return {
        "ok": ok,
        "local_only": True,
        "checks": checks,
        "expected": {
            "claude": (
                "Claude Code with mcp serve; run claude-list to verify the "
                "cross-session tools"
            ),
            "codex": "Codex CLI with the send-only queue command",
            "mcp_sdk": "Python package mcp >=2.1,<3",
        },
    }


@asynccontextmanager
async def _claude_client(
    *, claude_bin: str, timeout_seconds: float
) -> AsyncIterator[tuple[Any, type[Any]]]:
    try:
        from mcp import Client, StdioServerParameters
        from mcp.client.stdio import stdio_client
        from mcp.types import Implementation, TextContent
    except ImportError as error:
        raise TransportError(
            "mcp_sdk.not_installed",
            "install the repository requirements before using Claude transport",
        ) from error

    settings = json.dumps({"isolatePeerMachines": True}, separators=(",", ":"))
    parameters = StdioServerParameters(
        command=claude_bin,
        args=["--settings", settings, "mcp", "serve"],
    )
    client_info = Implementation(name="cam1-local-transport", version="1")
    try:
        with open(os.devnull, "w", encoding="utf-8") as error_sink:
            transport = stdio_client(parameters, errlog=error_sink)
            async with Client(
                transport,
                read_timeout_seconds=timeout_seconds,
                client_info=client_info,
            ) as client:
                yield client, TextContent
    except Exception as error:
        transport_error = _find_transport_error(error)
        if transport_error is not None:
            raise transport_error from error
        raise TransportError(
            "claude.mcp_failure",
            f"Claude MCP operation failed ({type(error).__name__})",
        ) from error


async def _require_claude_tools(client: Any, *names: str) -> None:
    available = {tool.name for tool in (await client.list_tools()).tools}
    missing = [name for name in names if name not in available]
    if missing:
        raise TransportError(
            "claude.tool_unavailable",
            f"Claude MCP server does not expose {', '.join(missing)}",
        )


async def _call_connected_tool(
    client: Any,
    text_content_type: type[Any],
    *,
    tool_name: str,
    arguments: dict[str, Any],
) -> ClaudeToolResponse:
    result = await client.call_tool(tool_name, arguments)
    text_content = tuple(
        block.text for block in result.content if isinstance(block, text_content_type)
    )
    return ClaudeToolResponse(
        protocol_version=(
            str(client.protocol_version) if client.protocol_version else None
        ),
        is_error=bool(result.is_error),
        structured_content=result.structured_content,
        text_content=text_content,
    )


def _listing_text(response: ClaudeToolResponse) -> str:
    if response.is_error:
        raise TransportError(
            "claude.list_failed", "Claude ListAgents reported an error"
        )
    for text in response.text_content:
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            decoded = None
        if isinstance(decoded, dict) and isinstance(decoded.get("listing"), str):
            return decoded["listing"]
        if "Peer sessions" in text:
            return text
    raise TransportError(
        "claude.list_format",
        "Claude ListAgents returned an unrecognized response format",
    )


def parse_peers(listing: str) -> tuple[Peer, ...]:
    """Parse the documented human-readable ListAgents rows, failing closed."""

    peers: list[Peer] = []
    for raw_line in listing.splitlines():
        parts = tuple(part.strip() for part in raw_line.strip().split("·"))
        if len(parts) < 3:
            continue
        matched = PEER_NAME_PATTERN.fullmatch(parts[0])
        if matched is None:
            continue
        metadata = tuple(part for part in parts[1:] if part)
        kind = metadata[0] if metadata else ""
        state = metadata[1] if len(metadata) > 1 else ""
        metadata_text = " ".join(metadata).lower()
        local = kind.lower() in LOCAL_SESSION_KINDS and not any(
            marker in metadata_text for marker in NONLOCAL_MARKERS
        )
        peers.append(
            Peer(
                name=matched.group("name"),
                ref=matched.group("ref").lower(),
                kind=kind,
                state=state,
                details=metadata[2:],
                local=local,
            )
        )
    return tuple(peers)


async def list_local_peers(
    *, claude_bin: str, timeout_seconds: float
) -> tuple[str | None, tuple[Peer, ...], tuple[Peer, ...]]:
    try:
        async with asyncio.timeout(timeout_seconds):
            async with _claude_client(
                claude_bin=claude_bin, timeout_seconds=timeout_seconds
            ) as (client, text_content_type):
                await _require_claude_tools(client, "ListAgents")
                response = await _call_connected_tool(
                    client,
                    text_content_type,
                    tool_name="ListAgents",
                    arguments={},
                )
    except TimeoutError as error:
        raise TransportError(
            "claude.timeout", "Claude discovery exceeded the overall timeout"
        ) from error
    peers = parse_peers(_listing_text(response))
    return (
        response.protocol_version,
        tuple(peer for peer in peers if peer.local),
        tuple(peer for peer in peers if not peer.local),
    )


def _resolve_local_peer(target: str, peers: Sequence[Peer]) -> Peer:
    if not target or "\n" in target or "\r" in target or len(target) > 300:
        raise TransportError(
            "argument.target", "target must be a bounded single-line ListAgents address"
        )
    qualified = [peer for peer in peers if peer.qualified_address == target]
    if len(qualified) == 1:
        return qualified[0]
    bare = [peer for peer in peers if peer.name == target]
    if bare:
        raise TransportError(
            "claude.target_unqualified",
            "target must include the exact fresh name and ref from claude-list",
        )
    raise TransportError(
        "claude.target_not_local",
        "target was not found among freshly discovered local Claude sessions",
    )


def _validate_envelope(
    envelope_path: str, against_path: str | None
) -> tuple[bytes, dict[str, Any]]:
    raw = cam1.read_envelope_file(envelope_path)
    envelope = cam1.parse_exact_bytes(raw)
    message_type = envelope.get("type")
    if message_type in cam1.REPLY_TYPES and against_path is None:
        raise TransportError(
            "argument.against_required",
            "reply envelopes require --against with the preserved original envelope",
        )
    against_raw = cam1.read_envelope_file(against_path) if against_path else None
    result = cam1.validate_exact_bytes(raw, against_raw=against_raw)
    if len(raw) > MAX_TRANSPORT_ENVELOPE_BYTES:
        raise TransportError(
            "transport.payload_too_large",
            "validated envelope exceeds the 65536-byte live-transport limit; "
            "send a compact path-and-hash handoff instead",
        )
    return raw, result.envelope


def _default_summary(envelope: dict[str, Any]) -> str:
    return f"CAM/1 {envelope['type']} message {envelope['message_id']}"


def _validated_summary(value: str) -> str:
    if not value or "\n" in value or "\r" in value or len(value) > 200:
        raise TransportError(
            "argument.summary", "summary must be 1-200 characters on one line"
        )
    return value


def _direct_receipt_objects(response: ClaudeToolResponse) -> tuple[dict[str, Any], ...]:
    candidates: list[dict[str, Any]] = []
    if isinstance(response.structured_content, dict):
        candidates.append(response.structured_content)
    for text in response.text_content:
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(decoded, dict):
            candidates.append(decoded)

    unique: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        try:
            key = json.dumps(candidate, separators=(",", ":"), sort_keys=True)
        except (TypeError, ValueError):
            continue
        unique[key] = candidate
    return tuple(unique.values())


def _accepted_claude_message_id(response: ClaudeToolResponse) -> str:
    if response.is_error:
        raise TransportError(
            "claude.send_rejected", "Claude SendMessage reported an MCP tool error"
        )
    candidates = _direct_receipt_objects(response)
    if len(candidates) == 1 and candidates[0].get("success") is False:
        raise TransportError(
            "claude.send_rejected", "Claude SendMessage rejected the message"
        )
    if len(candidates) != 1 or candidates[0].get("success") is not True:
        raise TransportError(
            "claude.receipt_unrecognized",
            "Claude SendMessage returned no unambiguous success receipt; delivery "
            "state is unknown and must not be retried automatically",
        )
    message_id = candidates[0].get("msg_id")
    if not isinstance(message_id, str):
        raise TransportError(
            "claude.receipt_unrecognized",
            "Claude SendMessage success receipt omitted a canonical msg_id; "
            "delivery state is unknown and must not be retried automatically",
        )
    try:
        canonical = str(uuid.UUID(message_id))
    except ValueError:
        canonical = ""
    if canonical != message_id:
        raise TransportError(
            "claude.receipt_unrecognized",
            "Claude SendMessage success receipt contained a noncanonical msg_id; "
            "delivery state is unknown and must not be retried automatically",
        )
    return canonical


async def send_to_claude(
    *,
    claude_bin: str,
    target: str,
    envelope_path: str,
    against_path: str | None,
    summary: str | None,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Validate and send one exact envelope to one freshly discovered local peer."""

    raw, envelope = _validate_envelope(envelope_path, against_path)
    try:
        async with asyncio.timeout(timeout_seconds):
            async with _claude_client(
                claude_bin=claude_bin, timeout_seconds=timeout_seconds
            ) as (client, text_content_type):
                await _require_claude_tools(client, "ListAgents", "SendMessage")
                listing_response = await _call_connected_tool(
                    client,
                    text_content_type,
                    tool_name="ListAgents",
                    arguments={},
                )
                peers = parse_peers(_listing_text(listing_response))
                local_peers = tuple(peer for peer in peers if peer.local)
                peer = _resolve_local_peer(target, local_peers)
                recipient = envelope.get("recipient", {})
                if recipient.get("agent_name") != peer.name:
                    raise TransportError(
                        "envelope.recipient_mismatch",
                        "envelope recipient.agent_name must equal the freshly "
                        "discovered peer name",
                    )
                transport_address = peer.qualified_address
                response = await _call_connected_tool(
                    client,
                    text_content_type,
                    tool_name="SendMessage",
                    arguments={
                        "to": transport_address,
                        "summary": _validated_summary(
                            summary
                            if summary is not None
                            else _default_summary(envelope)
                        ),
                        "message": raw.decode("utf-8"),
                    },
                )
    except TimeoutError as error:
        raise TransportError(
            "claude.timeout", "Claude send exceeded the overall timeout"
        ) from error
    transport_message_id = _accepted_claude_message_id(response)
    return {
        "ok": True,
        "status": "transport_accepted",
        "application_ack": False,
        "local_only": True,
        "target": transport_address,
        "target_ref": peer.ref,
        "message_id": envelope["message_id"],
        "transport_message_id": transport_message_id,
        "mcp_protocol": response.protocol_version,
        "transport_receipt": response.receipt(),
    }


def _canonical_uuid(value: str, *, label: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, ValueError):
        raise TransportError(
            f"argument.{label}", f"{label} must be a canonical UUID"
        ) from None
    canonical = str(parsed)
    if canonical != value:
        raise TransportError(
            f"argument.{label}", f"{label} must be a lowercase canonical UUID"
        )
    return canonical


def _codex_queue_receipt(stdout: str, *, expected_thread: str) -> dict[str, str]:
    receipt_text = stdout.strip()
    receipt_match = CODEX_QUEUE_RECEIPT_PATTERN.fullmatch(receipt_text)
    if receipt_match is None or receipt_match.group("thread_id") != expected_thread:
        raise TransportError(
            "codex.receipt_unrecognized",
            "Codex queue returned no exact receipt for the requested thread; delivery "
            "state is unknown and must not be retried automatically",
        )
    return {
        "queue_id": receipt_match.group("queue_id"),
        "thread_id": receipt_match.group("thread_id"),
        "text": receipt_text[:MAX_RECEIPT_TEXT],
    }


def reply_to_codex(
    *,
    codex_bin: str,
    thread: str,
    envelope_path: str,
    against_path: str | None,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Validate and queue one exact envelope to one literal local Codex thread."""

    thread = _canonical_uuid(thread, label="thread")
    raw, envelope = _validate_envelope(envelope_path, against_path)
    recipient_session = envelope.get("recipient", {}).get("session_id")
    if recipient_session != thread:
        raise TransportError(
            "envelope.recipient_mismatch",
            "envelope recipient.session_id must equal the literal Codex thread UUID",
        )
    try:
        completed = subprocess.run(
            [
                codex_bin,
                "queue",
                "--thread",
                thread,
                "--message",
                raw.decode("utf-8"),
            ],
            check=False,
            capture_output=True,
            shell=False,
            text=True,
            timeout=timeout_seconds,
        )
    except OSError as error:
        if error.errno == errno.E2BIG:
            raise TransportError(
                "transport.payload_too_large",
                "operating system rejected the Codex queue argument size; send a "
                "compact path-and-hash handoff instead",
            ) from error
        raise TransportError(
            "codex.queue_failure", "Codex queue command did not complete"
        ) from error
    except subprocess.TimeoutExpired as error:
        raise TransportError(
            "codex.queue_failure", "Codex queue command did not complete"
        ) from error
    if completed.returncode != 0:
        raise TransportError(
            "codex.queue_rejected",
            f"Codex queue exited with status {completed.returncode}",
        )
    transport_receipt = _codex_queue_receipt(completed.stdout, expected_thread=thread)
    return {
        "ok": True,
        "status": "transport_accepted",
        "application_ack": False,
        "local_only": True,
        "target_thread": thread,
        "message_id": envelope["message_id"],
        "transport_receipt": transport_receipt,
    }


class JsonArgumentParser(argparse.ArgumentParser):
    """Keep command-line failures on the documented JSON error channel."""

    def error(self, message: str) -> None:
        _emit(
            {
                "ok": False,
                "error": {"code": "argument.invalid", "detail": message[:500]},
            },
            stream=sys.stderr,
        )
        raise SystemExit(2)


def _parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(
        description="Use Claude Code's local messaging transport for CAM/1 envelopes."
    )
    parser.add_argument("--claude-bin", default="claude")
    parser.add_argument("--codex-bin", default="codex")
    parser.add_argument(
        "--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("doctor", help="check local transport prerequisites")
    subparsers.add_parser("claude-list", help="list eligible local Claude sessions")

    send_parser = subparsers.add_parser(
        "claude-send", help="send one validated envelope to a local Claude session"
    )
    send_parser.add_argument("--to", required=True)
    send_parser.add_argument("--envelope", required=True)
    send_parser.add_argument("--against")
    send_parser.add_argument("--summary")

    reply_parser = subparsers.add_parser(
        "codex-reply", help="queue one validated envelope to a local Codex session"
    )
    reply_parser.add_argument("--thread", required=True)
    reply_parser.add_argument("--envelope", required=True)
    reply_parser.add_argument("--against")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        timeout_seconds = _bounded_timeout(args.timeout_seconds)
        if args.command == "doctor":
            result = doctor(
                claude_bin=args.claude_bin,
                codex_bin=args.codex_bin,
                timeout_seconds=timeout_seconds,
            )
            _emit(result)
            return 0 if result["ok"] else 2

        if args.command == "claude-list":
            claude_bin = _resolve_binary(args.claude_bin, label="claude")
            protocol, local_peers, excluded = asyncio.run(
                list_local_peers(claude_bin=claude_bin, timeout_seconds=timeout_seconds)
            )
            _emit(
                {
                    "ok": True,
                    "local_only": True,
                    "mcp_protocol": protocol,
                    "agents": [peer.as_dict() for peer in local_peers],
                    "excluded_nonlocal_or_unknown": [
                        peer.as_dict() for peer in excluded
                    ],
                }
            )
            return 0

        if args.command == "claude-send":
            claude_bin = _resolve_binary(args.claude_bin, label="claude")
            result = asyncio.run(
                send_to_claude(
                    claude_bin=claude_bin,
                    target=args.to,
                    envelope_path=args.envelope,
                    against_path=args.against,
                    summary=args.summary,
                    timeout_seconds=timeout_seconds,
                )
            )
            _emit(result)
            return 0

        if args.command == "codex-reply":
            codex_bin = _resolve_binary(args.codex_bin, label="codex")
            result = reply_to_codex(
                codex_bin=codex_bin,
                thread=args.thread,
                envelope_path=args.envelope,
                against_path=args.against,
                timeout_seconds=timeout_seconds,
            )
            _emit(result)
            return 0
    except (cam1.CamValidationError, cam1.CliError) as error:
        if isinstance(error, cam1.CamValidationError):
            detail = [problem.as_dict() for problem in error.problems]
            _emit(
                {
                    "ok": False,
                    "error": {"code": "envelope.invalid", "problems": detail},
                },
                stream=sys.stderr,
            )
        else:
            _emit(
                {"ok": False, "error": {"code": error.code, "detail": error.detail}},
                stream=sys.stderr,
            )
        return 2
    except TransportError as error:
        _emit(
            {"ok": False, "error": {"code": error.code, "detail": error.detail}},
            stream=sys.stderr,
        )
        return 2
    except Exception as error:  # noqa: BLE001 - suppress raw transport internals
        _emit(
            {
                "ok": False,
                "error": {
                    "code": "transport.internal",
                    "detail": f"unexpected transport failure ({type(error).__name__})",
                },
            },
            stream=sys.stderr,
        )
        return 3
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
