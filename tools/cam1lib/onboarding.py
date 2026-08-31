# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Self-inspection and human-review cards for local CAM enrollment."""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import profile, project, routing
from .enrollment import EnrollmentProposal
from .protocol import CamUsageError

_PRODUCT_COMMAND = {"codex": "codex", "claude-code": "claude"}
_SESSION_ENVIRONMENT = {
    "codex": "CODEX_THREAD_ID",
    "claude-code": "CLAUDE_CODE_SESSION_ID",
}


def _canonical_uuid(value: str, *, field_name: str) -> str:
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError):
        raise CamUsageError(
            "onboarding.identifier",
            f"{field_name} must be a valid UUID",
        ) from None


def _resolved_executable(value: str | None, *, vendor: str) -> tuple[str, str]:
    command = _PRODUCT_COMMAND[vendor]
    if value is None:
        candidate = shutil.which(command)
        source = "path_candidate"
    else:
        supplied = Path(value).expanduser()
        if not supplied.is_absolute():
            raise CamUsageError(
                "onboarding.product_bin_absolute",
                "an explicitly supplied product executable must be an absolute path",
            )
        candidate = str(supplied)
        source = "explicit_candidate"
    if candidate is None:
        raise CamUsageError(
            "onboarding.product_bin_missing",
            f"no {command} executable candidate was found",
        )
    try:
        resolved = Path(candidate).resolve(strict=True)
        metadata = resolved.stat(follow_symlinks=False)
    except OSError:
        raise CamUsageError(
            "onboarding.product_bin_invalid",
            f"the proposed {command} executable is unavailable",
        ) from None
    if not stat.S_ISREG(metadata.st_mode) or not os.access(resolved, os.X_OK):
        raise CamUsageError(
            "onboarding.product_bin_invalid",
            f"the proposed {command} path is not an executable regular file",
        )
    return str(resolved), source


def resolve_product_executable(value: str, *, vendor: str) -> str:
    """Validate and canonicalize one explicitly selected product executable."""

    resolved, _ = _resolved_executable(value, vendor=vendor)
    return resolved


def _session_identifier(
    vendor: str,
    explicit: str | None,
    environment: Mapping[str, str],
) -> tuple[str, str]:
    variable = _SESSION_ENVIRONMENT[vendor]
    observed = environment.get(variable)
    if observed:
        observed = _canonical_uuid(observed, field_name=variable)
    if explicit is not None:
        supplied = _canonical_uuid(explicit, field_name="session_id")
        if observed is not None and supplied != observed:
            raise CamUsageError(
                "onboarding.session_mismatch",
                "supplied session UUID does not match this agent's session metadata",
            )
        return supplied, variable if observed is not None else "explicit_session_id"
    if observed is None:
        raise CamUsageError(
            "onboarding.session_id_missing",
            f"{variable} is unavailable; supply the current full session UUID for review",
        )
    return observed, variable


