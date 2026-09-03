# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Subprocess harness for legacy CLI tests unrelated to account approvals.

The production CLIs intentionally derive their approval registry from the
operating-system account, so tests must not redirect it through an environment
variable or write synthetic approvals into a developer's real account ledger.
Dedicated product-approval tests exercise the complete production gate.  This
harness keeps older onboarding and transport subprocess tests focused on their
own contracts by replacing only that gate inside the child process.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import cam1_project, cam1_transport  # noqa: E402
from tools.cam1lib import onboarding, product_executables, profile  # noqa: E402


def _resolved_product(
    *, vendor: str, product_bin: str, allow_path_lookup: bool = False
) -> tuple[str, dict[str, object]]:
    path, _source = product_executables.resolve_candidate_path(
        vendor,
        product_bin,
        allow_path_lookup=allow_path_lookup,
    )
    return path, {}


def _resolved_transport_product(
    value: str, *, vendor: str, binding: object | None = None
) -> str:
    del binding
    path, _source = product_executables.resolve_candidate_path(
        vendor,
        value,
        allow_path_lookup=True,
    )
    return path


def _current_test_profile(**kwargs: object) -> tuple[dict[str, object], bool]:
    current = profile.current_validation_profile()
    report = {"available": True, **current.as_dict()}
    override_used = bool(kwargs.get("allow_dirty") and current.source_control.dirty)
    return report, override_used


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in {"onboarding", "transport"}:
        raise SystemExit("usage: _cam1_cli_test_harness.py onboarding|transport ...")
    target = sys.argv[1]
    arguments = sys.argv[2:]
    with (
        mock.patch.object(
            onboarding.product_approvals,
            "require_approved_executable",
            side_effect=_resolved_product,
        ),
        mock.patch.object(
            onboarding.product_approvals,
            "require_approved_metadata",
            side_effect=_resolved_product,
        ),
        mock.patch.object(
            cam1_transport,
            "resolve_product_binary",
            side_effect=_resolved_transport_product,
        ),
        mock.patch.object(
            cam1_transport,
            "_require_current_product_approval",
        ),
        mock.patch.object(
            cam1_transport,
            "_require_live_validation_profile",
            side_effect=_current_test_profile,
        ),
        mock.patch.object(
            onboarding,
            "require_trusted_source",
            side_effect=profile.current_validation_profile,
        ),
    ):
        if target == "onboarding":
            return cam1_project.main(arguments)
        return cam1_transport.main(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
