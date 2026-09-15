# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Shared owner-private executable fixture for approval and transport tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.cam1lib import product_approvals, product_executables


class ProductApprovalTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name) / "account"
        self.home.mkdir(mode=0o700)
        self.home.chmod(0o700)
        self.bin_dir = self.home / "bin"
        self.bin_dir.mkdir(mode=0o700)
        self.marker = self.home / "executed"
        self.executable = self.bin_dir / "claude"
        self.executable.write_text(
            "#!/bin/sh\nprintf executed > " + str(self.marker) + "\n",
            encoding="utf-8",
        )
        self.executable.chmod(0o700)
        self.account_home_patch = mock.patch.object(
            product_approvals, "account_home", return_value=self.home
        )
        self.account_home_patch.start()
        self.addCleanup(self.account_home_patch.stop)

    def discover(self) -> product_executables.ExecutableCandidate:
        return product_executables.discover_candidate(
            "claude-code", str(self.executable), allow_path_lookup=False
        )

    def approve(self) -> dict[str, object]:
        candidate = self.discover()
        return product_approvals.approve_candidate(
            vendor="claude-code",
            product_bin=str(self.executable),
            expected_fingerprint_sha256=candidate.fingerprint_sha256,
            operator_reference="direct test operator confirmation",
        )
