# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Shared result and error types for the CAM project journal."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .errors import ProjectError

MAX_JOURNAL_BYTES = 128 * 1_048_576


class JournalError(ProjectError):
    """Fail-closed journal validation or append failure."""


@dataclass(frozen=True, slots=True)
class JournalVerification:
    project_id: str
    record_count: int
    last_sequence: int
    last_record_sha256: str | None
    total_bytes: int

    def summary(self) -> dict[str, Any]:
        return {
            "valid": True,
            "project_id": self.project_id,
            "record_count": self.record_count,
            "last_sequence": self.last_sequence,
            "last_record_sha256": self.last_record_sha256,
            "total_bytes": self.total_bytes,
        }


@dataclass(frozen=True, slots=True)
class PartialTailReport:
    """Read-only evidence for one recoverable incomplete EOF record."""

    journal_sha256: str
    journal_bytes: int
    verified_prefix_bytes: int
    verified_prefix_sha256: str
    partial_tail_bytes: int
    partial_tail_sha256: str
    prefix_verification: JournalVerification

    def summary(self) -> dict[str, Any]:
        return {
            "recoverable": True,
            "journal_sha256": self.journal_sha256,
            "journal_bytes": self.journal_bytes,
            "verified_prefix_bytes": self.verified_prefix_bytes,
            "verified_prefix_sha256": self.verified_prefix_sha256,
            "partial_tail_bytes": self.partial_tail_bytes,
            "partial_tail_sha256": self.partial_tail_sha256,
            "prefix": self.prefix_verification.summary(),
        }


@dataclass(frozen=True, slots=True)
class PartialTailRecovery:
    """Evidence returned after an explicit partial-tail recovery."""

    archive_path: str
    original_sha256: str
    original_bytes: int
    recovered_record: dict[str, Any]
    verification: JournalVerification

    def summary(self) -> dict[str, Any]:
        return {
            "archive_path": self.archive_path,
            "original_sha256": self.original_sha256,
            "original_bytes": self.original_bytes,
            "recovered_record_id": self.recovered_record["record_id"],
            "verification": self.verification.summary(),
        }