def _claude_agent_view(
    executable: str,
    session_id: str,
) -> routing.AgentViewSession:
    try:
        completed = subprocess.run(
            [executable, "agents", "--json"],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise CamUsageError(
            "onboarding.claude_discovery_failed",
            "Claude Agent View discovery did not complete",
        ) from None
    if completed.returncode != 0:
        raise CamUsageError(
            "onboarding.claude_discovery_failed",
            "Claude Agent View discovery exited unsuccessfully",
        )
    try:
        sessions = routing.parse_agent_view_sessions(completed.stdout)
        return routing.select_agent_view_identity_session(sessions, session_id)
    except routing.RoutingError as error:
        raise CamUsageError(error.code, error.detail) from error


def _slug(value: str, *, fallback: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    normalized = normalized[:63].rstrip("-")
    return normalized or fallback


@dataclass(frozen=True, slots=True)
class SelfInspection:
    project_id: str
    project_display_name: str
    project_root: str
    project_git_common_dir: str
    worktree_id: str
    cam_checkout: str
    validation_profile_sha256: str
    vendor: str
    session_id: str
    session_label: str | None
    session_kind: str | None
    session_git_top_level: str
    session_git_common_dir: str
    discovery_source: str
    common_name: str
    display_name: str
    role: str | None
    product_executable: str
    product_executable_source: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "project": {
                "project_id": self.project_id,
                "display_name": self.project_display_name,
                "project_root": self.project_root,
                "git_common_dir": self.project_git_common_dir,
                "worktree_id": self.worktree_id,
            },
            "participant": {
                "common_name": self.common_name,
                "display_name": self.display_name,
                "role": self.role,
                "vendor": self.vendor,
                "session_id": self.session_id,
                "session_label": self.session_label,
                "session_kind": self.session_kind,
            },
            "execution_context": {
                "cam_checkout": self.cam_checkout,
                "validation_profile_sha256": self.validation_profile_sha256,
                "product_executable": self.product_executable,
                "product_executable_source": self.product_executable_source,
            },
            "discovery": {
                "source": self.discovery_source,
                "session_git_top_level": self.session_git_top_level,
                "session_git_common_dir": self.session_git_common_dir,
                "transient_route_observed": False,
            },
        }


def require_trusted_source() -> profile.ValidationProfile:
    """Require trusted CAM bytes before enrollment state or product access."""

    try:
        return profile.require_live_profile(allow_dirty=False)
    except profile.ValidationProfileError as error:
        raise CamUsageError(error.code, error.detail) from error


def inspect_self(
    binding: project.ProjectBinding,
    *,
    vendor: str,
    common_name: str | None = None,
    display_name: str | None = None,
    role: str | None = None,
    session_id: str | None = None,
    session_label: str | None = None,
    session_kind: str | None = None,
    product_bin: str | None = None,
    environment: Mapping[str, str] | None = None,
) -> SelfInspection:
    """Discover the current session without creating a trusted roster entry."""

    if vendor not in _PRODUCT_COMMAND:
        raise CamUsageError("onboarding.vendor", "participant vendor is unsupported")
    live_profile = require_trusted_source()
    environ = os.environ if environment is None else environment
    canonical_session, session_source = _session_identifier(vendor, session_id, environ)
    executable, executable_source = _resolved_executable(product_bin, vendor=vendor)
    if vendor == "claude-code":
        discovered = _claude_agent_view(executable, canonical_session)
        if session_label is not None and session_label != discovered.product_name:
            raise CamUsageError(
                "onboarding.session_label_mismatch",
                "supplied session label does not match fresh Claude Agent View",
            )
        if session_kind is not None and session_kind != discovered.kind:
            raise CamUsageError(
                "onboarding.session_kind_mismatch",
                "supplied session kind does not match fresh Claude Agent View",
            )
        try:
            session_context = project.discover_git_context(
                discovered.cwd, git_bin=binding.git_bin
            )
        except project.ProjectError:
            raise CamUsageError(
                "onboarding.project_mismatch",
                "the current Claude session cwd is not in the selected Git project",
            ) from None
        if session_context.common_dir != binding.git_common_dir:
            raise CamUsageError(
                "onboarding.project_mismatch",
                "the current Claude session belongs to a different Git project",
            )
        observed_label = discovered.product_name
        observed_kind = discovered.kind
        discovery_source = f"{session_source}+claude_agent_view"
    else:
        try:
            session_context = project.discover_git_context(
                Path.cwd(), git_bin=binding.git_bin
            )
        except project.ProjectError:
            raise CamUsageError(
                "onboarding.project_mismatch",
                "the current Codex cwd is not in the selected Git project",
            ) from None
        if session_context.common_dir != binding.git_common_dir:
            raise CamUsageError(
                "onboarding.project_mismatch",
                "the current Codex session belongs to a different Git project",
            )
        observed_label = session_label
        observed_kind = session_kind
        discovery_source = session_source
    profile_sha256 = live_profile.validation_profile_sha256
    default_common_name = _slug(
        observed_label or f"{vendor}-{canonical_session[:8]}",
        fallback=f"{vendor}-{canonical_session[:8]}",
    )
    selected_common_name = common_name or default_common_name
    selected_display_name = display_name or observed_label or selected_common_name
    return SelfInspection(
        project_id=binding.project_id,
        project_display_name=binding.display_name,
        project_root=str(binding.git_top_level),
        project_git_common_dir=str(binding.git_common_dir),
        worktree_id=binding.worktree_id,
        cam_checkout=str(project.REPOSITORY_ROOT),
        validation_profile_sha256=profile_sha256,
        vendor=vendor,
        session_id=canonical_session,
        session_label=observed_label,
        session_kind=observed_kind,
        session_git_top_level=str(session_context.top_level),
        session_git_common_dir=str(session_context.common_dir),
        discovery_source=discovery_source,
        common_name=selected_common_name,
        display_name=selected_display_name,
        role=role,
        product_executable=executable,
        product_executable_source=executable_source,
    )


def identity_card(proposal: EnrollmentProposal) -> dict[str, Any]:
    """Return the exact human-review surface for one pending proposal."""

    context = proposal.execution_context
    exact_reply = f"Confirm CAM/1 enrollment {proposal.confirmation_code}."
    role = proposal.role if proposal.role is not None else "unspecified"
    session_label = (
        proposal.session_label if proposal.session_label is not None else "unavailable"
    )
    session_kind = (
        proposal.session_kind if proposal.session_kind is not None else "unavailable"
    )
    human_card = "\n".join(
        (
            "CAM/1 enrollment card",
            (f"Project: {proposal.project_display_name} ({proposal.project_id})"),
            f"Project root: {context.project_root}",
            (
                f"Participant: {proposal.common_name} "
                f"({proposal.display_name}; role: {role})"
            ),
            f"Product: {proposal.vendor}; label: {session_label}; kind: {session_kind}",
            f"Stable session UUID: {proposal.session_id}",
            (
                f"Product executable: {context.product_executable} "
                f"({context.product_executable_source})"
            ),
            f"CAM checkout: {context.cam_checkout}",
            f"Validation profile: {context.validation_profile_sha256}",
            f"Confirm exactly: {exact_reply}",
        )
    )
    return {
        "status": proposal.status.value.upper(),
        "proposal_id": proposal.proposal_id,
        "proposal_sha256": proposal.proposal_sha256,
        "confirmation_code": proposal.confirmation_code,
        "project": {
            "project_id": proposal.project_id,
            "display_name": proposal.project_display_name,
            "project_root": context.project_root,
        },
        "participant": {
            "participant_id": proposal.participant_id,
            "common_name": proposal.common_name,
            "display_name": proposal.display_name,
            "role": proposal.role,
            "vendor": proposal.vendor,
            "session_id": proposal.session_id,
            "session_label": proposal.session_label,
            "session_kind": proposal.session_kind,
        },
        "execution_context": context.as_dict(),
        "discovery": {
            "source": proposal.discovery_source,
            "session_git_top_level": proposal.session_git_top_level,
            "session_git_common_dir": proposal.session_git_common_dir,
            "transient_route_observed": False,
        },
        "human_confirmation": {
            "exact_reply": exact_reply,
            "meaning": (
                "Confirms the complete displayed proposal, including project, "
                "session, names, optional role, product metadata, CAM profile, and "
                "executable path; it does not authenticate the chat sender or "
                "authorize peer work."
            ),
        },
        "human_card": human_card,
        "worktree_effect": "No tracked or untracked worktree files are created.",
    }
