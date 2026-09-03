# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import concurrent.futures
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import cam1_transport
from tools.cam1lib import (
    compatibility,
    onboarding,
    participants,
    product_approvals,
    product_executables,
    project,
)

APPROVAL_PROCESS_HELPER = (
    Path(__file__).resolve().with_name("_product_approval_process.py")
)


class ProductApprovalTests(unittest.TestCase):
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

    def test_discovery_approval_status_and_require_never_execute_candidate(
        self,
    ) -> None:
        candidate = self.discover()
        self.assertFalse(self.marker.exists())
        result = self.approve()
        self.assertEqual(result["status"], "approved")
        status = product_approvals.approval_status(vendor="claude-code")
        self.assertEqual(len(status["active"]), 1)
        resolved, approval = product_approvals.require_approved_executable(
            vendor="claude-code",
            product_bin=str(self.executable),
        )
        self.assertEqual(resolved, candidate.canonical_path)
        self.assertEqual(approval["fingerprint_sha256"], candidate.fingerprint_sha256)
        self.assertFalse(self.marker.exists())

    def test_record_limit_rejects_append_without_poisoning_registry(self) -> None:
        second_executable = self.bin_dir / "codex"
        second_executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        second_executable.chmod(0o700)
        second_candidate = product_executables.discover_candidate(
            "codex",
            str(second_executable),
            allow_path_lookup=False,
        )

        with mock.patch.object(product_approvals, "MAX_REGISTRY_RECORDS", 1):
            self.approve()
            registry = product_approvals.registry_path()
            original = registry.read_bytes()
            with self.assertRaises(product_approvals.ProductApprovalError) as context:
                product_approvals.approve_candidate(
                    vendor="codex",
                    product_bin=str(second_executable),
                    expected_fingerprint_sha256=(second_candidate.fingerprint_sha256),
                    operator_reference="direct test operator confirmation",
                )
            self.assertEqual(
                context.exception.code,
                "product_approval.registry_limit",
            )
            self.assertEqual(registry.read_bytes(), original)
            status = product_approvals.approval_status()

        self.assertEqual(status["record_count"], 1)
        self.assertEqual(len(status["active"]), 1)

    def test_path_is_candidate_only_and_approval_requires_absolute_path(self) -> None:
        with mock.patch.dict(os.environ, {"PATH": str(self.bin_dir)}, clear=False):
            candidate = product_executables.discover_candidate("claude-code")
            self.assertEqual(candidate.source, "path_candidate")
            with self.assertRaises(product_approvals.ProductApprovalError) as context:
                product_approvals.approve_candidate(
                    vendor="claude-code",
                    product_bin="claude",
                    expected_fingerprint_sha256=candidate.fingerprint_sha256,
                    operator_reference="direct test operator confirmation",
                )
        self.assertEqual(
            context.exception.code, "product_approval.absolute_path_required"
        )
        self.assertFalse(self.marker.exists())

    def test_supplied_tilde_relative_and_terminal_control_paths_are_rejected(
        self,
    ) -> None:
        candidate = self.discover()
        with mock.patch.dict(
            os.environ,
            {"HOME": str(self.home), "PATH": str(self.bin_dir)},
            clear=False,
        ):
            for supplied in (
                "~/bin/claude",
                "bin/claude",
                f"{self.executable}\x1b[2J",
                f"{self.executable}\u202erorrim",
            ):
                with (
                    self.subTest(supplied=repr(supplied)),
                    self.assertRaises(product_approvals.ProductApprovalError),
                ):
                    product_executables.discover_candidate(
                        "claude-code",
                        supplied,
                        allow_path_lookup=True,
                    )
                with (
                    self.subTest(approval=repr(supplied)),
                    self.assertRaises(product_approvals.ProductApprovalError),
                ):
                    product_approvals.approve_candidate(
                        vendor="claude-code",
                        product_bin=supplied,
                        expected_fingerprint_sha256=candidate.fingerprint_sha256,
                        operator_reference="direct test operator confirmation",
                    )
        self.assertFalse(self.marker.exists())

    def test_fifo_is_rejected_before_open_for_hash_and_metadata_checks(self) -> None:
        fifo = self.bin_dir / "claude-fifo"
        os.mkfifo(fifo, mode=0o700)
        real_open = os.open

        def guarded_open(path: object, *args: object, **kwargs: object) -> int:
            if path == fifo.name and kwargs.get("dir_fd") is not None:
                raise AssertionError("FIFO must be rejected before open")
            return real_open(path, *args, **kwargs)

        with mock.patch.object(
            product_executables.os,
            "open",
            side_effect=guarded_open,
        ):
            operations = (
                lambda: product_executables.discover_candidate(
                    "claude-code", str(fifo), allow_path_lookup=False
                ),
                lambda: product_executables._metadata_opened(fifo),
            )
            for operation in operations:
                with (
                    self.subTest(operation=operation),
                    self.assertRaises(product_approvals.ProductApprovalError) as error,
                ):
                    operation()
                self.assertEqual(error.exception.code, "product_approval.file_type")

    def test_candidate_change_requires_a_fresh_card(self) -> None:
        candidate = self.discover()
        self.executable.write_text("#!/bin/sh\nexit 7\n", encoding="utf-8")
        self.executable.chmod(0o700)
        with self.assertRaises(product_approvals.ProductApprovalError) as context:
            product_approvals.approve_candidate(
                vendor="claude-code",
                product_bin=str(self.executable),
                expected_fingerprint_sha256=candidate.fingerprint_sha256,
                operator_reference="direct test operator confirmation",
            )
        self.assertEqual(context.exception.code, "product_approval.candidate_changed")
        self.assertEqual(product_approvals.approval_status()["active"], [])

    def test_fingerprint_drift_and_symlink_retarget_fail_closed(self) -> None:
        self.approve()
        self.executable.write_text("#!/bin/sh\nexit 9\n", encoding="utf-8")
        self.executable.chmod(0o700)
        with self.assertRaises(product_approvals.ProductApprovalError) as drift:
            product_approvals.require_approved_executable(
                vendor="claude-code", product_bin=str(self.executable)
            )
        self.assertEqual(drift.exception.code, "product_approval.drift")

        approved_target = self.bin_dir / "approved-target"
        approved_target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        approved_target.chmod(0o700)
        replacement = self.bin_dir / "replacement"
        replacement.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        replacement.chmod(0o700)
        link = self.bin_dir / "current-claude"
        link.symlink_to(approved_target)
        fresh = product_executables.discover_candidate(
            "claude-code", str(link), allow_path_lookup=False
        )
        product_approvals.approve_candidate(
            vendor="claude-code",
            product_bin=str(link),
            expected_fingerprint_sha256=fresh.fingerprint_sha256,
            operator_reference="direct approval after changed-file review",
        )
        link.unlink()
        link.symlink_to(replacement)
        with self.assertRaises(product_approvals.ProductApprovalError) as retargeted:
            product_approvals.require_approved_executable(
                vendor="claude-code", product_bin=str(link)
            )
        self.assertEqual(retargeted.exception.code, "product_approval.required")

        first_directory = self.bin_dir / "first"
        second_directory = self.bin_dir / "second"
        first_directory.mkdir()
        second_directory.mkdir()
        for directory, exit_code in (
            (first_directory, 0),
            (second_directory, 1),
        ):
            executable = directory / "claude"
            executable.write_text(
                f"#!/bin/sh\nexit {exit_code}\n",
                encoding="utf-8",
            )
            executable.chmod(0o700)
        directory_link = self.bin_dir / "selected"
        directory_link.symlink_to(first_directory, target_is_directory=True)
        ancestor_candidate = product_executables.discover_candidate(
            "claude-code",
            str(directory_link / "claude"),
            allow_path_lookup=False,
        )
        product_approvals.approve_candidate(
            vendor="claude-code",
            product_bin=ancestor_candidate.canonical_path,
            expected_fingerprint_sha256=ancestor_candidate.fingerprint_sha256,
            operator_reference="direct approval of resolved ancestor target",
        )
        directory_link.unlink()
        directory_link.symlink_to(second_directory, target_is_directory=True)
        with self.assertRaises(product_approvals.ProductApprovalError) as ancestor_swap:
            product_approvals.require_approved_executable(
                vendor="claude-code",
                product_bin=str(directory_link / "claude"),
            )
        self.assertEqual(ancestor_swap.exception.code, "product_approval.required")

    def test_revoke_is_guarded_and_append_only(self) -> None:
        result = self.approve()
        approval = result["approval"]
        revoked = product_approvals.revoke_approval(
            vendor="claude-code",
            product_bin=str(self.executable),
            approval_record_id=approval["record_id"],
            expected_fingerprint_sha256=approval["attributes"]["fingerprint_sha256"],
            operator_reference="direct test revocation",
        )
        self.assertEqual(revoked["status"], "revoked")
        status = product_approvals.approval_status()
        self.assertEqual(status["record_count"], 2)
        self.assertEqual(status["active"], [])
        with self.assertRaises(product_approvals.ProductApprovalError) as context:
            product_approvals.require_approved_executable(
                vendor="claude-code", product_bin=str(self.executable)
            )
        self.assertEqual(context.exception.code, "product_approval.required")

    def test_cached_metadata_recheck_replays_only_after_registry_change(self) -> None:
        candidate = product_executables.discover_candidate(
            "claude-code", str(self.executable), allow_path_lookup=False
        )
        approved = product_approvals.approve_candidate(
            vendor="claude-code",
            product_bin=str(self.executable),
            expected_fingerprint_sha256=candidate.fingerprint_sha256,
            operator_reference="direct test operator confirmation",
        )
        product_approvals.require_approved_executable(
            vendor="claude-code",
            product_bin=str(self.executable),
        )
        with mock.patch.object(
            product_approvals,
            "_verify",
            wraps=product_approvals._verify,
        ) as verify:
            product_approvals.require_approved_metadata(
                vendor="claude-code",
                product_bin=str(self.executable),
            )
            self.assertEqual(verify.call_count, 0)
            approval = approved["approval"]
            product_approvals.revoke_approval(
                vendor="claude-code",
                product_bin=str(self.executable),
                approval_record_id=approval["record_id"],
                expected_fingerprint_sha256=approval["attributes"][
                    "fingerprint_sha256"
                ],
                operator_reference="direct test revocation after cache",
            )
            with self.assertRaises(product_approvals.ProductApprovalError) as error:
                product_approvals.require_approved_metadata(
                    vendor="claude-code",
                    product_bin=str(self.executable),
                )
        self.assertEqual(error.exception.code, "product_approval.required")
        self.assertEqual(verify.call_count, 2)

    def test_metadata_recheck_requires_an_operation_local_full_attestation(
        self,
    ) -> None:
        self.approve()
        with product_approvals._VERIFIED_APPROVALS_LOCK:
            product_approvals._VERIFIED_APPROVALS.clear()
        with self.assertRaises(product_approvals.ProductApprovalError) as missing:
            product_approvals.require_approved_metadata(
                vendor="claude-code",
                product_bin=str(self.executable),
            )
        self.assertEqual(
            missing.exception.code,
            "product_approval.attestation_missing",
        )
        product_approvals.require_approved_executable(
            vendor="claude-code",
            product_bin=str(self.executable),
        )
        product_approvals.require_approved_metadata(
            vendor="claude-code",
            product_bin=str(self.executable),
        )

    def test_concurrent_identical_approval_appends_once(self) -> None:
        candidate = self.discover()

        def approve() -> str:
            return product_approvals.approve_candidate(
                vendor="claude-code",
                product_bin=str(self.executable),
                expected_fingerprint_sha256=candidate.fingerprint_sha256,
                operator_reference="same direct concurrent confirmation",
            )["status"]

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            statuses = list(executor.map(lambda _index: approve(), range(16)))
        self.assertEqual(statuses.count("approved"), 1)
        self.assertEqual(statuses.count("already_approved"), 15)
        self.assertEqual(product_approvals.approval_status()["record_count"], 1)

    def test_cross_process_identical_approval_appends_once(self) -> None:
        candidate = self.discover()
        gate = self.home / "start-approval-processes"
        command = [
            sys.executable,
            str(APPROVAL_PROCESS_HELPER),
            str(self.home),
            str(gate),
            "claude-code",
            str(self.executable),
            candidate.fingerprint_sha256,
        ]
        processes = [
            subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(8)
        ]
        gate.touch(mode=0o600)
        outputs: list[dict[str, str]] = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=20)
            self.assertEqual(process.returncode, 0, stderr)
            outputs.append(json.loads(stdout))

        statuses = [output["status"] for output in outputs]
        self.assertEqual(statuses.count("approved"), 1)
        self.assertEqual(statuses.count("already_approved"), 7)
        self.assertEqual(product_approvals.approval_status()["record_count"], 1)

    def test_registry_rejects_insecure_mode_and_tampered_chain(self) -> None:
        self.approve()
        path = self.home / "CAM" / "Approvals" / product_approvals.REGISTRY_NAME
        path.chmod(0o644)
        with self.assertRaises(product_approvals.ProductApprovalError) as insecure:
            product_approvals.approval_status()
        self.assertEqual(insecure.exception.code, "approval.registry.mode")

        path.chmod(0o600)
        raw = bytearray(path.read_bytes())
        offset = raw.index(b"direct test")
        raw[offset] = ord("D")
        path.write_bytes(raw)
        path.chmod(0o600)
        with self.assertRaises(product_approvals.ProductApprovalError):
            product_approvals.approval_status()

    def test_registry_replay_enforces_grandfather_migration_pairing(self) -> None:
        self.approve()
        path = self.home / "CAM" / "Approvals" / product_approvals.REGISTRY_NAME
        record = json.loads(path.read_text(encoding="utf-8"))
        record["attributes"]["basis"] = "grandfathered_roster"
        unsigned = dict(record)
        unsigned.pop("record_sha256")
        record["record_sha256"] = product_approvals._digest(unsigned)
        path.write_bytes(product_approvals._canonical_json(record) + b"\n")
        path.chmod(0o600)
        with self.assertRaises(product_approvals.ProductApprovalError) as error:
            product_approvals.approval_status()
        self.assertEqual(error.exception.code, "product_approval.migration")

    def test_registry_replay_rejects_unsafe_paths_and_noncanonical_metadata(
        self,
    ) -> None:
        self.approve()
        path = self.home / "CAM" / "Approvals" / product_approvals.REGISTRY_NAME
        original = json.loads(path.read_text(encoding="utf-8"))

        def write_record(record: dict[str, object]) -> None:
            unsigned = dict(record)
            unsigned.pop("record_sha256")
            record["record_sha256"] = product_approvals._digest(unsigned)
            path.write_bytes(product_approvals._canonical_json(record) + b"\n")
            path.chmod(0o600)

        for unsafe_path in ("/tmp/claude\x1b[2J", "/tmp/claude\u202eevil"):
            record = json.loads(json.dumps(original))
            attributes = record["attributes"]
            attributes["canonical_path"] = unsafe_path
            fingerprint = product_approvals.ExecutableFingerprint(
                **attributes["fingerprint"]
            )
            attributes["fingerprint_sha256"] = product_approvals._candidate_digest(
                attributes["vendor"],
                unsafe_path,
                fingerprint,
            )
            write_record(record)
            with (
                self.subTest(unsafe_path=repr(unsafe_path)),
                self.assertRaises(product_approvals.ProductApprovalError),
            ):
                product_approvals.approval_status()

        unsafe_reference = json.loads(json.dumps(original))
        unsafe_reference["attributes"]["operator_reference"] = (
            "operator approved\u202ereversed"
        )
        write_record(unsafe_reference)
        with self.assertRaises(product_approvals.ProductApprovalError):
            product_approvals.approval_status()

        noncanonical_cases = (
            ("record_id", "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"),
            ("recorded_at", original["recorded_at"].replace("Z", "+00:00")),
        )
        for field, value in noncanonical_cases:
            record = json.loads(json.dumps(original))
            record[field] = value
            write_record(record)
            with (
                self.subTest(field=field),
                self.assertRaises(product_approvals.ProductApprovalError),
            ):
                product_approvals.approval_status()

    def test_home_environment_does_not_choose_registry_location(self) -> None:
        hostile = self.home / "hostile-home"
        with mock.patch.dict(os.environ, {"HOME": str(hostile)}, clear=False):
            self.approve()
        expected = self.home / "CAM" / "Approvals" / product_approvals.REGISTRY_NAME
        self.assertTrue(expected.is_file())
        self.assertFalse(hostile.exists())

    def test_status_never_hashes_or_executes_the_product(self) -> None:
        self.approve()
        with mock.patch.object(
            product_executables,
            "_fingerprint_opened",
            side_effect=AssertionError("status must not hash a product"),
        ):
            status = product_approvals.approval_status(
                vendor="claude-code",
                product_bin=str(self.executable),
            )
        self.assertEqual(len(status["active"]), 1)
        self.assertFalse(self.marker.exists())

    def test_status_and_revoke_report_symlink_loops_as_bounded_errors(self) -> None:
        loop = self.bin_dir / "loop"
        loop.symlink_to(loop)
        operations = (
            lambda: product_approvals.approval_status(
                vendor="claude-code",
                product_bin=str(loop),
            ),
            lambda: product_approvals.revoke_approval(
                vendor="claude-code",
                product_bin=str(loop),
                approval_record_id="00000000-0000-4000-8000-000000000001",
                expected_fingerprint_sha256="a" * 64,
                operator_reference="direct test revocation",
            ),
        )
        for operation in operations:
            with (
                self.subTest(operation=operation),
                self.assertRaises(product_approvals.ProductApprovalError) as error,
            ):
                operation()
            self.assertEqual(error.exception.code, "product_approval.path")

    def test_failed_append_removes_partial_record(self) -> None:
        candidate = self.discover()
        original_write = product_approvals._write_all

        def partial_write(descriptor: int, raw: bytes) -> None:
            original_write(descriptor, raw[:17])
            raise project.ProjectError("state.write", "injected partial write")

        with (
            mock.patch.object(product_approvals, "_write_all", partial_write),
            self.assertRaises(product_approvals.ProductApprovalError) as context,
        ):
            product_approvals.approve_candidate(
                vendor="claude-code",
                product_bin=str(self.executable),
                expected_fingerprint_sha256=candidate.fingerprint_sha256,
                operator_reference="direct test operator confirmation",
            )
        self.assertEqual(context.exception.code, "product_approval.write")
        self.assertEqual(product_approvals.approval_status()["record_count"], 0)


class ProductApprovalTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        ProductApprovalTests.setUp(self)
        self.codex = self.bin_dir / "codex"
        self.codex.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.codex.chmod(0o700)

    def approve_both(self) -> None:
        for vendor, executable in (
            ("claude-code", self.executable),
            ("codex", self.codex),
        ):
            candidate = product_executables.discover_candidate(
                vendor, str(executable), allow_path_lookup=False
            )
            product_approvals.approve_candidate(
                vendor=vendor,
                product_bin=str(executable),
                expected_fingerprint_sha256=candidate.fingerprint_sha256,
                operator_reference="direct test operator confirmation",
            )

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

    def test_claude_onboarding_rechecks_metadata_immediately_before_product(
        self,
    ) -> None:
        events: list[str] = []
        discovered = mock.sentinel.discovered

        def full_approval(**_kwargs: object) -> tuple[str, dict[str, object]]:
            events.append("full")
            return str(self.executable), {}

        def metadata_approval(**_kwargs: object) -> tuple[str, dict[str, object]]:
            events.append("metadata")
            return str(self.executable), {}

        def run(*_args: object, **_kwargs: object) -> mock.Mock:
            events.append("product")
            return mock.Mock(returncode=0, stdout=b"{}")

        with (
            mock.patch.object(
                product_approvals,
                "require_approved_executable",
                side_effect=full_approval,
            ),
            mock.patch.object(
                product_approvals,
                "require_approved_metadata",
                side_effect=metadata_approval,
            ),
            mock.patch.object(onboarding.subprocess, "run", side_effect=run),
            mock.patch.object(onboarding.routing, "parse_agent_view_sessions"),
            mock.patch.object(
                onboarding.routing,
                "select_agent_view_identity_session",
                return_value=discovered,
            ),
        ):
            result = onboarding._claude_agent_view(
                str(self.executable),
                "00000000-0000-4000-8000-000000000101",
            )
        self.assertIs(result, discovered)
        self.assertEqual(events, ["full", "metadata", "product"])

    def test_codex_onboarding_requires_account_approval_before_proposal_data(
        self,
    ) -> None:
        binding = mock.Mock()
        binding.project_id = "00000000-0000-4000-8000-000000000301"
        binding.display_name = "test"
        binding.git_top_level = self.home
        binding.git_common_dir = self.home / ".git"
        binding.worktree_id = "main"
        binding.git_bin = "/usr/bin/git"
        source_profile = mock.Mock(validation_profile_sha256="b" * 64)
        git_context = mock.Mock(
            top_level=binding.git_top_level,
            common_dir=binding.git_common_dir,
        )
        with (
            mock.patch.object(
                onboarding,
                "require_trusted_source",
                return_value=source_profile,
            ),
            mock.patch.object(
                onboarding,
                "_session_identifier",
                return_value=(
                    "00000000-0000-4000-8000-000000000101",
                    "explicit_session_id",
                ),
            ),
            mock.patch.object(
                onboarding,
                "_resolved_executable",
                return_value=(str(self.codex), "explicit_candidate"),
            ),
            mock.patch.object(
                product_approvals,
                "require_approved_executable",
                side_effect=product_approvals.ProductApprovalError(
                    "product_approval.required",
                    "approval required",
                ),
            ) as require_approval,
            mock.patch.object(
                onboarding.project,
                "discover_git_context",
                return_value=git_context,
            ) as discover_git,
            self.assertRaises(onboarding.CamUsageError) as error,
        ):
            onboarding.inspect_self(
                binding,
                vendor="codex",
                session_id="00000000-0000-4000-8000-000000000101",
            )
        self.assertEqual(error.exception.code, "product_approval.required")
        require_approval.assert_called_once()
        discover_git.assert_not_called()

    def test_doctor_hashes_each_product_once_then_uses_metadata_rechecks(self) -> None:
        self.approve_both()
        successful_probe = {"ok": True, "exit_code": 0, "output": "test"}
        with (
            mock.patch.object(
                product_executables,
                "_fingerprint_opened",
                wraps=product_executables._fingerprint_opened,
            ) as fingerprint,
            mock.patch.object(
                product_approvals,
                "_metadata_opened",
                wraps=product_approvals._metadata_opened,
            ) as metadata,
            mock.patch.object(
                product_approvals,
                "_verify",
                wraps=product_approvals._verify,
            ) as verify,
            mock.patch.object(
                cam1_transport,
                "_require_live_validation_profile",
                return_value=({}, False),
            ),
            mock.patch.object(
                cam1_transport,
                "_resolve_project",
                side_effect=project.ProjectError("project.missing", "test"),
            ),
            mock.patch.object(
                cam1_transport, "_run_probe_before", return_value=successful_probe
            ),
            mock.patch.object(
                cam1_transport,
                "_agent_view_probe_before",
                return_value={"ok": True, "sessions": 1},
            ),
            mock.patch.object(
                cam1_transport, "_mcp_sdk_check", return_value=(True, "2.1.0")
            ),
            mock.patch.object(
                cam1_transport,
                "_with_validation_profile",
                side_effect=lambda payload: payload,
            ),
            mock.patch.object(cam1_transport, "_emit") as emit,
        ):
            returncode = cam1_transport.main(
                [
                    "--claude-bin",
                    str(self.executable),
                    "--codex-bin",
                    str(self.codex),
                    "doctor",
                ]
            )
        self.assertEqual(returncode, 0)
        self.assertEqual(fingerprint.call_count, 2)
        self.assertEqual(verify.call_count, 2)
        # Native doctor re-establishes both approval attestations from the
        # operation-local cache, then performs five immediate pre-probe checks.
        self.assertEqual(metadata.call_count, 7)
        payload = emit.call_args.args[0]
        self.assertTrue(payload["ok"])
        for label in ("claude", "codex"):
            approval = payload["checks"][label]["approval"]
            expected_vendor = "claude-code" if label == "claude" else "codex"
            self.assertEqual(approval["vendor"], expected_vendor)
            self.assertIn("record_id", approval)
            self.assertIn("record_sha256", approval)
            self.assertIn("fingerprint_sha256", approval)
        self.assertFalse(self.marker.exists())

    def test_each_product_command_fails_before_product_io_when_unapproved(self) -> None:
        cases = (
            ("doctor", ["doctor"], "doctor"),
            ("claude-list", ["claude-list"], "list_local_peers"),
            (
                "claude-preflight",
                ["claude-preflight", "--participant", "worker"],
                "preflight_project_claude",
            ),
            (
                "claude-send",
                [
                    "claude-send",
                    "--participant",
                    "worker",
                    "--envelope",
                    "/not/read.json",
                ],
                "send_project_claude",
            ),
            (
                "codex-send",
                [
                    "codex-send",
                    "--participant",
                    "worker",
                    "--envelope",
                    "/not/read.json",
                ],
                "send_project_codex",
            ),
        )
        for name, arguments, endpoint in cases:
            with (
                self.subTest(command=name),
                mock.patch.object(
                    cam1_transport,
                    "_require_live_validation_profile",
                    return_value=({}, False),
                ),
                mock.patch.object(
                    cam1_transport,
                    "_resolve_project",
                    return_value=mock.sentinel.binding,
                ),
                mock.patch.object(
                    cam1_transport,
                    "resolve_product_binary",
                    side_effect=cam1_transport.TransportError(
                        "product_approval.required", "approval required"
                    ),
                ),
                mock.patch.object(cam1_transport, endpoint) as product_operation,
                mock.patch.object(
                    cam1_transport,
                    "_with_validation_profile",
                    side_effect=lambda payload: payload,
                ),
                mock.patch.object(cam1_transport, "_emit"),
            ):
                returncode = cam1_transport.main(arguments)
            self.assertEqual(returncode, 2)
            product_operation.assert_not_called()

    def test_product_discover_cli_emits_card_without_execution(self) -> None:
        returncode, payload = self.invoke_product_cli(
            "product-discover",
            "--vendor",
            "claude-code",
            "--product-bin",
            str(self.executable),
        )
        self.assertEqual(returncode, 0)
        self.assertEqual(payload["status"], "approval_candidate")
        self.assertEqual(payload["approval_arguments"][0], "product-approve")
        self.assertFalse(self.marker.exists())

    def test_product_cli_rejects_unreplaced_operator_reference_without_mutation(
        self,
    ) -> None:
        returncode, discovered = self.invoke_product_cli(
            "product-discover",
            "--vendor",
            "claude-code",
            "--product-bin",
            str(self.executable),
        )
        self.assertEqual(returncode, 0)
        returncode, rejected = self.invoke_product_cli(
            *discovered["approval_arguments"]
        )
        self.assertEqual(returncode, 2)
        self.assertEqual(
            rejected["error"]["code"],
            "product_approval.operator_reference_reserved",
        )
        self.assertFalse((self.home / "CAM").exists())

        candidate = product_executables.discover_candidate(
            "claude-code", str(self.executable), allow_path_lookup=False
        )
        approved = product_approvals.approve_candidate(
            vendor="claude-code",
            product_bin=str(self.executable),
            expected_fingerprint_sha256=candidate.fingerprint_sha256,
            operator_reference="direct test operator confirmation",
        )
        approval = approved["approval"]
        returncode, rejected = self.invoke_product_cli(
            "product-revoke",
            "--vendor",
            "claude-code",
            "--product-bin",
            str(self.executable),
            "--approval-record-id",
            approval["record_id"],
            "--expected-fingerprint-sha256",
            approval["attributes"]["fingerprint_sha256"],
            "--operator-reference",
            "DIRECT_OPERATOR_REFERENCE",
        )
        self.assertEqual(returncode, 2)
        self.assertEqual(
            rejected["error"]["code"],
            "product_approval.operator_reference_reserved",
        )
        status = product_approvals.approval_status(vendor="claude-code")
        self.assertEqual(status["record_count"], 1)
        self.assertEqual(len(status["active"]), 1)

    def test_product_cli_guides_guarded_reapproval_after_path_drift(self) -> None:
        returncode, discovered = self.invoke_product_cli(
            "product-discover",
            "--vendor",
            "claude-code",
            "--product-bin",
            str(self.executable),
        )
        self.assertEqual(returncode, 0)
        approval_arguments = list(discovered["approval_arguments"])
        approval_arguments[-1] = "direct initial executable approval"
        returncode, approved = self.invoke_product_cli(*approval_arguments)
        self.assertEqual(returncode, 0)
        self.assertEqual(approved["status"], "approved")

        self.executable.write_text("#!/bin/sh\nexit 17\n", encoding="utf-8")
        self.executable.chmod(0o700)
        returncode, replacement = self.invoke_product_cli(
            "product-discover",
            "--vendor",
            "claude-code",
            "--product-bin",
            str(self.executable),
        )
        self.assertEqual(returncode, 0)
        self.assertEqual(replacement["status"], "replacement_approval_required")
        self.assertEqual(
            replacement["existing_approval"]["record_id"],
            approved["approval"]["record_id"],
        )

        replacement_approval = list(replacement["approval_arguments"])
        replacement_approval[-1] = "direct replacement executable approval"
        returncode, drift = self.invoke_product_cli(*replacement_approval)
        self.assertEqual(returncode, 2)
        self.assertEqual(drift["error"]["code"], "product_approval.drift")

        revocation_arguments = list(replacement["revocation_arguments"])
        revocation_arguments[-1] = "direct superseded executable revocation"
        returncode, revoked = self.invoke_product_cli(*revocation_arguments)
        self.assertEqual(returncode, 0)
        self.assertEqual(revoked["status"], "revoked")

        returncode, rediscovered = self.invoke_product_cli(
            "product-discover",
            "--vendor",
            "claude-code",
            "--product-bin",
            str(self.executable),
        )
        self.assertEqual(returncode, 0)
        self.assertEqual(rediscovered["status"], "approval_candidate")
        replacement_approval = list(rediscovered["approval_arguments"])
        replacement_approval[-1] = "direct replacement executable approval"
        returncode, reapproved = self.invoke_product_cli(*replacement_approval)
        self.assertEqual(returncode, 0)
        self.assertEqual(reapproved["status"], "approved")

        returncode, status = self.invoke_product_cli(
            "product-status",
            "--vendor",
            "claude-code",
            "--product-bin",
            str(self.executable),
        )
        self.assertEqual(returncode, 0)
        self.assertEqual(len(status["active"]), 1)
        self.assertEqual(
            status["active"][0]["attributes"]["fingerprint_sha256"],
            rediscovered["candidate"]["fingerprint_sha256"],
        )
        self.assertFalse(self.marker.exists())

    def test_legacy_roster_path_is_grandfathered_once_and_reused_cross_project(
        self,
    ) -> None:
        canonical_executable = product_executables.discover_candidate(
            "claude-code", str(self.executable), allow_path_lookup=False
        ).canonical_path
        participant = participants.Participant(
            participant_id="00000000-0000-4000-8000-000000000201",
            common_name="legacy-claude",
            display_name="Legacy Claude",
            role=None,
            vendor="claude-code",
            approved_product_executable=canonical_executable,
            status=participants.ParticipantStatus.BOUND,
            binding=participants.SessionBinding(
                generation=3,
                session_id="00000000-0000-4000-8000-000000000101",
                session_label="legacy-claude",
                session_kind="interactive",
                operator_reference="direct historical operator confirmation",
                bound_at="2026-09-01T00:00:00Z",
            ),
        )
        binding = mock.Mock()
        binding.project_id = "00000000-0000-4000-8000-000000000301"
        store = mock.Mock()
        snapshot = store.snapshot.return_value
        snapshot.roster.participants = {participant.participant_id: participant}
        proposal = mock.Mock()
        proposal.participant_id = participant.participant_id
        proposal.status.value = "confirmed"
        proposal.operator_reference = "direct historical product confirmation"
        proposal.execution_context.product_executable = canonical_executable
        proposal.execution_context.validation_profile_sha256 = next(
            iter(cam1_transport.LEGACY_PRODUCT_APPROVAL_PROFILES)
        )
        proposal.confirmed_at = "2026-09-01T00:00:01Z"
        proposal.proposal_id = "00000000-0000-4000-8000-000000000401"
        snapshot.enrollment.proposals = {proposal.proposal_id: proposal}
        with (
            mock.patch.object(cam1_transport.state, "StateStore", return_value=store),
            mock.patch.object(
                product_executables,
                "_fingerprint_opened",
                wraps=product_executables._fingerprint_opened,
            ) as fingerprint,
        ):
            resolved = cam1_transport.resolve_product_binary(
                str(self.executable), vendor="claude-code", binding=binding
            )
        self.assertEqual(resolved, canonical_executable)
        self.assertEqual(fingerprint.call_count, 1)
        status = product_approvals.approval_status(vendor="claude-code")
        self.assertEqual(status["record_count"], 1)
        approval = status["active"][0]
        self.assertEqual(approval["attributes"]["basis"], "grandfathered_roster")
        self.assertEqual(
            approval["attributes"]["migration"],
            {
                "project_id": binding.project_id,
                "participant_id": participant.participant_id,
                "binding_generation": 3,
                "source": "confirmed_enrollment",
                "source_reference": proposal.proposal_id,
            },
        )

        other_binding = mock.Mock()
        other_binding.project_id = "00000000-0000-4000-8000-000000000302"
        with mock.patch.object(cam1_transport.state, "StateStore") as state_store:
            reused = cam1_transport.resolve_product_binary(
                str(self.executable), vendor="claude-code", binding=other_binding
            )
        self.assertEqual(reused, canonical_executable)
        state_store.assert_not_called()
        self.assertEqual(product_approvals.approval_status()["record_count"], 1)

        product_approvals.revoke_approval(
            vendor="claude-code",
            product_bin=canonical_executable,
            approval_record_id=approval["record_id"],
            expected_fingerprint_sha256=approval["attributes"]["fingerprint_sha256"],
            operator_reference="direct revocation after migration test",
        )
        with (
            mock.patch.object(cam1_transport.state, "StateStore", return_value=store),
            self.assertRaises(cam1_transport.TransportError) as migration_reuse,
        ):
            cam1_transport.resolve_product_binary(
                str(self.executable),
                vendor="claude-code",
                binding=binding,
            )
        self.assertEqual(
            migration_reuse.exception.code,
            "product_approval.grandfather_used",
        )

    def test_new_or_unknown_profile_cannot_use_legacy_grandfathering(self) -> None:
        canonical_executable = product_executables.resolve_candidate_path(
            "claude-code",
            str(self.executable),
            allow_path_lookup=False,
        )[0]
        participant = participants.Participant(
            participant_id="00000000-0000-4000-8000-000000000211",
            common_name="new-claude",
            display_name="New Claude",
            role=None,
            vendor="claude-code",
            approved_product_executable=canonical_executable,
            status=participants.ParticipantStatus.BOUND,
            binding=participants.SessionBinding(
                generation=1,
                session_id="00000000-0000-4000-8000-000000000111",
                session_label="new-claude",
                session_kind="interactive",
                operator_reference="direct recent operator confirmation",
                bound_at="2026-09-02T00:00:00Z",
            ),
        )
        binding = mock.Mock()
        binding.project_id = "00000000-0000-4000-8000-000000000311"
        store = mock.Mock()
        snapshot = store.snapshot.return_value
        snapshot.roster.participants = {participant.participant_id: participant}
        proposal = mock.Mock()
        proposal.participant_id = participant.participant_id
        proposal.status.value = "confirmed"
        proposal.operator_reference = "direct recent product confirmation"
        proposal.execution_context.product_executable = canonical_executable
        proposal.execution_context.validation_profile_sha256 = "a" * 64
        proposal.confirmed_at = "2026-09-02T00:00:01Z"
        proposal.proposal_id = "00000000-0000-4000-8000-000000000411"
        snapshot.enrollment.proposals = {proposal.proposal_id: proposal}
        with (
            mock.patch.object(cam1_transport.state, "StateStore", return_value=store),
            self.assertRaises(cam1_transport.TransportError) as error,
        ):
            cam1_transport.resolve_product_binary(
                str(self.executable),
                vendor="claude-code",
                binding=binding,
            )
        self.assertEqual(error.exception.code, "product_approval.required")
        self.assertEqual(product_approvals.approval_status()["active"], [])

    def test_capability_is_a_local_prerequisite_not_an_active_gate(self) -> None:
        capability = compatibility.PRODUCT_EXECUTABLE_PREAPPROVAL_CAPABILITY
        self.assertIn(capability, compatibility.SUPPORTED_READER_CAPABILITIES)
        self.assertIsNone(
            compatibility.CompatibilityProjection().active_gate(
                compatibility.PRODUCT_EXECUTABLE_PREAPPROVAL_FEATURE_ID
            )
        )


if __name__ == "__main__":
    unittest.main()
