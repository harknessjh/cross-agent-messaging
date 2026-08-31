# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Journal-backed, non-routable participant enrollment proposals."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import PurePath
from typing import Any

from .participants import PARTICIPANT_NAME_PATTERN, VENDORS
from .protocol import CamUsageError

PROPOSAL_FORMAT = "CAM-ENROLLMENT-PROPOSAL/1"
PRODUCT_EXECUTABLE_SOURCES = frozenset({"explicit_candidate", "path_candidate"})
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class EnrollmentStatus(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    SUPERSEDED = "superseded"


def _text(
    value: Any,
    *,
    field_name: str,
    maximum: int,
    optional: bool = False,
) -> str | None:
    if value is None and optional:
        return None
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or any(character in value for character in "\r\n\x00")
    ):
        qualifier = " or null" if optional else ""
        raise CamUsageError(
            "onboarding.field",
            f"{field_name} must be a bounded nonempty single-line string{qualifier}",
        )
    return value


def _uuid(value: Any, *, field_name: str) -> str:
    text = _text(value, field_name=field_name, maximum=128)
    try:
        return str(uuid.UUID(str(text)))
    except (ValueError, AttributeError):
        raise CamUsageError(
            "onboarding.identifier",
            f"{field_name} must be a valid UUID",
        ) from None


def _path(value: Any, *, field_name: str) -> str:
    text = _text(value, field_name=field_name, maximum=4_096)
    if not PurePath(str(text)).is_absolute():
        raise CamUsageError(
            "onboarding.path",
            f"{field_name} must be an absolute path",
        )
    return str(text)


def _timestamp(value: Any, *, field_name: str) -> str:
    text = _text(value, field_name=field_name, maximum=64)
    assert isinstance(text, str)
    if not text.endswith("Z"):
        raise CamUsageError(
            "onboarding.timestamp", f"{field_name} must be a UTC timestamp"
        )
    try:
        parsed = dt.datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError:
        raise CamUsageError(
            "onboarding.timestamp", f"{field_name} must be a valid UTC timestamp"
        ) from None
    if parsed.utcoffset() != dt.timedelta(0):
        raise CamUsageError(
            "onboarding.timestamp", f"{field_name} must be a UTC timestamp"
        )
    return text


def _sha256(value: Any, *, field_name: str) -> str:
    text = _text(value, field_name=field_name, maximum=64)
    if not isinstance(text, str) or _SHA256_PATTERN.fullmatch(text) is None:
        raise CamUsageError(
            "onboarding.digest", f"{field_name} must be a lowercase SHA-256 digest"
        )
    return text


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    cam_checkout: str
    validation_profile_sha256: str
    project_root: str
    product_executable: str
    product_executable_source: str

    @classmethod
    def from_mapping(cls, value: Any) -> ExecutionContext:
        if not isinstance(value, Mapping) or set(value) != {
            "cam_checkout",
            "validation_profile_sha256",
            "project_root",
            "product_executable",
            "product_executable_source",
        }:
            raise CamUsageError(
                "onboarding.execution_context",
                "execution_context does not match the proposal contract",
            )
        executable_source = str(
            _text(
                value["product_executable_source"],
                field_name="product_executable_source",
                maximum=64,
            )
        )
        if executable_source not in PRODUCT_EXECUTABLE_SOURCES:
            raise CamUsageError(
                "onboarding.product_bin_source",
                "product executable source is unsupported",
            )
        return cls(
            cam_checkout=_path(value["cam_checkout"], field_name="cam_checkout"),
            validation_profile_sha256=_sha256(
                value["validation_profile_sha256"],
                field_name="validation_profile_sha256",
            ),
            project_root=_path(value["project_root"], field_name="project_root"),
            product_executable=_path(
                value["product_executable"], field_name="product_executable"
            ),
            product_executable_source=executable_source,
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "cam_checkout": self.cam_checkout,
            "validation_profile_sha256": self.validation_profile_sha256,
            "project_root": self.project_root,
            "product_executable": self.product_executable,
            "product_executable_source": self.product_executable_source,
        }


