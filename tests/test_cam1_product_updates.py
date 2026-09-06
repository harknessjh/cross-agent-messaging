# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import os
import shlex
from pathlib import Path
from typing import Any
from unittest import mock

from tests.test_cam1_project import NOW, ProjectTestCase
from tools import cam1_project, cam1_transport
from tools.cam1lib import participants, product_approvals, product_executables, state


class ProductUpdateTests(ProjectTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.binding = self.initialize()
        self.store = state.StateStore(self.binding)
        self.account = self.base / "account"
        self.account.mkdir(mode=0o700)
        self.marker = self.account / "executed"
        patch = mock.patch.object(
            product_approvals, "account_home", return_value=self.account
        )
        patch.start()
        self.addCleanup(patch.stop)
        self.old = self.executable("old product")
        self.new = self.executable("new product's binary")

    def executable(self, name: str) -> Path:
        path = self.account / name
        path.write_text(
            f"#!/bin/sh\ntouch {shlex.quote(str(self.marker))}\n", encoding="utf-8"
        )
        path.chmod(0o700)
        return path

    def enroll(
        self, vendor: str, *, path: Path | None = None
    ) -> participants.Participant:
        participant = self.store.participant_add(
            common_name=vendor,
            display_name=f"Test {vendor}",
            role="reviewer",
            vendor=vendor,
            approved_product_executable=str(path or self.old),
            now=NOW,
        )
        return self.store.participant_bind(
            participant.participant_id,
            session_id="00000000-0000-4000-8000-000000000201",
            session_label="test-session",
            session_kind="interactive",
            operator_reference="direct synthetic enrollment confirmation",
            bound_at=NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
            now=NOW,
        )

    def approve(self, vendor: str, path: Path) -> None:
        candidate = product_executables.discover_candidate(vendor, str(path))
        product_approvals.approve_candidate(
            vendor=vendor,
            product_bin=str(path),
            expected_fingerprint_sha256=candidate.fingerprint_sha256,
            operator_reference="direct synthetic executable approval",
        )

    def discover(
        self, vendor: str, *, selector: str | None = None, path: Path | None = None
    ) -> tuple[int, dict[str, Any]]:
        arguments = [
            "--project-root",
            str(self.repo),
            "--state-root",
            str(self.state_root),
            "--git-bin",
            self.binding.git_bin,
            "product-discover",
            "--vendor",
            vendor,
            "--participant",
            selector or vendor,
        ]
        if path is not None:
            arguments.extend(["--product-bin", str(path)])
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
            mock.patch.object(cam1_transport, "_emit") as emit,
        ):
            code = cam1_transport.main(arguments)
        return code, emit.call_args.args[0]

    def retained_bytes(self) -> dict[str, bytes]:
        roots = (self.binding.project_dir, self.account / "CAM")
        return {
            str(path): path.read_bytes()
            for root in roots
            for path in root.rglob("*")
            if path.is_file()
        }

    def apply_guidance(self, guidance: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        arguments = guidance["command"][2:]
        self.assertEqual(arguments[-1], "DIRECT_OPERATOR_REFERENCE")
        arguments[-1] = "direct synthetic operator approval of this roster path"
        with mock.patch.object(cam1_project, "_emit") as emit:
            code = cam1_project.main(arguments)
        return code, emit.call_args.args[0]

    def test_new_path_guidance_is_read_only_and_preserves_project_context(self) -> None:
        for vendor in ("codex", "claude-code"):
            with self.subTest(vendor=vendor):
                participant = self.enroll(vendor)
                self.approve(vendor, self.old)
                before = self.retained_bytes()
                code, card = self.discover(vendor, path=self.new)
                self.assertEqual(code, 0)
                self.assertEqual(card["status"], "approval_candidate")
                update = card["participant_update"]
                self.assertEqual(update["status"], "metadata_update_required")
                self.assertEqual(update["recorded_path"], str(self.old))
                self.assertEqual(update["candidate_path"], str(self.new))
                self.assertEqual(update["participant_id"], participant.participant_id)
                self.assertEqual(
                    update["metadata_revision"], participant.metadata_revision
                )
                argv = shlex.split(update["command_text"])
                self.assertEqual(argv, update["command"])
                for flag, value in (
                    ("--project-root", str(self.repo)),
                    ("--state-root", str(self.state_root)),
                    ("--git-bin", self.binding.git_bin),
                    ("--expected-revision", str(participant.metadata_revision)),
                    ("--participant", participant.participant_id),
                    ("--product-bin", str(self.new)),
                ):
                    self.assertEqual(argv[argv.index(flag) + 1], value)
                self.assertEqual(self.retained_bytes(), before)
                self.assertFalse(self.marker.exists())

    def test_path_discovery_resolves_updated_alias_even_if_old_version_removed(
        self,
    ) -> None:
        for vendor, command in (("codex", "codex"), ("claude-code", "claude")):
            with self.subTest(vendor=vendor):
                self.enroll(vendor)
                alias = self.account / command
                alias.symlink_to(self.new)
                self.old.unlink(missing_ok=True)
                with mock.patch.dict(os.environ, {"PATH": str(self.account)}):
                    code, card = self.discover(vendor)
                self.assertEqual(code, 0)
                self.assertEqual(card["candidate"]["canonical_path"], str(self.new))
                self.assertEqual(
                    card["participant_update"]["recorded_path"], str(self.old)
                )
                self.assertFalse(self.marker.exists())

    def test_approved_new_path_needs_only_reviewed_roster_update_not_reenrollment(
        self,
    ) -> None:
        for vendor in ("codex", "claude-code"):
            with self.subTest(vendor=vendor):
                original = self.enroll(vendor)
                self.approve(vendor, self.old)
                self.approve(vendor, self.new)
                code, card = self.discover(vendor, path=self.new)
                self.assertEqual((code, card["status"]), (0, "already_approved"))
                code, result = self.apply_guidance(card["participant_update"])
                self.assertEqual((code, result["status"]), (0, "metadata_updated"))
                current = self.store.snapshot().roster.select(vendor)
                self.assertEqual(current.binding, original.binding)
                self.assertEqual(current.participant_id, original.participant_id)
                self.assertEqual(current.display_name, original.display_name)
                self.assertEqual(current.role, original.role)
                self.assertEqual(
                    current.metadata_revision, original.metadata_revision + 1
                )
                self.assertEqual(current.approved_product_executable, str(self.new))
                self.assertEqual(
                    len(product_approvals.approval_status(vendor=vendor)["active"]), 2
                )
                code, card = self.discover(vendor, path=self.new)
                self.assertEqual(code, 0)
                self.assertEqual(
                    card["participant_update"]["status"], "roster_path_current"
                )
                self.assertNotIn("command", card["participant_update"])
                self.assertFalse(self.marker.exists())

    def test_stale_guidance_cannot_overwrite_concurrent_metadata_update(self) -> None:
        original = self.enroll("codex")
        self.approve("codex", self.new)
        _, card = self.discover("codex", path=self.new)
        self.store.participant_update_metadata(
            "codex",
            display_name="Revised display name",
            role=original.role,
            approved_product_executable=str(self.old),
            expected_revision=original.metadata_revision,
            operator_reference="direct synthetic descriptive update",
            updated_at=NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
            now=NOW,
        )
        before = self.retained_bytes()
        code, result = self.apply_guidance(card["participant_update"])
        self.assertEqual(code, 2)
        self.assertEqual(result["error"]["code"], "roster.metadata_conflict")
        self.assertEqual(self.retained_bytes(), before)

    def test_same_path_replacement_still_requires_guarded_account_approval(
        self,
    ) -> None:
        self.enroll("codex", path=self.new)
        self.approve("codex", self.new)
        self.new.write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")
        before = self.retained_bytes()
        code, card = self.discover("codex", path=self.new)
        self.assertEqual((code, card["status"]), (0, "replacement_approval_required"))
        self.assertEqual(card["participant_update"]["status"], "roster_path_current")
        self.assertNotIn("command", card["participant_update"])
        self.assertIn("revocation_command", card)
        self.assertEqual(self.retained_bytes(), before)
        with self.assertRaises(cam1_transport.TransportError) as error:
            cam1_transport.resolve_product_binary(str(self.new), vendor="codex")
        self.assertEqual(error.exception.code, "product_approval.drift")

    def test_unknown_vendor_mismatched_and_inactive_participants_fail_before_candidate_inspection(
        self,
    ) -> None:
        self.enroll("codex")
        for vendor, selector, expected in (
            ("codex", "unknown", "roster.participant_unknown"),
            ("claude-code", "codex", "roster.vendor_mismatch"),
        ):
            with (
                self.subTest(vendor=vendor, selector=selector),
                mock.patch.object(product_executables, "discover_candidate") as inspect,
            ):
                code, result = self.discover(vendor, selector=selector, path=self.new)
                self.assertEqual(code, 2)
                self.assertEqual(result["error"]["code"], expected)
                inspect.assert_not_called()
        self.store.participant_invalidate(
            "codex", reason="synthetic stale binding", now=NOW
        )
        with mock.patch.object(product_executables, "discover_candidate") as inspect:
            code, result = self.discover("codex", path=self.new)
            self.assertEqual(code, 2)
            self.assertEqual(result["error"]["code"], "roster.participant_stale")
            inspect.assert_not_called()

    def test_missing_recorded_version_reports_recovery_without_path_fallback(
        self,
    ) -> None:
        self.old.unlink()
        for vendor in ("codex", "claude-code"):
            with (
                self.subTest(vendor=vendor),
                mock.patch.object(product_executables.shutil, "which") as lookup,
                self.assertRaises(cam1_transport.TransportError) as error,
            ):
                cam1_transport.resolve_product_binary(str(self.old), vendor=vendor)
            self.assertEqual(error.exception.code, "product_approval.not_found")
            self.assertIn(str(self.old), error.exception.detail)
            self.assertIn(f"product-discover --vendor {vendor}", error.exception.detail)
            lookup.assert_not_called()

    def test_roster_mismatch_reports_both_paths_and_keeps_existing_error_code(
        self,
    ) -> None:
        original = self.enroll("claude-code")
        with self.assertRaises(cam1_transport.TransportError) as error:
            cam1_transport._require_approved_product_executable(original, str(self.new))
        self.assertEqual(error.exception.code, "roster.product_executable_mismatch")
        self.assertIn(repr(str(self.old)), error.exception.detail)
        self.assertIn(repr(str(self.new)), error.exception.detail)
        self.assertIn("--participant", error.exception.detail)

    def test_candidate_drift_after_discovery_is_not_approved_by_roster_guidance(
        self,
    ) -> None:
        self.enroll("codex")
        self.approve("codex", self.new)
        _, card = self.discover("codex", path=self.new)
        self.new.write_text("#!/bin/sh\nexit 4\n", encoding="utf-8")
        # Metadata associates a path, not bytes. Regardless of whether that
        # association is recorded, the existing account gate rejects drift.
        self.apply_guidance(card["participant_update"])
        with self.assertRaises(cam1_transport.TransportError) as error:
            cam1_transport.resolve_product_binary(str(self.new), vendor="codex")
        self.assertEqual(error.exception.code, "product_approval.drift")
        self.assertFalse(self.marker.exists())

    def test_participant_discovery_keeps_the_clean_source_gate(self) -> None:
        with (
            mock.patch.object(
                cam1_transport,
                "_require_live_validation_profile",
                side_effect=cam1_transport.TransportError(
                    "validator.dirty", "synthetic dirty checkout"
                ),
            ),
            mock.patch.object(cam1_transport, "_resolve_project") as resolve,
            mock.patch.object(product_executables, "discover_candidate") as inspect,
            mock.patch.object(
                cam1_transport,
                "_with_validation_profile",
                side_effect=lambda payload: payload,
            ),
            mock.patch.object(cam1_transport, "_emit") as emit,
        ):
            code = cam1_transport.main(
                [
                    "product-discover",
                    "--vendor",
                    "codex",
                    "--participant",
                    "codex",
                ]
            )
        self.assertEqual(code, 2)
        self.assertEqual(emit.call_args.args[0]["error"]["code"], "validator.dirty")
        resolve.assert_not_called()
        inspect.assert_not_called()
