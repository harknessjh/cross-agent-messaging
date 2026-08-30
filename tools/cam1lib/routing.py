# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Bounded parsing and exact matching for local Claude session discovery."""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from typing import Any

MAX_AGENT_VIEW_BYTES = 1_048_576
MAX_AGENT_VIEW_SESSIONS = 512
MAX_LIST_AGENTS_CHARS = 262_144
MAX_LIST_AGENTS_PEERS = 512
LOCAL_SESSION_KINDS = frozenset(
    {"background", "headless", "interactive", "non-interactive", "print"}
)
ADDRESSABLE_SESSION_STATES = frozenset({"busy", "idle", "running", "waiting"})
NONLOCAL_MARKERS = ("cloud", "remote control", "other machine")
PEER_NAME_PATTERN = re.compile(r"^(?P<name>.+?) \[(?P<ref>[0-9a-fA-F]{6})\]$")
AGENT_VIEW_ID_PATTERN = re.compile(r"^[0-9a-fA-F]{8}$")


class RoutingError(ValueError):
    """A bounded discovery or route-selection failure."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code[:80]
        self.detail = detail[:500]
        super().__init__(self.detail)


@dataclass(frozen=True, slots=True)
class AgentViewSession:
    """One allowlisted row from ``claude agents --json``."""

    session_id: str
    agent_view_id: str | None
    product_name: str
    cwd: str
    kind: str
    state: str
    started_at_ms: int
    process_id: int | None = None

    @property
    def process_backed(self) -> bool:
        """Return whether Agent View observed a live process for this row."""

        return self.process_id is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "agent_view_id": self.agent_view_id,
            "product_name": self.product_name,
            "cwd": self.cwd,
            "kind": self.kind,
            "state": self.state,
            "started_at_ms": self.started_at_ms,
            "process_backed": self.process_backed,
        }


@dataclass(frozen=True, slots=True)
class Peer:
    """One bounded row returned by Claude Code's MCP ``ListAgents`` tool."""

    name: str
    ref: str
    kind: str
    state: str
    details: tuple[str, ...]
    local: bool
    addressable: bool

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
            "addressable": self.addressable,
        }


@dataclass(frozen=True, slots=True)
class ClaudeRoute:
    """A fresh, exact Agent View-to-ListAgents mapping."""

    session: AgentViewSession
    peer: Peer

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session.session_id,
            "agent_view_id": self.session.agent_view_id,
            "product_name": self.session.product_name,
            "list_agents_name": self.peer.name,
            "list_agents_ref": self.peer.ref,
            "qualified_address": self.peer.qualified_address,
            "kind": self.peer.kind,
            "state": self.peer.state,
            "fresh": True,
        }


