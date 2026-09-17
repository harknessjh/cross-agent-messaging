# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests._native_executable_fixture import write_native
from tests._product_approval_test_case import ProductApprovalTestCase
from tools import cam1_transport
from tools.cam1lib import (
    onboarding,
    product_approvals,
)
from tools.cam1lib import (
    product_installations as installations,
)


class ProductInstallationTests(ProductApprovalTestCase):
    def setUp(self) -> None:
        super().setUp()
        product_approvals.begin_operation()
        self.addCleanup(product_approvals.begin_operation)
        self.root = self.home.resolve() / "product"
        self.root.mkdir(mode=0o700)
        self.v1, self.v2 = self.root / "v1", self.root / "v2"
        write_native(self.v1, "version 1")
        write_native(self.v2, "version 2")
        self.launcher = self.bin_dir.resolve() / "stable-claude"
        self.launcher.symlink_to(self.v1)

    def card(self):
        return installations.discover(
            vendor="claude-code",
            launcher=str(self.launcher),
            installation_root=str(self.root),
        )

    def approve_installation(self):
        card = self.card()
        return installations.approve(
            vendor="claude-code",
            launcher=str(self.launcher),
            installation_root=str(self.root),
            expected_card_sha256=card["card_sha256"],
            operator_reference="direct synthetic installation choice",
        )

    def resolve(self, path=None):
        return product_approvals.require_approved_executable(
            vendor="claude-code", product_bin=str(path or self.launcher)
        )

    def update(self, target=None):
        self.launcher.unlink()
        self.launcher.symlink_to(target or self.v2)

    def approve_strict(self, path):
        candidate = product_approvals.discover_candidate("claude-code", str(path))
        return product_approvals.approve_candidate(
            vendor="claude-code",
            product_bin=str(path),
            expected_fingerprint_sha256=candidate.fingerprint_sha256,
            operator_reference="direct synthetic strict choice",
        )

    def test_normal_update_reuses_policy_without_any_ledger_write(self):
        with mock.patch(
            "subprocess.run", side_effect=AssertionError("must not execute")
        ):
            approved = self.approve_installation()
            before = Path(approved["registry"]).read_bytes()
            first, first_evidence = self.resolve()
            self.assertEqual(first, str(self.v1))
            self.update()
            product_approvals.begin_operation()
            second, second_evidence = self.resolve()
            self.assertEqual(second, str(self.v2))
            self.assertEqual(first_evidence["record_id"], second_evidence["record_id"])
            self.assertNotEqual(
                first_evidence["fingerprint_sha256"],
                second_evidence["fingerprint_sha256"],
            )
            self.assertFalse(second_evidence["fingerprint_is_release_approval"])
            self.assertEqual(Path(approved["registry"]).read_bytes(), before)
            self.assertFalse(
                (
                    Path(approved["registry"]).parent / product_approvals.REGISTRY_NAME
                ).exists()
            )

    def test_update_during_operation_stops_before_prelaunch(self):
        self.approve_installation()
        target, _ = self.resolve()
        self.update()
        with self.assertRaises(product_approvals.ProductApprovalError) as error:
            product_approvals.require_approved_metadata(
                vendor="claude-code", product_bin=target
            )
        self.assertEqual(error.exception.code, "installation.drift")

    def shifted_root(self, **changes):
        inspect = installations._directory

        def observe(path, **kwargs):
            result = inspect(path, **kwargs)
            return {**result, **changes} if path == self.root else result

        return mock.patch.object(installations, "_directory", side_effect=observe)

    def test_root_device_drift_between_operations_needs_no_new_approval(self):
        approved = self.approve_installation()
        before = Path(approved["registry"]).read_bytes()
        target, evidence = self.resolve()
        policy = evidence["selection"]["root_identity"]
        self.assertIn("filesystem", policy)
        self.assertNotIn("dev", policy)
        product_approvals.begin_operation()
        with self.shifted_root(dev=evidence["root_device"] + 2):
            later, later_evidence = self.resolve()
            self.assertEqual(later, target)
            self.assertEqual(later_evidence["record_id"], evidence["record_id"])
            self.assertEqual(later_evidence["root_device"], evidence["root_device"] + 2)
            product_approvals.require_approved_metadata(
                vendor="claude-code", product_bin=target
            )
        self.assertEqual(Path(approved["registry"]).read_bytes(), before)

    def test_root_device_drift_within_operation_still_refuses(self):
        self.approve_installation()
        target, evidence = self.resolve()
        with self.shifted_root(dev=evidence["root_device"] + 2):
            with self.assertRaises(product_approvals.ProductApprovalError) as error:
                product_approvals.require_approved_metadata(
                    vendor="claude-code", product_bin=target
                )
        self.assertEqual(error.exception.code, "installation.drift")

    def test_changed_filesystem_inode_or_owner_remains_a_root_mismatch(self):
        self.approve_installation()
        _, evidence = self.resolve()
        identity = evidence["selection"]["root_identity"]
        filesystem = identity["filesystem"]
        changed_filesystem = filesystem[:-1] + ("1" if filesystem[-1] != "1" else "2")
        for changes in (
            {"filesystem": changed_filesystem},
            {"inode": identity["inode"] + 1},
            {"uid": identity["uid"] + 1},
        ):
            product_approvals.begin_operation()
            with self.subTest(changes=changes), self.shifted_root(**changes):
                with self.assertRaises(product_approvals.ProductApprovalError) as error:
                    self.resolve()
                self.assertEqual(error.exception.code, "installation.root_changed")

    def test_unavailable_filesystem_identity_is_not_ignored(self):
        self.approve_installation()
        with (
            mock.patch(
                "tools._cam1_executable.filesystem_identity",
                side_effect=OSError("unavailable"),
            ),
            mock.patch.object(installations, "discover_candidate") as fingerprint,
        ):
            with self.assertRaises(product_approvals.ProductApprovalError) as error:
                self.resolve()
        self.assertEqual(error.exception.code, "installation.directory")
        fingerprint.assert_not_called()

    def test_device_drift_between_card_and_approval_is_not_silently_accepted(self):
        card = self.card()
        with self.shifted_root(dev=card["root_device"] + 2):
            with self.assertRaises(product_approvals.ProductApprovalError) as error:
                installations.approve(
                    vendor="claude-code",
                    launcher=str(self.launcher),
                    installation_root=str(self.root),
                    expected_card_sha256=card["card_sha256"],
                    operator_reference="direct synthetic installation choice",
                )
        self.assertEqual(error.exception.code, "installation.card_stale")
        self.assertEqual(installations.status()["active"], [])

    def test_target_device_drift_within_operation_still_refuses(self):
        self.approve_installation()
        target, _ = self.resolve()
        observed = installations._metadata_opened(Path(target))
        observed["dev"] += 2
        with mock.patch.object(
            installations, "_metadata_opened", return_value=observed
        ):
            with self.assertRaises(product_approvals.ProductApprovalError) as error:
                product_approvals.require_approved_metadata(
                    vendor="claude-code", product_bin=target
                )
        self.assertEqual(error.exception.code, "installation.drift")

    def test_in_place_target_change_stops_current_but_not_next_operation(self):
        self.approve_installation()
        target, _ = self.resolve()
        write_native(self.v1, "replaced")
        with self.assertRaises(product_approvals.ProductApprovalError):
            product_approvals.require_approved_metadata(
                vendor="claude-code", product_bin=target
            )
        product_approvals.begin_operation()
        self.assertEqual(self.resolve()[0], target)

    def test_card_change_is_not_approval(self):
        card = self.card()
        self.update()
        with self.assertRaises(product_approvals.ProductApprovalError) as error:
            installations.approve(
                vendor="claude-code",
                launcher=str(self.launcher),
                installation_root=str(self.root),
                expected_card_sha256=card["card_sha256"],
                operator_reference="direct synthetic confirmation",
            )
        self.assertEqual(error.exception.code, "installation.card_stale")
        self.assertEqual(installations.status()["active"], [])

    def test_root_escape_and_root_replacement_fail(self):
        self.approve_installation()
        self.update(self.executable.resolve())
        with self.assertRaises(product_approvals.ProductApprovalError) as error:
            self.resolve()
        self.assertEqual(error.exception.code, "installation.escape")
        self.root.rename(self.root.with_name("old-installation"))
        self.root.mkdir(mode=0o700)
        write_native(self.v1)
        self.update(self.v1)
        with self.assertRaises(product_approvals.ProductApprovalError) as error:
            self.resolve()
        self.assertEqual(error.exception.code, "installation.root_changed")

    def test_unsafe_native_and_script_replacements_fail(self):
        self.approve_installation()
        self.v1.write_text("#!/bin/sh\nexit 0\n")
        with self.assertRaises(product_approvals.ProductApprovalError) as error:
            self.resolve()
        self.assertEqual(error.exception.code, "product_approval.native_required")
        write_native(self.v1)
        self.root.chmod(0o777)
        with self.assertRaises(product_approvals.ProductApprovalError) as error:
            self.resolve()
        self.assertEqual(error.exception.code, "installation.directory")

    def test_intermediate_symlink_parent_is_checked(self):
        self.approve_installation()
        intermediate = self.home.resolve() / "untrusted"
        intermediate.mkdir(mode=0o777)
        intermediate.chmod(0o777)
        alias = intermediate / "target"
        alias.symlink_to(self.v1)
        self.update(alias)
        with self.assertRaises(product_approvals.ProductApprovalError) as error:
            self.resolve()
        self.assertEqual(error.exception.code, "installation.directory")

    def test_version_directory_symlink_is_supported(self):
        release = self.root / "release"
        release.mkdir()
        target = release / "claude"
        write_native(target)
        current = self.root / "current"
        current.symlink_to("release", target_is_directory=True)
        self.update(current / "claude")
        self.approve_installation()
        self.assertEqual(self.resolve()[0], str(target))

    def test_revocation_does_not_fall_back_to_existing_strict_approval(self):
        candidate = product_approvals.discover_candidate("claude-code", str(self.v1))
        product_approvals.approve_candidate(
            vendor="claude-code",
            product_bin=str(self.v1),
            expected_fingerprint_sha256=candidate.fingerprint_sha256,
            operator_reference="strict fixture",
        )
        approval = self.approve_installation()["record"]
        self.resolve()
        installations.revoke(
            vendor="claude-code",
            launcher=str(self.launcher),
            approval_record_id=approval["record_id"],
            expected_policy_sha256=approval["attributes"]["policy_sha256"],
            operator_reference="revoke fixture",
        )
        with self.assertRaises(product_approvals.ProductApprovalError) as error:
            self.resolve()
        self.assertEqual(error.exception.code, "installation.revoked")
        self.assertEqual(self.resolve(self.v1)[0], str(self.v1))

    def test_preserves_stable_metadata_selection_but_not_strict_roster(self):
        self.approve_installation()
        self.assertEqual(
            onboarding.resolve_product_executable(
                str(self.launcher), vendor="claude-code"
            ),
            str(self.launcher),
        )
        target, _ = self.resolve()
        stable_participant = mock.Mock(
            vendor="claude-code", approved_product_executable=str(self.launcher)
        )
        cam1_transport._require_approved_product_executable(stable_participant, target)
        strict_participant = mock.Mock(
            vendor="claude-code", approved_product_executable=target
        )
        with self.assertRaises(cam1_transport.TransportError):
            cam1_transport._require_approved_product_executable(
                strict_participant, target
            )

    def test_direct_binary_cannot_implicitly_migrate_existing_roster(self):
        with self.assertRaises(product_approvals.ProductApprovalError) as error:
            installations.discover(
                vendor="claude-code",
                launcher=str(self.v1),
                installation_root=str(self.root),
            )
        self.assertEqual(error.exception.code, "installation.stable_launcher")

    def test_reused_strict_path_cannot_become_an_installation_launcher(self):
        for revoked in (False, True):
            with self.subTest(revoked=revoked):
                path = self.root / f"previous-strict-{revoked}"
                write_native(path)
                strict = self.approve_strict(path)
                if revoked:
                    approval = strict["approval"]
                    product_approvals.revoke_approval(
                        vendor="claude-code",
                        product_bin=str(path),
                        approval_record_id=approval["record_id"],
                        expected_fingerprint_sha256=approval["attributes"][
                            "fingerprint_sha256"
                        ],
                        operator_reference="direct synthetic revocation",
                    )
                registry = Path(strict["registry"])
                before = registry.read_bytes()
                path.rename(path.with_suffix(".old"))
                path.symlink_to(self.v2)
                with self.assertRaises(product_approvals.ProductApprovalError) as error:
                    installations.discover(
                        vendor="claude-code",
                        launcher=str(path),
                        installation_root=str(self.root),
                    )
                self.assertEqual(
                    error.exception.code, "installation.strict_path_conflict"
                )
                self.assertEqual(registry.read_bytes(), before)
                self.assertEqual(installations.status()["active"], [])

    def test_strict_absolute_alias_works_without_selecting_installation_trust(self):
        strict = self.approve_strict(self.v1)
        alias = str(self.root / ".." / self.root.name / self.v1.name)
        for with_installation in (False, True):
            with self.subTest(with_installation=with_installation):
                if with_installation:
                    self.approve_installation()
                product_approvals.begin_operation()
                target, evidence = self.resolve(alias)
                self.assertEqual(target, str(self.v1))
                self.assertEqual(evidence["record_id"], strict["approval"]["record_id"])
                self.assertEqual(evidence["basis"], "operator_confirmation")
                product_approvals.require_approved_metadata(
                    vendor="claude-code", product_bin=target
                )

    def test_history_added_after_card_or_before_append_prevents_approval(self):
        mutate = installations._mutate
        for phase in ("after_card", "before_append"):
            with self.subTest(phase=phase):
                self.launcher = self.bin_dir.resolve() / phase
                self.launcher.symlink_to(self.v1)
                card = self.card()

                def add_strict_history():
                    self.launcher.unlink()
                    write_native(self.launcher)
                    self.approve_strict(self.launcher)
                    self.update(self.v1)

                def mutate_after_history(**kwargs):
                    add_strict_history()
                    return mutate(**kwargs)

                if phase == "after_card":
                    add_strict_history()
                with mock.patch.object(
                    installations,
                    "_mutate",
                    side_effect=mutate_after_history
                    if phase == "before_append"
                    else mutate,
                ):
                    with self.assertRaises(
                        product_approvals.ProductApprovalError
                    ) as error:
                        installations.approve(
                            vendor="claude-code",
                            launcher=str(self.launcher),
                            installation_root=str(self.root),
                            expected_card_sha256=card["card_sha256"],
                            operator_reference="direct synthetic installation choice",
                        )
                self.assertEqual(
                    error.exception.code, "installation.strict_path_conflict"
                )
                self.assertEqual(installations.status()["active"], [])

    def test_existing_conflicting_installation_fails_resolution_and_prelaunch(self):
        strict = self.approve_strict(self.v1)
        strict_bytes = Path(strict["registry"]).read_bytes()
        self.v1.rename(self.root / "old-native")
        self.v1.symlink_to(self.v2)
        self.launcher = self.v1
        # Reproduce a record created by the earlier candidate without this guard.
        with mock.patch.object(
            product_approvals, "has_approval_history", return_value=False
        ):
            installation = self.approve_installation()
            target, _ = self.resolve()
        installation_bytes = Path(installation["registry"]).read_bytes()
        participant = SimpleNamespace(
            vendor="claude-code",
            common_name="unchanged-strict",
            approved_product_executable=str(self.v1),
        )
        cam1_transport._require_approved_product_executable(participant, target)
        with self.assertRaises(cam1_transport.TransportError) as error:
            cam1_transport._require_product_metadata(target, label="claude")
        self.assertEqual(error.exception.code, "installation.strict_path_conflict")
        product_approvals.begin_operation()
        with self.assertRaises(cam1_transport.TransportError) as error:
            cam1_transport._approved_binary(str(self.launcher), label="claude")
        self.assertEqual(error.exception.code, "installation.strict_path_conflict")
        self.assertEqual(participant.approved_product_executable, str(self.v1))
        self.assertEqual(Path(strict["registry"]).read_bytes(), strict_bytes)
        self.assertEqual(
            Path(installation["registry"]).read_bytes(), installation_bytes
        )

    def test_corrupt_strict_history_is_not_ignored_by_installation_selection(self):
        strict = self.approve_strict(self.v1)
        self.approve_installation()
        registry = Path(strict["registry"])
        damaged = registry.read_bytes() + b'{"incomplete":'
        registry.write_bytes(damaged)
        product_approvals.begin_operation()
        with self.assertRaises(product_approvals.ProductApprovalError) as error:
            self.resolve()
        self.assertEqual(error.exception.code, "product_approval.recovery_required")
        self.assertEqual(registry.read_bytes(), damaged)

    def test_non_normalized_installation_selection_cannot_fall_back_to_strict(self):
        self.approve_strict(self.v1)
        approved = self.approve_installation()["record"]
        alias = str(
            self.launcher.parent / ".." / self.launcher.parent.name / self.launcher.name
        )
        for revoked in (False, True):
            with self.subTest(revoked=revoked):
                if revoked:
                    installations.revoke(
                        vendor="claude-code",
                        launcher=str(self.launcher),
                        approval_record_id=approved["record_id"],
                        expected_policy_sha256=approved["attributes"]["policy_sha256"],
                        operator_reference="direct synthetic revocation",
                    )
                product_approvals.begin_operation()
                with self.assertRaises(product_approvals.ProductApprovalError) as error:
                    self.resolve(alias)
                self.assertEqual(error.exception.code, "installation.path")

    def test_missing_installation_history_stops_project_use_but_allows_strict_probe(
        self,
    ):
        strict = self.approve_strict(self.v1)
        approved = self.approve_installation()
        target, _ = self.resolve()
        registry = Path(approved["registry"])
        before = registry.read_bytes()
        retained = registry.with_suffix(".retained")
        registry.rename(retained)
        with self.assertRaises(product_approvals.ProductApprovalError) as error:
            product_approvals.require_approved_metadata(
                vendor="claude-code", product_bin=target
            )
        self.assertEqual(error.exception.code, "installation.approval_changed")
        product_approvals.begin_operation()
        target, evidence = cam1_transport._approved_binary(
            str(self.launcher), label="claude"
        )
        self.assertEqual(evidence["record_id"], strict["approval"]["record_id"])
        cam1_transport._require_product_metadata(target, label="claude")
        participant = SimpleNamespace(
            vendor="claude-code",
            common_name="synthetic",
            approved_product_executable=str(self.launcher),
        )
        with self.assertRaises(cam1_transport.TransportError) as error:
            cam1_transport._require_approved_product_executable(participant, target)
        self.assertEqual(error.exception.code, "roster.product_executable_mismatch")
        self.assertEqual(retained.read_bytes(), before)
        self.assertFalse(registry.exists())

    def test_only_one_full_hash_per_operation(self):
        self.approve_installation()
        with mock.patch.object(
            installations, "discover_candidate", wraps=installations.discover_candidate
        ) as fingerprint:
            target, _ = self.resolve()
            self.resolve(target)
            product_approvals.require_approved_metadata(
                vendor="claude-code", product_bin=target
            )
        self.assertEqual(fingerprint.call_count, 1)

    def test_discovery_keeps_stable_path_after_update(self):
        self.approve_installation()
        self.update()
        with mock.patch(
            "subprocess.run", side_effect=AssertionError("no product execution")
        ):
            card = cam1_transport.discover_product_executable(
                vendor="claude-code", product_bin=str(self.launcher)
            )
        self.assertEqual(card["status"], "installation_approved")
        self.assertEqual(card["selection_path"], str(self.launcher))
        self.assertEqual(card["candidate"]["canonical_path"], str(self.v2))
        self.assertNotIn("approval_command", card)

    def test_doctor_copy_paste_flags_keep_both_stable_selections(self):
        self.approve_installation()
        codex_launcher = self.bin_dir.resolve() / "stable-codex"
        codex_launcher.symlink_to(self.v2)
        card = installations.discover(
            vendor="codex",
            launcher=str(codex_launcher),
            installation_root=str(self.root),
        )
        installations.approve(
            vendor="codex",
            launcher=str(codex_launcher),
            installation_root=str(self.root),
            expected_card_sha256=card["card_sha256"],
            operator_reference="direct synthetic codex installation",
        )
        with (
            mock.patch.object(
                cam1_transport, "_run_probe_before", return_value={"ok": True}
            ),
            mock.patch.object(
                cam1_transport, "_agent_view_probe_before", return_value={"ok": True}
            ),
            mock.patch.object(
                cam1_transport, "_mcp_sdk_check", return_value=(True, "2.1.1")
            ),
        ):
            report = cam1_transport.doctor(
                claude_bin=str(self.launcher),
                codex_bin=str(codex_launcher),
                timeout_seconds=20,
            )
        self.assertTrue(report["ok"])
        self.assertEqual(
            report["live_path_configuration"]["required_global_arguments"],
            ["--claude-bin", str(self.launcher), "--codex-bin", str(codex_launcher)],
        )
        self.assertEqual(
            report["checks"]["claude"]["approval"]["basis"],
            "operator_selected_installation",
        )

    def test_onboarding_keeps_stable_path_while_discovery_uses_native_target(self):
        self.approve_installation()
        binding = SimpleNamespace(
            project_id="00000000-0000-4000-8000-000000000001",
            display_name="synthetic",
            git_top_level=self.home,
            git_common_dir=self.home / ".git",
            worktree_id="00000000-0000-4000-8000-000000000002",
            git_bin="/usr/bin/git",
        )
        context = SimpleNamespace(
            top_level=self.home, common_dir=binding.git_common_dir
        )
        session = SimpleNamespace(
            product_name="synthetic claude", kind="interactive", cwd=str(self.home)
        )
        with (
            mock.patch.object(
                onboarding,
                "require_trusted_source",
                return_value=SimpleNamespace(validation_profile_sha256="a" * 64),
            ),
            mock.patch.object(
                onboarding, "_claude_agent_view", return_value=session
            ) as discovery,
            mock.patch.object(
                onboarding.project, "discover_git_context", return_value=context
            ),
        ):
            inspection = onboarding.inspect_self(
                binding,
                vendor="claude-code",
                product_bin=str(self.launcher),
                session_id="00000000-0000-4000-8000-000000000003",
                environment={},
            )
        discovery.assert_called_once_with(
            str(self.v1), "00000000-0000-4000-8000-000000000003"
        )
        self.assertEqual(inspection.product_executable, str(self.launcher))

    def test_placeholder_approval_reference_and_path_lookup_refused(self):
        card = self.card()
        with self.assertRaises(product_approvals.ProductApprovalError):
            installations.approve(
                vendor="claude-code",
                launcher=str(self.launcher),
                installation_root=str(self.root),
                expected_card_sha256=card["card_sha256"],
                operator_reference="DIRECT_OPERATOR_REFERENCE",
            )
        with self.assertRaises(product_approvals.ProductApprovalError):
            installations.discover(
                vendor="claude-code",
                launcher="claude",
                installation_root=str(self.root),
            )
        self.assertEqual(installations.status()["active"], [])

    def test_uncertain_append_retains_bytes_and_installation_reconciliation(self):
        card = self.card()
        original = product_approvals._write_all

        def append_then_fail(fd, raw):
            original(fd, raw)
            raise OSError("synthetic append uncertainty")

        with mock.patch.object(
            product_approvals, "_write_all", side_effect=append_then_fail
        ):
            with self.assertRaises(product_approvals.ProductApprovalError) as error:
                installations.approve(
                    vendor="claude-code",
                    launcher=str(self.launcher),
                    installation_root=str(self.root),
                    expected_card_sha256=card["card_sha256"],
                    operator_reference="synthetic failure test",
                )
        self.assertEqual(error.exception.audit["mutation_state"], "unknown")
        self.assertEqual(
            error.exception.audit["reconciliation_arguments"],
            ["product-installation-status"],
        )
        self.assertEqual(len(installations.status()["active"]), 1)

    def test_partial_tail_not_repaired_or_hidden(self):
        result = self.approve_installation()
        registry = Path(result["registry"])
        before = registry.read_bytes() + b'{"partial":'
        registry.write_bytes(before)
        with self.assertRaises(product_approvals.ProductApprovalError) as error:
            installations.status()
        self.assertEqual(error.exception.code, "installation.partial_tail")
        self.assertEqual(registry.read_bytes(), before)

    def test_schema_rejects_wrong_event_attributes(self):
        record = self.approve_installation()["record"]
        record["event_type"] = installations.REVOKED
        unsigned = dict(record)
        unsigned.pop("record_sha256")
        record["record_sha256"] = product_approvals._digest(unsigned)
        with self.assertRaises(product_approvals.ProductApprovalError):
            installations._parse_record(
                product_approvals._canonical_json(record) + b"\n"
            )

    def test_cli_uses_standard_source_gate_and_json_errors(self):
        emitted = []
        with (
            mock.patch.object(
                cam1_transport,
                "_require_live_validation_profile",
                return_value=({}, False),
            ) as gate,
            mock.patch.object(
                cam1_transport,
                "_emit",
                side_effect=lambda payload, **kw: emitted.append(payload),
            ),
            mock.patch.object(
                cam1_transport,
                "_with_validation_profile",
                side_effect=lambda payload: payload,
            ),
        ):
            code = cam1_transport.main(
                [
                    "product-installation-discover",
                    "--vendor",
                    "claude-code",
                    "--launcher",
                    str(self.launcher),
                    "--installation-root",
                    str(self.root),
                ]
            )
            self.assertEqual(code, 0)
            gate.assert_called_once()
            card = emitted[-1]
            approval_args = card["approval_command"][2:]
            approval_args[-1] = "direct synthetic CLI approval"
            self.assertEqual(cam1_transport.main(approval_args), 0)
            self.assertEqual(cam1_transport.main(["product-installation-status"]), 0)
            self.assertEqual(emitted[-1]["record_count"], 1)
        # All artifacts stayed under this test's account, never the real ledger.
        self.assertTrue(
            Path(emitted[-1]["registry"]).resolve().is_relative_to(self.home.resolve())
        )
        self.assertEqual(os.stat(emitted[-1]["registry"]).st_mode & 0o777, 0o600)
        json.dumps(emitted[-1])