@dataclass(frozen=True, slots=True)
class EnrollmentProposal:
    project_id: str
    project_display_name: str
    proposal_id: str
    participant_id: str
    common_name: str
    display_name: str
    role: str | None
    vendor: str
    session_id: str
    session_label: str | None
    session_kind: str | None
    session_git_top_level: str
    session_git_common_dir: str
    discovery_source: str
    proposed_at: str
    execution_context: ExecutionContext
    supersedes: tuple[str, ...] = ()
    proposal_sha256: str = ""
    status: EnrollmentStatus = EnrollmentStatus.PENDING
    superseded_by: str | None = None
    confirmed_at: str | None = None
    operator_reference: str | None = None

    @property
    def confirmation_code(self) -> str:
        return self.proposal_sha256[:12]

    def payload(self) -> dict[str, Any]:
        return {
            "format": PROPOSAL_FORMAT,
            "project_id": self.project_id,
            "project_display_name": self.project_display_name,
            "proposal_id": self.proposal_id,
            "participant_id": self.participant_id,
            "common_name": self.common_name,
            "display_name": self.display_name,
            "role": self.role,
            "vendor": self.vendor,
            "session_id": self.session_id,
            "session_label": self.session_label,
            "session_kind": self.session_kind,
            "session_git_top_level": self.session_git_top_level,
            "session_git_common_dir": self.session_git_common_dir,
            "discovery_source": self.discovery_source,
            "proposed_at": self.proposed_at,
            "execution_context": self.execution_context.as_dict(),
            "supersedes": list(self.supersedes),
        }

    def as_dict(self, *, redact: bool = False) -> dict[str, Any]:
        payload = self.payload()
        if redact:
            payload["session_id"] = "redacted"
            payload["session_git_top_level"] = "redacted"
            payload["session_git_common_dir"] = "redacted"
            payload["execution_context"] = {
                "cam_checkout": "redacted",
                "validation_profile_sha256": self.execution_context.validation_profile_sha256,
                "project_root": "redacted",
                "product_executable": "redacted",
                "product_executable_source": (
                    self.execution_context.product_executable_source
                ),
            }
        return {
            **payload,
            "proposal_sha256": self.proposal_sha256,
            "confirmation_code": self.confirmation_code,
            "status": self.status.value,
            "superseded_by": self.superseded_by,
            "confirmed_at": self.confirmed_at,
            "operator_reference": (
                "redacted"
                if redact and self.operator_reference is not None
                else self.operator_reference
            ),
        }


