# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Shared bounded errors for CAM project-state operations."""

from __future__ import annotations

from typing import Any


class ProjectError(Exception):
    """Bounded project-state failure suitable for a machine-readable CLI."""

    def __init__(self, code: str, detail: str, *, audit: dict[str, Any] | None = None):
        self.code = code[:80]
        self.detail = detail[:300]
        self.audit = audit
        super().__init__(self.detail)
