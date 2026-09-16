# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import json
import unittest
from unittest import mock

from tests._native_executable_fixture import write_native
from tools import cam1_transport
from tools.cam1lib import (
    compatibility,
    onboarding,
    participants,
    product_approvals,
    product_executables,
    project,
)

if __package__:
    from ._product_approval_test_case import ProductApprovalTestCase
else:
    from _product_approval_test_case import ProductApprovalTestCase


class ProductApprovalTransportTests(ProductApprovalTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.codex = self.bin_dir / "codex"
        write_native(self.codex)
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

    def test_cli_reports_committed_approval_and_revocation_on_unlock_failure(
        self,
    ) -> None:
        candidate = product_executables.discover_candidate(
            "claude-code", str(self.executable), allow_path_lookup=False
        )
        original_write = product_approvals._write_all
        original_flock = product_approvals.fcntl.flock
        record = None
        for command in ("product-approve", "product-revoke"):
            with self.subTest(command=command):
                append_state = {"descriptor": None}

                def write(descriptor, raw, current=append_state):
                    current["descriptor"] = descriptor
                    original_write(descriptor, raw)

                def flock(descriptor, operation, current=append_state):
                    original_flock(descriptor, operation)
                    if (
                        descriptor == current["descriptor"]
                        and operation == product_approvals.fcntl.LOCK_UN
                    ):
                        raise OSError("synthetic CLI mutation cleanup failure")

                arguments = [
                    command,
                    "--vendor",
                    "claude-code",
                    "--product-bin",
                    str(self.executable),
                    "--expected-fingerprint-sha256",
                    candidate.fingerprint_sha256,
                    "--operator-reference",
                    "Direct synthetic operator confirmation",
                ]
                if command == "product-revoke":
                    self.assertIsNotNone(record)
                    arguments.extend(("--approval-record-id", record["record_id"]))
                with (
                    mock.patch.object(
                        product_approvals, "_write_all", side_effect=write
                    ),
                    mock.patch.object(
                        product_approvals.fcntl, "flock", side_effect=flock
                    ),
                ):
                    code, payload = self.invoke_product_cli(*arguments)
                self.assertEqual(code, 2, payload)
                self.assertEqual(
                    payload["error"]["code"], "product_approval.committed_uncertain"
                )
                self.assertEqual(payload["audit"]["mutation_state"], "committed")
                self.assertEqual(
                    payload["audit"]["reconciliation_arguments"], ["product-status"]
                )
                records = [
                    json.loads(line)
                    for line in product_approvals.registry_path()
                    .read_bytes()
                    .splitlines()
                ]
                record = records[-1]
                self.assertEqual(
                    record["record_id"], payload["audit"]["intended_record_id"]
                )
                self.assertEqual(product_approvals._VERIFIED_APPROVALS, {})
        self.assertEqual(product_approvals.approval_status()["active"], [])

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

        write_native(self.executable, "replacement 17")
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

    def test_legacy_roster_paths_need_direct_approval_for_both_vendors(self) -> None:
        for vendor, executable in (
            ("claude-code", self.executable),
            ("codex", self.codex),
        ):
            with self.subTest(vendor=vendor):
                candidate = product_executables.discover_candidate(
                    vendor, str(executable)
                )
                participant = mock.Mock(
                    vendor=vendor,
                    status=participants.ParticipantStatus.BOUND,
                    participant_id="00000000-0000-4000-8000-000000000201",
                    approved_product_executable=candidate.canonical_path,
                )
                participant.binding.generation = 3
                proposal = mock.Mock(
                    participant_id=participant.participant_id,
                    operator_reference="direct historical operator confirmation",
                    confirmed_at="2026-09-01T00:00:00Z",
                    proposal_id="00000000-0000-4000-8000-000000000401",
                )
                proposal.status.value = "confirmed"
                proposal.execution_context.product_executable = candidate.canonical_path
                proposal.execution_context.validation_profile_sha256 = (
                    "c1752841229245561fcbd34570749978d1229098f8b09861aae44305b666c06d"
                )
                store = mock.Mock()
                store.snapshot.return_value.roster.participants = {
                    participant.participant_id: participant
                }
                store.snapshot.return_value.enrollment.proposals = {
                    proposal.proposal_id: proposal
                }
                binding = mock.Mock(project_id="00000000-0000-4000-8000-000000000301")
                for replaced in (False, True):
                    if replaced:
                        write_native(executable, "replacement 7")
                        executable.chmod(0o700)
                    before = product_approvals.approval_status().get("record_count", 0)
                    with (
                        self.subTest(replaced=replaced),
                        mock.patch.object(
                            cam1_transport.state, "StateStore", return_value=store
                        ),
                        self.assertRaises(cam1_transport.TransportError) as error,
                    ):
                        cam1_transport.resolve_product_binary(
                            str(executable), vendor=vendor, binding=binding
                        )
                    self.assertEqual(error.exception.code, "product_approval.required")
                    self.assertIn("product-discover", error.exception.detail)
                    self.assertEqual(
                        product_approvals.approval_status().get("record_count", 0),
                        before,
                    )
                    self.assertFalse(self.marker.exists())

                candidate = product_executables.discover_candidate(
                    vendor, str(executable)
                )
                approval = product_approvals.approve_candidate(
                    vendor=vendor,
                    product_bin=str(executable),
                    expected_fingerprint_sha256=candidate.fingerprint_sha256,
                    operator_reference="direct approval of the newly reviewed bytes",
                )["approval"]
                with mock.patch.object(cam1_transport.state, "StateStore") as lookup:
                    self.assertEqual(
                        cam1_transport.resolve_product_binary(
                            str(executable), vendor=vendor, binding=mock.Mock()
                        ),
                        candidate.canonical_path,
                    )
                    lookup.assert_not_called()
                product_approvals.revoke_approval(
                    vendor=vendor,
                    product_bin=str(executable),
                    approval_record_id=approval["record_id"],
                    expected_fingerprint_sha256=candidate.fingerprint_sha256,
                    operator_reference="direct synthetic revocation",
                )
                with self.assertRaises(cam1_transport.TransportError):
                    cam1_transport.resolve_product_binary(
                        str(executable), vendor=vendor, binding=binding
                    )

    def test_historical_grandfathered_record_remains_readable_without_rewriting(
        self,
    ) -> None:
        candidate = product_executables.discover_candidate(
            "claude-code", str(self.executable)
        )
        product_approvals.approve_candidate(
            vendor="claude-code",
            product_bin=str(self.executable),
            expected_fingerprint_sha256=candidate.fingerprint_sha256,
            operator_reference="direct synthetic approval",
        )
        path = product_approvals.registry_path()
        record = json.loads(path.read_bytes())
        record["attributes"]["basis"] = "grandfathered_roster"
        record["attributes"]["migration"] = {
            "project_id": "00000000-0000-4000-8000-000000000301",
            "participant_id": "00000000-0000-4000-8000-000000000201",
            "binding_generation": 3,
            "source": "confirmed_enrollment",
            "source_reference": "00000000-0000-4000-8000-000000000401",
        }
        unsigned = {
            key: value for key, value in record.items() if key != "record_sha256"
        }
        record["record_sha256"] = product_approvals._digest(unsigned)
        raw = product_approvals._canonical_json(record) + b"\n"
        path.write_bytes(raw)
        path.chmod(0o600)
        product_approvals.begin_operation()  # Replay a historical fixture in a fresh operation.
        with mock.patch.object(cam1_transport.state, "StateStore") as lookup:
            resolved = cam1_transport.resolve_product_binary(
                str(self.executable), vendor="claude-code"
            )
            lookup.assert_not_called()
        self.assertEqual(resolved, record["attributes"]["canonical_path"])
        self.assertEqual(
            product_approvals.approval_status()["active"][0]["attributes"]["basis"],
            "grandfathered_roster",
        )
        self.assertEqual(path.read_bytes(), raw)

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