def _bounded_text(value: Any, *, label: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise RoutingError(
            "claude.agents_format",
            f"{label} must be a bounded nonempty single-line string",
        )
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise RoutingError(
            "claude.agents_format",
            f"{label} must be valid Unicode without surrogate code points",
        ) from None
    if (
        not value
        or len(value) > maximum
        or "\n" in value
        or "\r" in value
        or "\x00" in value
    ):
        raise RoutingError(
            "claude.agents_format",
            f"{label} must be a bounded nonempty single-line string",
        )
    return value


def _canonical_session_id(value: Any) -> str:
    if not isinstance(value, str):
        raise RoutingError(
            "claude.agents_format", "sessionId must be a canonical UUID string"
        )
    try:
        canonical = str(uuid.UUID(value))
    except ValueError:
        canonical = ""
    if value != canonical:
        raise RoutingError(
            "claude.agents_format", "sessionId must be a canonical UUID string"
        )
    return canonical


def _object_without_duplicate_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RoutingError(
                "claude.agents_format",
                "claude agents JSON must not contain duplicate object keys",
            )
        result[key] = value
    return result


def _reject_nonfinite_constant(_value: str) -> None:
    raise RoutingError(
        "claude.agents_format",
        "claude agents JSON must not contain non-finite numbers",
    )


def _optional_bounded_text(
    value: Any, *, label: str, maximum: int
) -> str | None:
    if value is None:
        return None
    return _bounded_text(value, label=label, maximum=maximum)


def parse_agent_view_sessions(raw: bytes) -> dict[str, tuple[AgentViewSession, ...]]:
    """Parse heterogeneous Agent View rows grouped by full session UUID."""

    if len(raw) > MAX_AGENT_VIEW_BYTES:
        raise RoutingError(
            "claude.agents_size",
            f"claude agents output exceeds {MAX_AGENT_VIEW_BYTES} bytes",
        )
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_nonfinite_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise RoutingError(
            "claude.agents_format", "claude agents returned invalid UTF-8 JSON"
        ) from None
    if not isinstance(value, list) or len(value) > MAX_AGENT_VIEW_SESSIONS:
        raise RoutingError(
            "claude.agents_format",
            "claude agents must return a bounded JSON array",
        )

    grouped: dict[str, list[AgentViewSession]] = {}
    for row in value:
        if not isinstance(row, dict):
            raise RoutingError(
                "claude.agents_format", "claude agents rows must be JSON objects"
            )
        session_id = _canonical_session_id(row.get("sessionId"))
        agent_view_id = _optional_bounded_text(
            row.get("id"), label="id", maximum=8
        )
        if agent_view_id is not None:
            agent_view_id = agent_view_id.lower()
            if AGENT_VIEW_ID_PATTERN.fullmatch(agent_view_id) is None:
                raise RoutingError(
                    "claude.agents_format",
                    "id must be the eight-hexadecimal Agent View identifier",
                )
            if agent_view_id != session_id.split("-", 1)[0]:
                raise RoutingError(
                    "claude.agent_id_mismatch",
                    "Agent View id does not match the full sessionId prefix",
                )
        product_name = _bounded_text(row.get("name"), label="name", maximum=256)
        cwd = _bounded_text(row.get("cwd"), label="cwd", maximum=4_096)
        kind = _bounded_text(row.get("kind"), label="kind", maximum=64)
        background_state = _optional_bounded_text(
            row.get("state"), label="state", maximum=64
        )
        process_status = _optional_bounded_text(
            row.get("status"), label="status", maximum=64
        )
        has_process_id = "pid" in row
        has_process_status = "status" in row
        if has_process_id != has_process_status:
            raise RoutingError(
                "claude.agents_format",
                "pid and status must either both be present or both be absent",
            )
        process_id: int | None = None
        if has_process_id:
            process_id = row.get("pid")
            if type(process_id) is not int or process_id <= 0 or process_id > 2**31:
                raise RoutingError(
                    "claude.agents_format",
                    "pid must be a bounded positive integer",
                )
        if process_status is None and background_state is None:
            raise RoutingError(
                "claude.agents_format",
                "each Agent View row must contain state or a pid/status pair",
            )
        state = process_status if process_status is not None else background_state
        assert state is not None
        started_at_ms = row.get("startedAt")
        if (
            type(started_at_ms) is not int
            or started_at_ms < 0
            or started_at_ms > 10**16
        ):
            raise RoutingError(
                "claude.agents_format",
                "startedAt must be a bounded non-negative integer",
            )
        grouped.setdefault(session_id, []).append(
            AgentViewSession(
                session_id=session_id,
                agent_view_id=agent_view_id,
                product_name=product_name,
                cwd=cwd,
                kind=kind,
                state=state,
                started_at_ms=started_at_ms,
                process_id=process_id,
            )
        )
    return {session_id: tuple(rows) for session_id, rows in grouped.items()}


def parse_list_agents_peers(listing: str) -> tuple[Peer, ...]:
    """Parse bounded human-readable MCP ``ListAgents`` rows, failing closed."""

    if not isinstance(listing, str) or len(listing) > MAX_LIST_AGENTS_CHARS:
        raise RoutingError(
            "claude.list_size",
            f"Claude ListAgents output exceeds {MAX_LIST_AGENTS_CHARS} characters",
        )
    peers: list[Peer] = []
    qualified_addresses: set[str] = set()
    for raw_line in listing.splitlines():
        parts = tuple(part.strip() for part in raw_line.strip().split("·"))
        if len(parts) < 3:
            continue
        matched = PEER_NAME_PATTERN.fullmatch(parts[0])
        if matched is None:
            continue
        metadata = tuple(part for part in parts[1:] if part)
        if any(len(part) > 512 for part in metadata):
            raise RoutingError(
                "claude.list_format", "Claude ListAgents metadata is too long"
            )
        kind = metadata[0] if metadata else ""
        state = metadata[1] if len(metadata) > 1 else ""
        metadata_text = " ".join(metadata).lower()
        local = kind.lower() in LOCAL_SESSION_KINDS and not any(
            marker in metadata_text for marker in NONLOCAL_MARKERS
        )
        addressable = local and state.lower() in ADDRESSABLE_SESSION_STATES
        peer = Peer(
            name=_bounded_text(
                matched.group("name"), label="ListAgents name", maximum=256
            ),
            ref=matched.group("ref").lower(),
            kind=kind,
            state=state,
            details=metadata[2:],
            local=local,
            addressable=addressable,
        )
        if peer.qualified_address in qualified_addresses:
            raise RoutingError(
                "claude.target_ambiguous",
                "Claude ListAgents returned the same qualified address more than once",
            )
        qualified_addresses.add(peer.qualified_address)
        peers.append(peer)
        if len(peers) > MAX_LIST_AGENTS_PEERS:
            raise RoutingError(
                "claude.list_size",
                f"Claude ListAgents returned more than {MAX_LIST_AGENTS_PEERS} peers",
            )
    return tuple(peers)


def select_agent_view_session(
    sessions: dict[str, tuple[AgentViewSession, ...]], session_id: str
) -> AgentViewSession:
    """Select one exact full session UUID without accepting a short ID or name."""

    canonical = _canonical_session_id(session_id)
    representations = sessions.get(canonical)
    if representations is None:
        raise RoutingError(
            "claude.session_not_found",
            "full sessionId was not present in fresh claude agents output",
        )
    process_backed = tuple(row for row in representations if row.process_backed)
    if len(process_backed) > 1:
        raise RoutingError(
            "claude.session_ambiguous",
            "selected sessionId has more than one live process-backed representation",
        )
    candidates = process_backed if process_backed else representations
    eligible = tuple(
        row
        for row in candidates
        if row.kind.lower() in LOCAL_SESSION_KINDS
        and row.state.lower() in ADDRESSABLE_SESSION_STATES
    )
    if len(eligible) != 1:
        raise RoutingError(
            "claude.session_not_local" if not eligible else "claude.session_ambiguous",
            "selected sessionId does not have one eligible live local representation",
        )
    selected = eligible[0]
    same_name_session_ids = {
        row.session_id
        for rows in sessions.values()
        for row in rows
        if row.product_name == selected.product_name
        and row.kind.lower() in LOCAL_SESSION_KINDS
        and row.state.lower() in ADDRESSABLE_SESSION_STATES
    }
    if same_name_session_ids != {selected.session_id}:
        raise RoutingError(
            "claude.agent_name_ambiguous",
            "multiple active Claude sessions share the selected mutable product name",
        )
    return selected


def resolve_list_agents_target(target: str, peers: tuple[Peer, ...]) -> Peer:
    """Resolve one exact fresh qualified address without retargeting by name."""

    if not target or "\n" in target or "\r" in target or len(target) > 300:
        raise RoutingError(
            "argument.target", "target must be a bounded single-line ListAgents address"
        )
    qualified = [peer for peer in peers if peer.qualified_address == target]
    if len(qualified) > 1:
        raise RoutingError(
            "claude.target_ambiguous",
            "fresh ListAgents output contains the target address more than once",
        )
    if len(qualified) == 1:
        return qualified[0]
    if any(peer.name == target for peer in peers):
        raise RoutingError(
            "claude.target_unqualified",
            "target must include the exact fresh name and ref from claude-list",
        )
    raise RoutingError(
        "claude.target_not_local",
        "target was not found among freshly discovered local Claude sessions",
    )


def correlate_route(
    session: AgentViewSession,
    local_peers: tuple[Peer, ...],
    *,
    requested_target: str | None = None,
) -> ClaudeRoute:
    """Correlate unique mutable name to one fresh ListAgents address."""

    matching = [peer for peer in local_peers if peer.name == session.product_name]
    if len(matching) != 1:
        code = "claude.route_not_found" if not matching else "claude.route_ambiguous"
        raise RoutingError(
            code,
            "selected full sessionId does not map to one unique fresh ListAgents name/ref",
        )
    peer = matching[0]
    if requested_target is not None and peer.qualified_address != requested_target:
        raise RoutingError(
            "claude.target_session_mismatch",
            "requested target does not equal the fresh route for the selected sessionId",
        )
    return ClaudeRoute(session=session, peer=peer)
