# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Native one-shot Claude MCP and Codex queue transport primitives."""

from __future__ import annotations

import asyncio
import datetime as dt
import errno
import hashlib
import importlib.metadata
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from builtins import BaseExceptionGroup
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if __package__:
    from . import cam1
    from .cam1lib import project, routing, state
else:  # Direct execution adds tools/ rather than the repo to sys.path.
    import cam1  # type: ignore[no-redef]
    from cam1lib import project, routing, state  # type: ignore[no-redef]

DEFAULT_TIMEOUT_SECONDS = 20.0
MAX_TIMEOUT_SECONDS = 120.0
MAX_RECEIPT_TEXT = 4_096
MAX_RECEIPT_BLOCKS = 8
# Keep the one-shot public transport below common per-argument limits.  This is
# deliberately narrower than the 1 MiB offline CAM/1 envelope limit because
# ``codex queue`` currently receives the envelope as one argv value.
MAX_TRANSPORT_ENVELOPE_BYTES = 64 * 1_024
MIN_MCP_SDK_VERSION = (2, 1)
LOCAL_SESSION_KINDS = routing.LOCAL_SESSION_KINDS
NONLOCAL_MARKERS = routing.NONLOCAL_MARKERS
PEER_NAME_PATTERN = routing.PEER_NAME_PATTERN
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

    def __init__(
        self,
        code: str,
        detail: str,
        *,
        audit: dict[str, Any] | None = None,
    ) -> None:
        self.code = code[:80]
        self.detail = detail[:500]
        self.audit = audit
        super().__init__(self.detail)


Peer = routing.Peer


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


@dataclass(frozen=True)
class ValidatedEnvelope:
    """Exact live payload plus its validated envelope and optional root."""

    raw: bytes
    envelope: dict[str, Any]
    original: dict[str, Any] | None
    original_raw: bytes | None


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


def _with_validation_profile(
    payload: dict[str, Any],
    *,
    validation_profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        **payload,
        "validation_profile": validation_profile
        if validation_profile is not None
        else cam1.validation_profile_report(),
    }


def _require_live_validation_profile(
    *,
    allow_dirty: bool,
    expected_sha256: str | None,
) -> tuple[dict[str, Any], bool]:
    try:
        current = cam1.require_live_profile(
            allow_dirty=allow_dirty,
            expected_sha256=expected_sha256,
        )
    except cam1.ValidationProfileError as error:
        raise TransportError(error.code, error.detail) from error
    profile_report = {"available": True, **current.as_dict()}
    override_used = bool(
        allow_dirty
        and current.source_control.kind == "git"
        and current.source_control.dirty
    )
    return profile_report, override_used


def _resolve_binary(value: str, *, label: str, allow_path_lookup: bool = False) -> str:
    supplied = Path(value).expanduser()
    if not supplied.is_absolute() and not allow_path_lookup:
        raise TransportError(
            f"{label}.absolute_path_required",
            f"live {label} operations require an operator-approved absolute path",
        )
    candidate = shutil.which(value) if os.path.sep not in value else str(supplied)
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