def proposal_sha256(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(payload),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_proposal(
    *,
    project_id: str,
    project_display_name: str,
    proposal_id: str,
    participant_id: str,
    common_name: str,
    display_name: str,
    role: str | None,
    vendor: str,
    session_id: str,
    session_label: str | None,
    session_kind: str | None,
    session_git_top_level: str,
    session_git_common_dir: str,
    discovery_source: str,
    proposed_at: str,
    execution_context: Mapping[str, Any] | ExecutionContext,
    supersedes: tuple[str, ...] = (),
    expected_sha256: str | None = None,
) -> EnrollmentProposal:
    name = _text(common_name, field_name="common_name", maximum=63)
    if not isinstance(name, str) or PARTICIPANT_NAME_PATTERN.fullmatch(name) is None:
        raise CamUsageError(
            "onboarding.common_name",
            "common name must be lowercase letters, digits, or hyphens",
        )
    normalized_vendor = _text(vendor, field_name="vendor", maximum=32)
    if normalized_vendor not in VENDORS:
        raise CamUsageError("onboarding.vendor", "participant vendor is unsupported")
    context = (
        execution_context
        if isinstance(execution_context, ExecutionContext)
        else ExecutionContext.from_mapping(execution_context)
    )
    proposal = EnrollmentProposal(
        project_id=_uuid(project_id, field_name="project_id"),
        project_display_name=str(
            _text(
                project_display_name,
                field_name="project_display_name",
                maximum=128,
            )
        ),
        proposal_id=_uuid(proposal_id, field_name="proposal_id"),
        participant_id=_uuid(participant_id, field_name="participant_id"),
        common_name=name,
        display_name=str(_text(display_name, field_name="display_name", maximum=128)),
        role=_text(role, field_name="role", maximum=512, optional=True),
        vendor=str(normalized_vendor),
        session_id=_uuid(session_id, field_name="session_id"),
        session_label=_text(
            session_label, field_name="session_label", maximum=256, optional=True
        ),
        session_kind=_text(
            session_kind, field_name="session_kind", maximum=64, optional=True
        ),
        session_git_top_level=_path(
            session_git_top_level, field_name="session_git_top_level"
        ),
        session_git_common_dir=_path(
            session_git_common_dir, field_name="session_git_common_dir"
        ),
        discovery_source=str(
            _text(discovery_source, field_name="discovery_source", maximum=128)
        ),
        proposed_at=_timestamp(proposed_at, field_name="proposed_at"),
        execution_context=context,
        supersedes=tuple(_uuid(value, field_name="supersedes") for value in supersedes),
    )
    if proposal.vendor == "claude-code" and proposal.session_kind is None:
        raise CamUsageError(
            "onboarding.session_kind_required",
            "Claude enrollment requires a discovered session kind",
        )
    if proposal.vendor == "claude-code" and proposal.session_label is None:
        raise CamUsageError(
            "onboarding.session_label_required",
            "Claude enrollment requires a discovered session label",
        )
    digest = proposal_sha256(proposal.payload())
    if (
        expected_sha256 is not None
        and _sha256(expected_sha256, field_name="proposal_sha256") != digest
    ):
        raise CamUsageError(
            "onboarding.digest_mismatch",
            "proposal digest does not match its exact canonical payload",
        )
    return replace(proposal, proposal_sha256=digest)


@dataclass(slots=True)
class EnrollmentProjection:
    proposals: dict[str, EnrollmentProposal] = field(default_factory=dict)

    def add(self, proposal: EnrollmentProposal) -> EnrollmentProposal:
        if proposal.proposal_id in self.proposals:
            raise CamUsageError(
                "onboarding.proposal_id_conflict",
                "enrollment proposal identifier already exists",
            )
        expected_supersedes = {
            candidate.proposal_id
            for candidate in self.proposals.values()
            if candidate.status == EnrollmentStatus.PENDING
            and candidate.vendor == proposal.vendor
            and candidate.session_id == proposal.session_id
        }
        if set(proposal.supersedes) != expected_supersedes:
            raise CamUsageError(
                "onboarding.supersession_mismatch",
                "proposal must supersede every prior pending proposal for this session",
            )
        for proposal_id in proposal.supersedes:
            current = self.proposals[proposal_id]
            self.proposals[proposal_id] = replace(
                current,
                status=EnrollmentStatus.SUPERSEDED,
                superseded_by=proposal.proposal_id,
            )
        self.proposals[proposal.proposal_id] = proposal
        return proposal

    def select(self, proposal_id: str) -> EnrollmentProposal:
        canonical = _uuid(proposal_id, field_name="proposal_id")
        try:
            return self.proposals[canonical]
        except KeyError:
            raise CamUsageError(
                "onboarding.proposal_unknown", "enrollment proposal is not known"
            ) from None

    def confirm(
        self,
        proposal_id: str,
        *,
        expected_sha256: str,
        operator_reference: str,
        confirmed_at: str,
    ) -> EnrollmentProposal:
        current = self.select(proposal_id)
        digest = _sha256(expected_sha256, field_name="expected_proposal_sha256")
        if digest != current.proposal_sha256:
            raise CamUsageError(
                "onboarding.digest_mismatch",
                "confirmation digest does not match the displayed proposal",
            )
        if current.status == EnrollmentStatus.SUPERSEDED:
            raise CamUsageError(
                "onboarding.proposal_superseded",
                "superseded enrollment proposal cannot be confirmed",
            )
        reference = _text(
            operator_reference, field_name="operator_reference", maximum=1_024
        )
        timestamp = _timestamp(confirmed_at, field_name="confirmed_at")
        confirmed_time = dt.datetime.fromisoformat(timestamp[:-1] + "+00:00")
        proposed_time = dt.datetime.fromisoformat(current.proposed_at[:-1] + "+00:00")
        if confirmed_time < proposed_time:
            raise CamUsageError(
                "onboarding.confirmation_chronology",
                "enrollment cannot be confirmed before it was proposed",
            )
        if current.status == EnrollmentStatus.CONFIRMED:
            return current
        updated = replace(
            current,
            status=EnrollmentStatus.CONFIRMED,
            confirmed_at=timestamp,
            operator_reference=str(reference),
        )
        self.proposals[current.proposal_id] = updated
        return updated

    def as_dict(self, *, redact: bool = False) -> dict[str, Any]:
        return {
            "proposals": [
                proposal.as_dict(redact=redact)
                for proposal in sorted(
                    self.proposals.values(),
                    key=lambda item: (item.proposed_at, item.proposal_id),
                )
            ]
        }
