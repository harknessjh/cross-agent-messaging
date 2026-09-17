# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import subprocess
from unittest import mock

from tests._product_approval_test_case import ProductApprovalTestCase
from tools import _cam1_bootstrap as bootstrap
from tools.cam1lib import product_approvals, product_executables, profile, project_git
from tools.cam1lib.errors import ProjectError


class ExecutableBoundaryTests(ProductApprovalTestCase):
    def test_cached_product_approval_rechecks_parent_permissions(self) -> None:
        self.approve()
        for entrypoint in (
            product_approvals.require_approved_executable,
            product_approvals.require_approved_metadata,
        ):
            product_approvals.require_approved_executable(
                vendor="claude-code", product_bin=str(self.executable)
            )
            self.bin_dir.chmod(0o777)
            try:
                with (
                    self.subTest(entrypoint=entrypoint.__name__),
                    self.assertRaises(ProjectError) as error,
                ):
                    entrypoint(vendor="claude-code", product_bin=str(self.executable))
                self.assertEqual(error.exception.code, "product_approval.writable")
            finally:
                self.bin_dir.chmod(0o700)

    def test_script_drift_blocks_cached_launch_but_not_history_or_revocation(
        self,
    ) -> None:
        approved = self.approve()
        self.executable.write_text(
            "#!/usr/bin/env python3\nprint('not approved')\n", encoding="utf-8"
        )
        for entrypoint in (
            product_approvals.require_approved_executable,
            product_approvals.require_approved_metadata,
        ):
            with (
                self.subTest(entrypoint=entrypoint.__name__),
                self.assertRaises(ProjectError) as error,
            ):
                entrypoint(vendor="claude-code", product_bin=str(self.executable))
            self.assertEqual(error.exception.code, "product_approval.native_required")
        status = product_approvals.approval_status(
            vendor="claude-code", product_bin=str(self.executable)
        )
        self.assertEqual(len(status["active"]), 1)
        product_approvals.revoke_approval(
            vendor="claude-code",
            product_bin=str(self.executable),
            approval_record_id=approved["approval"]["record_id"],
            expected_fingerprint_sha256=approved["approval"]["attributes"][
                "fingerprint_sha256"
            ],
            operator_reference="direct test revocation",
        )

    def test_full_discovery_rejects_script_before_any_spawn(self) -> None:
        self.executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        with mock.patch("subprocess.run") as spawn:
            with self.assertRaises(ProjectError) as error:
                self.discover()
        self.assertEqual(error.exception.code, "product_approval.native_required")
        spawn.assert_not_called()

    def test_all_git_launch_paths_reject_script_and_permission_drift(self) -> None:
        calls = (
            lambda: project_git._git_output(
                str(self.executable), self.home, "--git-dir"
            ),
            lambda: bootstrap._run_git(str(self.executable), str(self.home), "status"),
            lambda: profile._run_git(str(self.executable), self.home, "status"),
        )
        for bad_state in ("permissions", "script"):
            if bad_state == "permissions":
                self.bin_dir.chmod(0o777)
            else:
                self.bin_dir.chmod(0o700)
                self.executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            for call in calls:
                with (
                    self.subTest(state=bad_state, call=call),
                    mock.patch("subprocess.run") as spawn,
                ):
                    with self.assertRaises(
                        (
                            ProjectError,
                            bootstrap.BootstrapError,
                            profile.ValidationProfileError,
                        )
                    ):
                        call()
                    spawn.assert_not_called()

    def test_git_default_selection_skips_ineligible_candidate_without_running_it(
        self,
    ) -> None:
        script = self.bin_dir / "fake-git"
        script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        script.chmod(0o700)
        for module, function, constant in (
            (bootstrap, bootstrap._git_executable, "_GIT_EXECUTABLE_CANDIDATES"),
            (profile, profile._git_executable, "GIT_EXECUTABLE_CANDIDATES"),
            (
                project_git,
                project_git._default_git_executable,
                "GIT_EXECUTABLE_CANDIDATES",
            ),
        ):
            with (
                self.subTest(module=module.__name__),
                mock.patch.object(
                    module, constant, (str(script), str(self.executable))
                ),
                mock.patch("subprocess.run") as spawn,
            ):
                self.assertEqual(function(), str(self.executable.resolve()))
                spawn.assert_not_called()

    def test_git_launch_uses_checked_canonical_target_not_alias(self) -> None:
        alias = self.bin_dir / "git-alias"
        alias.symlink_to(self.executable)
        with mock.patch(
            "subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, b"ok\n", b""),
        ) as spawn:
            bootstrap._run_git(str(alias), str(self.home), "status")
            profile._run_git(str(alias), self.home, "status")
            project_git._git_output(str(alias), self.home, "--git-dir")
        self.assertEqual(spawn.call_count, 3)
        for call in spawn.call_args_list:
            self.assertEqual(call.args[0][0], str(self.executable.resolve()))

    def test_native_check_does_not_rehash_cached_product(self) -> None:
        self.approve()
        product_approvals.require_approved_executable(
            vendor="claude-code", product_bin=str(self.executable)
        )
        with mock.patch.object(
            product_executables,
            "_fingerprint_opened",
            side_effect=AssertionError("unexpected rehash"),
        ):
            product_approvals.require_approved_metadata(
                vendor="claude-code", product_bin=str(self.executable)
            )