def _agent_view_probe_before(claude_bin: str, deadline: float) -> dict[str, Any]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return {"ok": False, "detail": "overall doctor timeout was exhausted"}
    try:
        completed = subprocess.run(
            [claude_bin, "agents", "--json"],
            check=False,
            capture_output=True,
            shell=False,
            timeout=remaining,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"ok": False, "detail": "Agent View probe did not complete"}
    if completed.returncode != 0:
        return {"ok": False, "exit_code": completed.returncode}
    try:
        sessions = routing.parse_agent_view_sessions(completed.stdout)
    except routing.RoutingError as error:
        return {"ok": False, "detail": error.detail}
    return {"ok": True, "sessions": len(sessions)}


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
    *,
    claude_bin: str,
    codex_bin: str,
    timeout_seconds: float,
    _facade: Any | None = None,
) -> dict[str, Any]:
    """Check installed prerequisites without opening a messaging session."""

    facade = _facade if _facade is not None else sys.modules[__name__]
    timeout_seconds = facade._bounded_timeout(timeout_seconds)
    deadline = time.monotonic() + timeout_seconds
    checks: dict[str, Any] = {}
    try:
        resolved_claude = facade._resolve_binary(
            claude_bin, label="claude", allow_path_lookup=True
        )
        checks["claude"] = {
            "path": resolved_claude,
            "version": facade._run_probe_before(
                [resolved_claude, "--version"], deadline
            ),
            "mcp_serve": facade._run_probe_before(
                [resolved_claude, "mcp", "serve", "--help"],
                deadline,
                required_text=("claude mcp serve",),
            ),
            "agent_view": facade._agent_view_probe_before(resolved_claude, deadline),
        }
    except TransportError as error:
        checks["claude"] = {"ok": False, "error": error.code}

    try:
        resolved_codex = facade._resolve_binary(
            codex_bin, label="codex", allow_path_lookup=True
        )
        checks["codex"] = {
            "path": resolved_codex,
            "version": facade._run_probe_before(
                [resolved_codex, "--version"], deadline
            ),
            "queue": facade._run_probe_before(
                [resolved_codex, "queue", "--help"],
                deadline,
                required_text=("--thread", "--message"),
            ),
        }
    except TransportError as error:
        checks["codex"] = {"ok": False, "error": error.code}

    sdk_supported, sdk_version = facade._mcp_sdk_check()
    checks["mcp_sdk"] = {"ok": sdk_supported, "version": sdk_version}

    claude = checks["claude"]
    codex = checks["codex"]
    prerequisites_ok = bool(
        sdk_supported
        and claude.get("version", {}).get("ok")
        and claude.get("mcp_serve", {}).get("ok")
        and claude.get("agent_view", {}).get("ok")
        and codex.get("version", {}).get("ok")
        and codex.get("queue", {}).get("ok")
    )
    resolved_claude = claude.get("path")
    resolved_codex = codex.get("path")
    explicit_paths = (
        Path(claude_bin).expanduser().is_absolute()
        and Path(codex_bin).expanduser().is_absolute()
    )
    required_arguments = (
        [
            "--claude-bin",
            resolved_claude,
            "--codex-bin",
            resolved_codex,
        ]
        if isinstance(resolved_claude, str) and isinstance(resolved_codex, str)
        else None
    )
    ok = prerequisites_ok and explicit_paths
    return {
        "ok": ok,
        "status": (
            "ready"
            if ok
            else (
                "operator_path_confirmation_required"
                if prerequisites_ok and not explicit_paths
                else "failed"
            )
        ),
        "prerequisites_ok": prerequisites_ok,
        "local_only": True,
        "checks": checks,
        "live_path_configuration": {
            "operator_approval_required": True,
            "explicit_absolute_paths_supplied": explicit_paths,
            "required_global_arguments": required_arguments,
            "copy_paste_flags": (
                shlex.join(required_arguments)
                if required_arguments is not None
                else None
            ),
            "ready": ok,
        },
        "expected": {
            "claude": (
                "Claude Code with Agent View JSON and mcp serve; run claude-list "
                "to verify the cross-session tools"
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
        error_descriptor = os.open(os.devnull, os.O_WRONLY)
        with os.fdopen(error_descriptor, "w", encoding="utf-8") as error_sink:
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


async def _require_claude_tools(client: Any, *names: str) -> dict[str, dict[str, Any]]:
    available = {tool.name: tool for tool in (await client.list_tools()).tools}
    missing = [name for name in names if name not in available]
    if missing:
        raise TransportError(
            "claude.tool_unavailable",
            f"Claude MCP server does not expose {', '.join(missing)}",
        )
    return {
        name: (
            available[name].input_schema
            if isinstance(available[name].input_schema, dict)
            else {}
        )
        for name in names
    }


def _schema_accepts_boolean(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    declared_type = value.get("type")
    if declared_type == "boolean" or (
        isinstance(declared_type, list) and "boolean" in declared_type
    ):
        return True
    return any(
        _schema_accepts_boolean(candidate)
        for keyword in ("anyOf", "oneOf")
        for candidate in (
            value.get(keyword) if isinstance(value.get(keyword), list) else []
        )
    )


def _supports_notify_when_idle(send_schema: dict[str, Any]) -> bool:
    properties = send_schema.get("properties")
    return isinstance(properties, dict) and _schema_accepts_boolean(
        properties.get("notify_when_idle")
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

    try:
        return routing.parse_list_agents_peers(listing)
    except routing.RoutingError as error:
        raise TransportError(error.code, error.detail) from error


async def list_local_peers(
    *, claude_bin: str, timeout_seconds: float
) -> tuple[
    str | None,
    tuple[Peer, ...],
    tuple[Peer, ...],
    tuple[Peer, ...],
]:
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
        tuple(peer for peer in peers if peer.local and peer.addressable),
        tuple(peer for peer in peers if peer.local and not peer.addressable),
        tuple(peer for peer in peers if not peer.local),
    )


def _resolve_local_peer(target: str, peers: Sequence[Peer]) -> Peer:
    try:
        return routing.resolve_list_agents_target(target, tuple(peers))
    except routing.RoutingError as error:
        raise TransportError(error.code, error.detail) from error


def _discover_agent_view_sessions(
    *, claude_bin: str, timeout_seconds: float
) -> dict[str, tuple[routing.AgentViewSession, ...]]:
    """Run one bounded full-session discovery without retaining runtime endpoints."""

    try:
        completed = subprocess.run(
            [claude_bin, "agents", "--json"],
            check=False,
            capture_output=True,
            shell=False,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise TransportError(
            "claude.agents_failure", "claude agents discovery did not complete"
        ) from error
    if completed.returncode != 0:
        raise TransportError(
            "claude.agents_failure",
            f"claude agents exited with status {completed.returncode}",
        )
    try:
        return routing.parse_agent_view_sessions(completed.stdout)
    except routing.RoutingError as error:
        raise TransportError(error.code, error.detail) from error


def _select_agent_view_session(
    sessions: dict[str, tuple[routing.AgentViewSession, ...]], session_id: str
) -> routing.AgentViewSession:
    try:
        return routing.select_agent_view_session(sessions, session_id)
    except routing.RoutingError as error:
        raise TransportError(error.code, error.detail) from error


def _correlate_route(
    session: routing.AgentViewSession,
    peers: Sequence[Peer],
    *,
    requested_target: str | None,
) -> routing.ClaudeRoute:
    try:
        return routing.correlate_route(
            session,
            tuple(peers),
            requested_target=requested_target,
        )
    except routing.RoutingError as error:
        raise TransportError(error.code, error.detail) from error


_STABLE_AGENT_VIEW_FIELDS = (
    "session_id",
    "agent_view_id",
    "product_name",
    "cwd",
    "kind",
    "started_at_ms",
    "process_id",
)


async def _refresh_agent_view_session(
    selected: routing.AgentViewSession,
    *,
    claude_bin: str,
    timeout_seconds: float,
) -> routing.AgentViewSession:
    """Recheck stable Agent View identity after MCP route correlation."""

    sessions = await asyncio.to_thread(
        _discover_agent_view_sessions,
        claude_bin=claude_bin,
        timeout_seconds=timeout_seconds,
    )
    refreshed = _select_agent_view_session(sessions, selected.session_id)
    changed = [
        field_name
        for field_name in _STABLE_AGENT_VIEW_FIELDS
        if getattr(selected, field_name) != getattr(refreshed, field_name)
    ]
    if changed:
        raise TransportError(
            "claude.session_changed",
            "Claude Agent View identity changed during route discovery",
        )
    return refreshed


def _require_project_session_cwd(
    binding: project.ProjectBinding,
    session: routing.AgentViewSession,
) -> project.GitContext:
    """Resolve a live Claude cwd and require the bound Git project identity."""

    candidate = Path(session.cwd).expanduser()
    if not candidate.is_absolute():
        raise TransportError(
            "claude.project_mismatch",
            "Claude Agent View cwd is not an absolute project path",
        )
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError, ValueError, UnicodeError):
        raise TransportError(
            "claude.project_mismatch",
            "Claude Agent View cwd could not be resolved",
        ) from None
    if not resolved.is_dir():
        raise TransportError(
            "claude.project_mismatch",
            "Claude Agent View cwd is not a directory",
        )
    try:
        session_context = project.discover_git_context(
            resolved,
            git_bin=binding.git_bin,
        )
    except project.ProjectError as error:
        raise TransportError(
            "claude.project_mismatch",
            "Claude session cwd is not an eligible Git worktree",
        ) from error
    if session_context.common_dir != binding.git_common_dir:
        raise TransportError(
            "claude.project_mismatch",
            "Claude session belongs to a different Git project",
        )
    return session_context


def _validate_envelope(
    envelope_path: str, against_path: str | None
) -> ValidatedEnvelope:
    if envelope_path == "-" or against_path == "-":
        raise TransportError(
            "argument.envelope_file",
            "live transport requires bounded regular files; stdin is offline-only",
        )
    raw = cam1.read_private_envelope_file(envelope_path)
    against_raw = (
        cam1.read_private_envelope_file(against_path) if against_path else None
    )
    parsed = cam1.parse_exact_bytes(raw)
    message_type = parsed.get("type")
    if message_type in cam1.REPLY_TYPES and against_raw is None:
        raise TransportError(
            "argument.against_required",
            "reply envelopes require --against with the preserved original envelope",
        )
    if message_type == "cancel" and against_raw is None:
        raise TransportError(
            "argument.against_required",
            "cancel envelopes require --against with the preserved request envelope",
        )
    if message_type == "cancel":
        envelope, original = state.validate_cancel_exact_bytes(raw, against_raw)
    else:
        result = cam1.validate_exact_bytes(raw, against_raw=against_raw)
        envelope = result.envelope
        original = (
            cam1.parse_exact_bytes(against_raw) if against_raw is not None else None
        )
    if len(raw) > MAX_TRANSPORT_ENVELOPE_BYTES:
        raise TransportError(
            "transport.payload_too_large",
            "validated envelope exceeds the 65536-byte live-transport limit; "
            "send a compact path-and-hash handoff instead",
        )
    return ValidatedEnvelope(
        raw=raw,
        envelope=envelope,
        original=original,
        original_raw=against_raw,
    )


def _require_original_callback(
    validated: ValidatedEnvelope,
    *,
    transport: str,
    address: str,
) -> None:
    if validated.envelope["type"] not in cam1.REPLY_TYPES:
        return
    original = validated.original
    if original is None:
        raise TransportError(
            "argument.against_required",
            "reply envelopes require --against with the preserved original envelope",
        )
    reply_to = original["reply_to"]
    if not isinstance(reply_to, dict):
        raise TransportError(
            "envelope.callback_unavailable",
            "preserved original does not provide a live reply_to route",
        )
    address_matches = _uuid_values_equal(reply_to["address"], address)
    if reply_to["transport"] != transport or not address_matches:
        raise TransportError(
            "envelope.callback_mismatch",
            "live reply target must exactly match the preserved original reply_to",
        )


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
            "claude.send_failed",
            "Claude SendMessage reported an MCP tool error; delivery state is unknown",
        )
    candidates = _direct_receipt_objects(response)
    if len(candidates) == 1 and candidates[0].get("success") is False:
        raise TransportError(
            "claude.send_failed",
            "Claude SendMessage returned success=false; delivery state is unknown",
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


async def _preflight_claude_session(
    *,
    claude_bin: str,
    session_id: str,
    target: str | None,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Correlate a full sessionId to one unique fresh ListAgents route."""

    try:
        async with asyncio.timeout(timeout_seconds):
            sessions = await asyncio.to_thread(
                _discover_agent_view_sessions,
                claude_bin=claude_bin,
                timeout_seconds=timeout_seconds,
            )
            selected = _select_agent_view_session(sessions, session_id)
            async with _claude_client(
                claude_bin=claude_bin, timeout_seconds=timeout_seconds
            ) as (client, text_content_type):
                schemas = await _require_claude_tools(
                    client, "ListAgents", "SendMessage"
                )
                listing_response = await _call_connected_tool(
                    client,
                    text_content_type,
                    tool_name="ListAgents",
                    arguments={},
                )
                peers = parse_peers(_listing_text(listing_response))
                local_peers = tuple(
                    peer for peer in peers if peer.local and peer.addressable
                )
                route = _correlate_route(
                    selected,
                    local_peers,
                    requested_target=target,
                )
                selected = await _refresh_agent_view_session(
                    selected,
                    claude_bin=claude_bin,
                    timeout_seconds=timeout_seconds,
                )
                route = routing.ClaudeRoute(session=selected, peer=route.peer)
    except TimeoutError as error:
        raise TransportError(
            "claude.timeout", "Claude route preflight exceeded the overall timeout"
        ) from error
    return {
        "ok": True,
        "status": "route_preflight",
        "local_only": True,
        "mcp_protocol": listing_response.protocol_version,
        "identity": selected.as_dict(),
        "route": route.as_dict(),
        "notify_when_idle_supported": _supports_notify_when_idle(
            schemas["SendMessage"]
        ),
        "operator_correlation_required": True,
    }


async def _send_to_claude(
    *,
    claude_bin: str,
    target: str | None,
    session_id: str,
    envelope_path: str,
    against_path: str | None,
    summary: str | None,
    timeout_seconds: float,
    before_send: Callable[[ValidatedEnvelope, routing.ClaudeRoute], None],
) -> dict[str, Any]:
    """Resolve a full session UUID and send to its fresh local peer route."""

    validated = _validate_envelope(envelope_path, against_path)
    raw = validated.raw
    envelope = validated.envelope
    validated_summary = _validated_summary(
        summary if summary is not None else _default_summary(envelope)
    )
    try:
        async with asyncio.timeout(timeout_seconds):
            sessions = await asyncio.to_thread(
                _discover_agent_view_sessions,
                claude_bin=claude_bin,
                timeout_seconds=timeout_seconds,
            )
            selected = _select_agent_view_session(sessions, session_id)
            async with _claude_client(
                claude_bin=claude_bin, timeout_seconds=timeout_seconds
            ) as (client, text_content_type):
                schemas = await _require_claude_tools(
                    client, "ListAgents", "SendMessage"
                )
                listing_response = await _call_connected_tool(
                    client,
                    text_content_type,
                    tool_name="ListAgents",
                    arguments={},
                )
                peers = parse_peers(_listing_text(listing_response))
                local_peers = tuple(
                    peer for peer in peers if peer.local and peer.addressable
                )
                route = _correlate_route(
                    selected,
                    local_peers,
                    requested_target=target,
                )
                selected = await _refresh_agent_view_session(
                    selected,
                    claude_bin=claude_bin,
                    timeout_seconds=timeout_seconds,
                )
                route = routing.ClaudeRoute(session=selected, peer=route.peer)
                peer = route.peer
                recipient = envelope.get("recipient", {})
                if recipient.get("vendor") != "claude-code":
                    raise TransportError(
                        "envelope.recipient_mismatch",
                        "Claude transport requires recipient.vendor=claude-code",
                    )
                if not _uuid_values_equal(
                    recipient.get("session_id"), selected.session_id
                ):
                    raise TransportError(
                        "envelope.recipient_mismatch",
                        "envelope recipient.session_id must equal the selected full "
                        "Claude sessionId",
                    )
                transport_address = peer.qualified_address
                _require_original_callback(
                    validated,
                    transport="claude_send_message",
                    address=selected.session_id,
                )
                before_send(validated, route)
                send_arguments: dict[str, Any] = {
                    "to": transport_address,
                    "summary": validated_summary,
                    "message": raw.decode("utf-8"),
                }
                notify_when_idle = _supports_notify_when_idle(schemas["SendMessage"])
                if notify_when_idle:
                    send_arguments["notify_when_idle"] = True
                response = await _call_connected_tool(
                    client,
                    text_content_type,
                    tool_name="SendMessage",
                    arguments=send_arguments,
                )
    except TimeoutError as error:
        raise TransportError(
            "claude.timeout", "Claude send exceeded the overall timeout"
        ) from error
    transport_message_id = _accepted_claude_message_id(response)
    result = {
        "ok": True,
        "status": "transport_accepted",
        "application_ack": False,
        "local_only": True,
        "target": transport_address,
        "target_ref": peer.ref,
        "message_id": envelope["message_id"],
        "transport_message_id": transport_message_id,
        "mcp_protocol": response.protocol_version,
        "notify_when_idle_requested": notify_when_idle,
        "transport_receipt": response.receipt(),
    }
    result["target_session_id"] = selected.session_id
    result["target_agent_view_id"] = selected.agent_view_id
    return result


def _canonical_uuid(value: str, *, label: str) -> str:
    if not isinstance(value, str):
        raise TransportError(f"argument.{label}", f"{label} must be a valid UUID")
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        raise TransportError(
            f"argument.{label}", f"{label} must be a valid UUID"
        ) from None
    canonical = str(parsed)
    if value.lower() != canonical:
        raise TransportError(
            f"argument.{label}",
            f"{label} must use canonical 8-4-4-4-12 UUID spelling",
        )
    return canonical


def _uuid_values_equal(left: Any, right: Any) -> bool:
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    try:
        return uuid.UUID(left) == uuid.UUID(right)
    except (ValueError, AttributeError):
        return False


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


def _send_to_codex_queue(
    *,
    codex_bin: str,
    thread: str,
    envelope_path: str,
    against_path: str | None,
    timeout_seconds: float,
    before_send: Callable[[ValidatedEnvelope], None],
) -> dict[str, Any]:
    """Validate and queue one exact envelope to one literal local Codex thread."""

    thread = _canonical_uuid(thread, label="thread")
    validated = _validate_envelope(envelope_path, against_path)
    raw = validated.raw
    envelope = validated.envelope
    recipient = envelope.get("recipient", {})
    if recipient.get("vendor") != "codex":
        raise TransportError(
            "envelope.recipient_mismatch",
            "Codex queue transport requires recipient.vendor=codex",
        )
    recipient_session = recipient.get("session_id")
    if not _uuid_values_equal(recipient_session, thread):
        raise TransportError(
            "envelope.recipient_mismatch",
            "envelope recipient.session_id must equal the literal Codex thread UUID",
        )
    _require_original_callback(
        validated,
        transport="codex_queue",
        address=thread,
    )
    before_send(validated)
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
            "codex.queue_failed",
            f"Codex queue exited with status {completed.returncode}; delivery state is unknown",
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


def _utc_now() -> tuple[dt.datetime, str]:
    observed = dt.datetime.now(dt.UTC)
    timespec = "microseconds" if observed.microsecond else "seconds"
    return observed, observed.isoformat(timespec=timespec).replace("+00:00", "Z")


def _record_summary(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "record_id": record["record_id"],
        "sequence": record["sequence"],
        "event_type": record["event_type"],
        "record_sha256": record["record_sha256"],
    }


def _resolve_project(args: Any) -> project.ProjectBinding:
    return project.resolve_project(
        args.project_root,
        state_root=args.state_root,
        git_bin=args.git_bin,
    )


def _domain_transport_error(error: Exception) -> TransportError:
    if isinstance(error, cam1.CamValidationError):
        codes = list(dict.fromkeys(problem.code for problem in error.problems))[:8]
        suffix = f" ({', '.join(codes)})" if codes else ""
        return TransportError(
            "envelope.invalid",
            f"envelope failed project lifecycle validation{suffix}",
        )
    code = getattr(error, "code", "project.invalid")
    detail = getattr(error, "detail", "project state operation failed")
    return TransportError(str(code), str(detail))


def _require_session_guard(
    supplied: str | None,
    expected: str,
    *,
    label: str,
) -> None:
    if supplied is None:
        return
    if _canonical_uuid(supplied, label=label) != expected:
        raise TransportError(
            f"argument.{label}_mismatch",
            f"{label} guard does not equal the participant's bound session UUID",
        )


def _delivery_state(error: TransportError, attempt: Any) -> str:
    if not attempt.dispatch_started or error.code == "transport.payload_too_large":
        return "not_attempted"
    return "unknown"
