# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Shared test harness for approval-ledger recovery behavior."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import cam1_transport
from tools.cam1lib import product_approvals, product_executables


class ProductApprovalRecoveryTestCase(unittest.TestCase):
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
        account_home_patch = mock.patch.object(
            product_approvals, "account_home", return_value=self.home
        )
        account_home_patch.start()
        self.addCleanup(account_home_patch.stop)

    @property
    def registry(self) -> Path:
        return self.home / "CAM" / "Approvals" / product_approvals.REGISTRY_NAME

    def approve(self) -> dict[str, object]:
        candidate = product_executables.discover_candidate(
            "claude-code", str(self.executable), allow_path_lookup=False
        )
        return product_approvals.approve_candidate(
            vendor="claude-code",
            product_bin=str(self.executable),
            expected_fingerprint_sha256=candidate.fingerprint_sha256,
            operator_reference="direct test operator confirmation",
        )

    def damage_registry(
        self, tail: bytes = b'{"interrupted":'
    ) -> tuple[bytes, bytes, dict[str, object]]:
        self.approve()
        prefix = self.registry.read_bytes()
        damaged = prefix + tail
        self.registry.write_bytes(damaged)
        self.registry.chmod(0o600)
        status = product_approvals.approval_recovery_status()
        return prefix, damaged, status

    @staticmethod
    def recovery_kwargs(status: dict[str, object]) -> dict[str, object]:
        recovery = status["recovery"]
        identity = recovery["registry_identity"]
        return {
            "expected_registry_sha256": recovery["registry_sha256"],
            "expected_registry_bytes": recovery["registry_bytes"],
            "expected_registry_device": identity["device"],
            "expected_registry_inode": identity["inode"],
            "expected_registry_ctime_ns": identity["ctime_ns"],
            "expected_registry_mtime_ns": identity["mtime_ns"],
            "expected_prefix_sha256": recovery["verified_prefix_sha256"],
            "expected_prefix_bytes": recovery["verified_prefix_bytes"],
            "expected_prefix_record_count": recovery["verified_prefix_record_count"],
            "expected_tail_sha256": recovery["partial_tail_sha256"],
            "expected_tail_bytes": recovery["partial_tail_bytes"],
            "reason": "directly reviewed interrupted append",
            "operator_reference": "direct test operator recovery confirmation",
        }

    def invoke_product_cli(self, *arguments: str) -> tuple[int, dict[str, object]]:
        emitted: list[dict[str, object]] = []

        def capture(payload: dict[str, object], **_kwargs: object) -> None:
            emitted.append(payload)

        with (
            mock.patch.object(
                cam1_transport,
                "_require_live_validation_profile",
                return_value=({}, False),
            ),
            mock.patch.object(
                cam1_transport,
                "_with_validation_profile",
                side_effect=lambda payload: payload,
            ),
            mock.patch.object(cam1_transport, "_emit", side_effect=capture),
        ):
            returncode = cam1_transport.main(list(arguments))
        self.assertTrue(emitted)
        return returncode, emitted[-1]
