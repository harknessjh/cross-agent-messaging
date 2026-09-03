# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import concurrent.futures
import datetime as dt
import json
import os
import stat
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from tools import cam1_project
from tools.cam1lib import journal, onboarding, profile, project, state
from tools.cam1lib.enrollment import EnrollmentStatus
from tools.cam1lib.protocol import CamUsageError
from tools.cam1lib.state_store import ParticipantAlreadyEnrolled

CLI_TEST_HARNESS = Path(__file__).resolve().with_name("_cam1_cli_test_harness.py")

if __package__:
    from .test_cam1_project import (
        CLAUDE_SESSION,
        CODEX_SESSION,
        NOW,
        ProjectTestCase,
    )
else:
    from test_cam1_project import (
        CLAUDE_SESSION,
        CODEX_SESSION,
        NOW,
        ProjectTestCase,
    )


def _timestamp(value: dt.datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


class EnrollmentStateTests(ProjectTestCase):
    def execution_context(self, binding: project.ProjectBinding) -> dict[str, str]:
        return {
            "cam_checkout": str(Path(__file__).resolve().parents[1]),
            "validation_profile_sha256": "a" * 64,
            "project_root": str(binding.git_top_level),
            "product_executable": "/example/bin/codex",
            "product_executable_source": "explicit_candidate",
        }

    def propose_codex(
        self, binding: project.ProjectBinding
    ) -> tuple[state.StateStore, object]:
        store = state.StateStore(binding)
        proposal, reused = store.participant_enrollment_propose(
            common_name="coordinator",
            display_name="Primary coordinator",
            role=None,
            vendor="codex",
            session_id=CODEX_SESSION,
            session_label=None,
            session_kind=None,
            session_git_top_level=str(binding.git_top_level),
            session_git_common_dir=str(binding.git_common_dir),
            discovery_source="CODEX_THREAD_ID",
            execution_context=self.execution_context(binding),
            now=NOW,
        )
        self.assertFalse(reused)
        return store, proposal

    def test_proposal_is_non_routable_until_atomic_confirmation(self) -> None:
        binding = self.initialize()
        store, proposal = self.propose_codex(binding)

        pending = store.snapshot()
        self.assertEqual(len(pending.roster.participants), 0)
        self.assertEqual(
            pending.enrollment.select(proposal.proposal_id).status,
            EnrollmentStatus.PENDING,
        )
        with self.assertRaises(CamUsageError) as pending_context:
            pending.roster.require_correlated_route("coordinator")
        self.assertEqual(pending_context.exception.code, "roster.participant_unknown")

        participant, reused = store.participant_enrollment_confirm(
            proposal.proposal_id,
            expected_proposal_sha256=proposal.proposal_sha256,
            operator_reference="direct operator confirmation in this session",
            confirmed_at=_timestamp(NOW + dt.timedelta(seconds=1)),
            now=NOW + dt.timedelta(seconds=1),
        )
        self.assertFalse(reused)
        self.assertEqual(participant.status.value, "bound")
        self.assertEqual(participant.role, None)
        self.assertEqual(participant.metadata_revision, 1)
        self.assertEqual(participant.approved_product_executable, "/example/bin/codex")
        self.assertEqual(participant.route.status.value, "operator_correlated")

        repeated, reused = store.participant_enrollment_confirm(
            proposal.proposal_id,
            expected_proposal_sha256=proposal.proposal_sha256,
            operator_reference="same direct confirmation repeated",
            confirmed_at=_timestamp(NOW + dt.timedelta(seconds=2)),
            now=NOW + dt.timedelta(seconds=2),
        )
        self.assertTrue(reused)
        self.assertEqual(repeated.participant_id, participant.participant_id)
        self.assertEqual(
            [record["event_type"] for record in journal.replay_records(binding)],
            [
                state.PARTICIPANT_ENROLLMENT_PROPOSED,
                state.PARTICIPANT_ENROLLMENT_CONFIRMED,
            ],
        )
        rebuilt = store.rebuild()
        self.assertEqual(
            rebuilt.enrollment.select(proposal.proposal_id).status,
            EnrollmentStatus.CONFIRMED,
        )

    def test_changed_pending_proposal_supersedes_without_binding(self) -> None:
        binding = self.initialize()
        store, first = self.propose_codex(binding)
        second, reused = store.participant_enrollment_propose(
            common_name="coordinator",
            display_name="Renamed coordinator",
            role="coordination",
            vendor="codex",
            session_id=CODEX_SESSION,
            session_label=None,
            session_kind=None,
            session_git_top_level=str(binding.git_top_level),
            session_git_common_dir=str(binding.git_common_dir),
            discovery_source="CODEX_THREAD_ID",
            execution_context=self.execution_context(binding),
            now=NOW + dt.timedelta(seconds=1),
        )
        self.assertFalse(reused)
        snapshot = store.snapshot()
        self.assertEqual(
            snapshot.enrollment.select(first.proposal_id).status,
            EnrollmentStatus.SUPERSEDED,
        )
        self.assertEqual(
            snapshot.enrollment.select(second.proposal_id).status,
            EnrollmentStatus.PENDING,
        )
        self.assertEqual(len(snapshot.roster.participants), 0)
        before = len(journal.replay_records(binding))
        with self.assertRaises(CamUsageError) as superseded_context:
            store.participant_enrollment_confirm(
                first.proposal_id,
                expected_proposal_sha256=first.proposal_sha256,
                operator_reference="late confirmation",
                confirmed_at=_timestamp(NOW + dt.timedelta(seconds=2)),
            )
        self.assertEqual(
            superseded_context.exception.code, "onboarding.proposal_superseded"
        )
        self.assertEqual(len(journal.replay_records(binding)), before)

    def test_pending_proposal_does_not_reserve_common_name(self) -> None:
        binding = self.initialize()
        store, first = self.propose_codex(binding)
        second_session = "00000000-0000-4000-8000-000000000103"
        second, reused = store.participant_enrollment_propose(
            common_name="coordinator",
            display_name="Another coordinator",
            role=None,
            vendor="codex",
            session_id=second_session,
            session_label=None,
            session_kind=None,
            session_git_top_level=str(binding.git_top_level),
            session_git_common_dir=str(binding.git_common_dir),
            discovery_source="explicit_session_id",
            execution_context=self.execution_context(binding),
            now=NOW + dt.timedelta(seconds=1),
        )
        self.assertFalse(reused)
        self.assertEqual(len(store.snapshot().roster.participants), 0)

        store.participant_enrollment_confirm(
            first.proposal_id,
            expected_proposal_sha256=first.proposal_sha256,
            operator_reference="first direct confirmation",
            confirmed_at=_timestamp(NOW + dt.timedelta(seconds=2)),
            now=NOW + dt.timedelta(seconds=2),
        )
        records_before_conflict = len(journal.replay_records(binding))
        with self.assertRaises(CamUsageError) as conflict:
            store.participant_enrollment_confirm(
                second.proposal_id,
                expected_proposal_sha256=second.proposal_sha256,
                operator_reference="second direct confirmation",
                confirmed_at=_timestamp(NOW + dt.timedelta(seconds=3)),
                now=NOW + dt.timedelta(seconds=3),
            )
        self.assertEqual(conflict.exception.code, "roster.name_conflict")
        self.assertEqual(len(journal.replay_records(binding)), records_before_conflict)
        self.assertEqual(
            store.snapshot().enrollment.select(second.proposal_id).status,
            EnrollmentStatus.PENDING,
        )

        replacement, reused = store.participant_enrollment_propose(
            common_name="second-coordinator",
            display_name="Another coordinator",
            role=None,
            vendor="codex",
            session_id=second_session,
            session_label=None,
            session_kind=None,
            session_git_top_level=str(binding.git_top_level),
            session_git_common_dir=str(binding.git_common_dir),
            discovery_source="explicit_session_id",
            execution_context=self.execution_context(binding),
            now=NOW + dt.timedelta(seconds=4),
        )
        self.assertFalse(reused)
        enrolled, reused = store.participant_enrollment_confirm(
            replacement.proposal_id,
            expected_proposal_sha256=replacement.proposal_sha256,
            operator_reference="fresh direct confirmation",
            confirmed_at=_timestamp(NOW + dt.timedelta(seconds=5)),
            now=NOW + dt.timedelta(seconds=5),
        )
        self.assertFalse(reused)
        self.assertEqual(enrolled.common_name, "second-coordinator")

    def test_confirmation_cannot_predate_proposal(self) -> None:
        binding = self.initialize()
        store, proposal = self.propose_codex(binding)
        records_before = len(journal.replay_records(binding))

        with self.assertRaises(CamUsageError) as chronology:
            store.participant_enrollment_confirm(
                proposal.proposal_id,
                expected_proposal_sha256=proposal.proposal_sha256,
                operator_reference="synthetic predated confirmation",
                confirmed_at=_timestamp(NOW - dt.timedelta(seconds=1)),
                now=NOW + dt.timedelta(seconds=1),
            )

        self.assertEqual(
            chronology.exception.code, "onboarding.confirmation_chronology"
        )
        self.assertEqual(len(journal.replay_records(binding)), records_before)
        self.assertEqual(len(store.snapshot().roster.participants), 0)

    def test_claude_proposal_requires_discovered_label_and_kind(self) -> None:
        binding = self.initialize()
        store = state.StateStore(binding)

        with self.assertRaises(CamUsageError) as missing_label:
            store.participant_enrollment_propose(
                common_name="claude-reviewer",
                display_name="Claude reviewer",
                role=None,
                vendor="claude-code",
                session_id=CLAUDE_SESSION,
                session_label=None,
                session_kind="interactive",
                session_git_top_level=str(binding.git_top_level),
                session_git_common_dir=str(binding.git_common_dir),
                discovery_source="CLAUDE_CODE_SESSION_ID+claude_agent_view",
                execution_context={
                    **self.execution_context(binding),
                    "product_executable": "/example/bin/claude",
                },
                now=NOW,
            )

        self.assertEqual(
            missing_label.exception.code, "onboarding.session_label_required"
        )
        with self.assertRaises(CamUsageError) as missing_kind:
            store.participant_enrollment_propose(
                common_name="claude-reviewer",
                display_name="Claude reviewer",
                role=None,
                vendor="claude-code",
                session_id=CLAUDE_SESSION,
                session_label="claude-reviewer",
                session_kind=None,
                session_git_top_level=str(binding.git_top_level),
                session_git_common_dir=str(binding.git_common_dir),
                discovery_source="CLAUDE_CODE_SESSION_ID+claude_agent_view",
                execution_context={
                    **self.execution_context(binding),
                    "product_executable": "/example/bin/claude",
                },
                now=NOW,
            )
        self.assertEqual(
            missing_kind.exception.code, "onboarding.session_kind_required"
        )
        self.assertEqual(journal.replay_records(binding), ())

    def test_identical_pending_proposal_is_reused_without_append(self) -> None:
        binding = self.initialize()
        store, first = self.propose_codex(binding)
        repeated, reused = store.participant_enrollment_propose(
            common_name="coordinator",
            display_name="Primary coordinator",
            role=None,
            vendor="codex",
            session_id=CODEX_SESSION.upper(),
            session_label=None,
            session_kind=None,
            session_git_top_level=str(binding.git_top_level),
            session_git_common_dir=str(binding.git_common_dir),
            discovery_source="CODEX_THREAD_ID",
            execution_context=self.execution_context(binding),
            now=NOW + dt.timedelta(seconds=1),
        )

        self.assertTrue(reused)
        self.assertEqual(repeated.proposal_id, first.proposal_id)
        self.assertEqual(len(journal.replay_records(binding)), 1)

    def test_concurrent_identical_proposals_append_once(self) -> None:
        binding = self.initialize()

        def propose() -> tuple[str, bool]:
            candidate, reused = state.StateStore(
                project.resolve_project(self.repo, state_root=self.state_root)
            ).participant_enrollment_propose(
                common_name="coordinator",
                display_name="Primary coordinator",
                role=None,
                vendor="codex",
                session_id=CODEX_SESSION,
                session_label=None,
                session_kind=None,
                session_git_top_level=str(binding.git_top_level),
                session_git_common_dir=str(binding.git_common_dir),
                discovery_source="CODEX_THREAD_ID",
                execution_context=self.execution_context(binding),
                now=NOW,
            )
            return candidate.proposal_id, reused

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: propose(), range(2)))

        self.assertEqual(len({proposal_id for proposal_id, _ in results}), 1)
        self.assertEqual(sorted(reused for _, reused in results), [False, True])
        self.assertEqual(len(journal.replay_records(binding)), 1)

    def test_linked_worktrees_share_enrollment_and_confirmation(self) -> None:
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "CAM test")
        self.git("commit", "--quiet", "--allow-empty", "-m", "initial")
        primary = self.initialize()
        linked_path = self.base / "linked"
        self.git("worktree", "add", "--quiet", "-b", "linked", str(linked_path))
        linked = project.initialize_project(
            linked_path,
            state_root=self.state_root,
            now=NOW,
        )
        primary_store, proposal = self.propose_codex(primary)

        linked_store = state.StateStore(linked)
        self.assertEqual(
            linked_store.snapshot().enrollment.select(proposal.proposal_id).proposal_id,
            proposal.proposal_id,
        )
        participant, reused = linked_store.participant_enrollment_confirm(
            proposal.proposal_id,
            expected_proposal_sha256=proposal.proposal_sha256,
            operator_reference="direct confirmation in linked worktree",
            confirmed_at=_timestamp(NOW + dt.timedelta(seconds=1)),
            now=NOW + dt.timedelta(seconds=1),
        )

        self.assertFalse(reused)
        self.assertEqual(
            primary_store.snapshot().roster.select(participant.participant_id).binding,
            participant.binding,
        )

    def test_metadata_update_is_audited_and_preserves_identity_and_route(self) -> None:
        binding = self.initialize()
        store, proposal = self.propose_codex(binding)
        participant, _ = store.participant_enrollment_confirm(
            proposal.proposal_id,
            expected_proposal_sha256=proposal.proposal_sha256,
            operator_reference="direct confirmation",
            confirmed_at=_timestamp(NOW + dt.timedelta(seconds=1)),
            now=NOW + dt.timedelta(seconds=1),
        )
        original_binding = participant.binding
        original_route = participant.route
        updated = store.participant_update_metadata(
            participant.participant_id,
            display_name="Coordination lead",
            role="review",
            approved_product_executable="/new/bin/codex",
            expected_revision=1,
            operator_reference="operator approved descriptive metadata",
            updated_at=_timestamp(NOW + dt.timedelta(seconds=2)),
            now=NOW + dt.timedelta(seconds=2),
        )
        self.assertEqual(updated.metadata_revision, 2)
        self.assertEqual(updated.binding, original_binding)
        self.assertEqual(updated.route, original_route)
        self.assertEqual(updated.role, "review")
        records_before_retry = len(journal.replay_records(binding))
        retried = store.participant_update_metadata(
            participant.participant_id,
            display_name="Coordination lead",
            role="review",
            approved_product_executable="/new/bin/codex",
            expected_revision=1,
            operator_reference="repeated update",
            updated_at=_timestamp(NOW + dt.timedelta(seconds=3)),
        )
        self.assertEqual(retried.metadata_revision, 2)
        self.assertEqual(len(journal.replay_records(binding)), records_before_retry)

    def test_metadata_update_requires_valid_timestamp(self) -> None:
        binding = self.initialize()
        store, proposal = self.propose_codex(binding)
        participant, _ = store.participant_enrollment_confirm(
            proposal.proposal_id,
            expected_proposal_sha256=proposal.proposal_sha256,
            operator_reference="direct confirmation",
            confirmed_at=_timestamp(NOW + dt.timedelta(seconds=1)),
            now=NOW + dt.timedelta(seconds=1),
        )
        records_before = len(journal.replay_records(binding))

        with self.assertRaises(CamUsageError) as timestamp:
            store.participant_update_metadata(
                participant.participant_id,
                display_name="Changed display name",
                role=None,
                approved_product_executable="/example/bin/codex",
                expected_revision=1,
                operator_reference="synthetic malformed timestamp",
                updated_at="not-a-timestamp",
                now=NOW + dt.timedelta(seconds=2),
            )

        self.assertEqual(timestamp.exception.code, "state.timestamp")
        self.assertEqual(len(journal.replay_records(binding)), records_before)
        self.assertEqual(
            store.snapshot().roster.select(participant.participant_id).display_name,
            "Primary coordinator",
        )

    def test_already_enrolled_result_carries_locked_participant(self) -> None:
        binding = self.initialize()
        store, proposal = self.propose_codex(binding)
        participant, _ = store.participant_enrollment_confirm(
            proposal.proposal_id,
            expected_proposal_sha256=proposal.proposal_sha256,
            operator_reference="direct confirmation",
            confirmed_at=_timestamp(NOW + dt.timedelta(seconds=1)),
            now=NOW + dt.timedelta(seconds=1),
        )
        records_before = len(journal.replay_records(binding))

        with self.assertRaises(ParticipantAlreadyEnrolled) as enrolled:
            store.participant_enrollment_propose(
                common_name="ignored",
                display_name="Ignored",
                role=None,
                vendor="codex",
                session_id=CODEX_SESSION,
                session_label=None,
                session_kind=None,
                session_git_top_level=str(binding.git_top_level),
                session_git_common_dir=str(binding.git_common_dir),
                discovery_source="CODEX_THREAD_ID",
                execution_context=self.execution_context(binding),
                now=NOW + dt.timedelta(seconds=2),
            )

        self.assertEqual(enrolled.exception.participant, participant)
        self.assertEqual(len(journal.replay_records(binding)), records_before)


class OnboardingInspectionGuardTests(ProjectTestCase):
    def test_project_root_defaults_to_current_working_directory(self) -> None:
        args = cam1_project._parser().parse_args(
            ["onboarding", "prepare", "--vendor", "codex"]
        )
        self.assertEqual(args.project_root, ".")

    def test_cli_checks_cam_source_before_project_initialization(self) -> None:
        blocked = CamUsageError(
            "profile.path_set_mismatch", "synthetic untrusted CAM source"
        )

        with (
            mock.patch.object(
                cam1_project.product_approvals,
                "begin_operation",
            ) as begin_operation,
            mock.patch.object(
                cam1_project.onboarding,
                "require_trusted_source",
                side_effect=blocked,
            ),
            mock.patch.object(cam1_project.project, "initialize_project") as initialize,
            mock.patch.object(cam1_project, "_emit") as emit,
        ):
            return_code = cam1_project.main(
                [
                    "--project-root",
                    str(self.repo),
                    "onboarding",
                    "prepare",
                    "--vendor",
                    "codex",
                ]
            )

        self.assertEqual(return_code, 2)
        begin_operation.assert_called_once_with()
        initialize.assert_not_called()
        self.assertEqual(
            emit.call_args.args[0]["error"]["code"], "profile.path_set_mismatch"
        )

    def test_untrusted_cam_source_blocks_before_product_discovery(self) -> None:
        binding = self.initialize()
        blocked = profile.ValidationProfileError(
            "profile.path_set_mismatch", "synthetic untrusted CAM source"
        )

        with (
            mock.patch.object(profile, "require_live_profile", side_effect=blocked),
            mock.patch.object(onboarding, "_resolved_executable") as executable,
            self.assertRaises(CamUsageError) as rejected,
        ):
            onboarding.inspect_self(
                binding,
                vendor="codex",
                session_id=CODEX_SESSION,
                environment={},
            )

        self.assertEqual(rejected.exception.code, "profile.path_set_mismatch")
        executable.assert_not_called()


class OnboardingCliTests(ProjectTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.bin_dir = self.base / "bin"
        self.bin_dir.mkdir(mode=0o700)
        self.codex_bin = self.bin_dir / "codex"
        self.codex_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.codex_bin.chmod(0o700)

    def run_onboarding(
        self,
        *arguments: str,
        vendor: str,
        session_id: str,
        cwd: Path | None = None,
        explicit_project_root: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        variable = "CODEX_THREAD_ID" if vendor == "codex" else "CLAUDE_CODE_SESSION_ID"
        environment[variable] = session_id
        environment["PATH"] = f"{self.bin_dir}{os.pathsep}{environment['PATH']}"
        global_arguments = ["--state-root", str(self.state_root)]
        if explicit_project_root:
            global_arguments[:0] = ["--project-root", str(self.repo)]
        return subprocess.run(
            [
                sys.executable,
                str(CLI_TEST_HARNESS),
                "onboarding",
                *global_arguments,
                *arguments,
            ],
            cwd=cwd or self.repo,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )

    def worktree_files(self) -> dict[str, bytes]:
        return {
            str(path.relative_to(self.repo)): path.read_bytes()
            for path in self.repo.rglob("*")
            if path.is_file() and ".git" not in path.relative_to(self.repo).parts
        }

    def git_status(self) -> str:
        return self.git("status", "--porcelain=v2", "--untracked-files=all").stdout

    def has_head(self) -> bool:
        completed = subprocess.run(
            [
                project.DEFAULT_GIT_BIN,
                "-C",
                str(self.repo),
                "rev-parse",
                "--verify",
                "HEAD",
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return completed.returncode == 0

    def test_prepare_defaults_to_current_project_from_root_and_nested_cwd(
        self,
    ) -> None:
        status_before = self.git_status()
        files_before = self.worktree_files()
        common_arguments = (
            "onboarding",
            "prepare",
            "--vendor",
            "codex",
            "--common-name",
            "cam-codex",
            "--product-bin",
            str(self.codex_bin),
        )

        from_root = self.run_onboarding(
            *common_arguments,
            vendor="codex",
            session_id=CODEX_SESSION,
            explicit_project_root=False,
        )
        self.assertEqual(from_root.returncode, 0, from_root.stderr)
        root_card = json.loads(from_root.stdout)["identity_card"]
        self.assertEqual(root_card["project"]["project_root"], str(self.repo))

        nested = self.repo / "nested" / "directory"
        nested.mkdir(parents=True)
        from_nested = self.run_onboarding(
            *common_arguments,
            vendor="codex",
            session_id=CODEX_SESSION,
            cwd=nested,
            explicit_project_root=False,
        )
        self.assertEqual(from_nested.returncode, 0, from_nested.stderr)
        nested_card = json.loads(from_nested.stdout)["identity_card"]
        self.assertEqual(nested_card["project"]["project_root"], str(self.repo))
        self.assertEqual(nested_card["proposal_id"], root_card["proposal_id"])

        binding = project.resolve_project(self.repo, state_root=self.state_root)
        self.assertEqual(binding.git_top_level, self.repo)
        self.assertEqual(self.git_status(), status_before)
        self.assertEqual(self.worktree_files(), files_before)

    def test_codex_prepare_confirm_is_one_card_and_does_not_pollute_worktree(
        self,
    ) -> None:
        status_before = self.git_status()
        files_before = self.worktree_files()
        self.assertFalse(self.has_head())

        prepared = self.run_onboarding(
            "onboarding",
            "prepare",
            "--vendor",
            "codex",
            "--common-name",
            "cam-codex",
            "--display-name",
            "CAM Codex",
            "--product-bin",
            str(self.codex_bin),
            vendor="codex",
            session_id=CODEX_SESSION,
        )
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        payload = json.loads(prepared.stdout)
        card = payload["identity_card"]
        self.assertEqual(card["status"], "PENDING")
        self.assertEqual(card["participant"]["session_id"], CODEX_SESSION)
        self.assertIsNone(card["participant"]["role"])
        self.assertEqual(card["project"]["display_name"], "example project")
        self.assertIn(CODEX_SESSION, card["human_card"])
        self.assertIn(card["human_confirmation"]["exact_reply"], card["human_card"])
        serialized_card = json.dumps(card).lower()
        self.assertNotIn("uds:", serialized_card)
        self.assertNotIn("list_agents", serialized_card)
        self.assertNotIn('"pid"', serialized_card)
        binding = project.resolve_project(self.repo, state_root=self.state_root)
        self.assertEqual(
            len(state.StateStore(binding).snapshot().roster.participants), 0
        )

        rejected = self.run_onboarding(
            "onboarding",
            "confirm",
            "--proposal-id",
            card["proposal_id"],
            "--confirmation-code",
            "0" * 12,
            "--operator-reference",
            "direct confirmation in test session",
            vendor="codex",
            session_id=CODEX_SESSION,
        )
        self.assertEqual(rejected.returncode, 2)
        self.assertEqual(len(journal.replay_records(binding)), 1)

        confirmed = self.run_onboarding(
            "onboarding",
            "confirm",
            "--proposal-id",
            card["proposal_id"],
            "--confirmation-code",
            card["confirmation_code"],
            "--operator-reference",
            "direct confirmation in test session",
            vendor="codex",
            session_id=CODEX_SESSION,
        )
        self.assertEqual(confirmed.returncode, 0, confirmed.stderr)
        self.assertEqual(json.loads(confirmed.stdout)["status"], "enrolled")
        self.assertEqual(len(journal.replay_records(binding)), 2)
        repeated = self.run_onboarding(
            "onboarding",
            "confirm",
            "--proposal-id",
            card["proposal_id"],
            "--confirmation-code",
            card["confirmation_code"],
            "--operator-reference",
            "same confirmation repeated",
            vendor="codex",
            session_id=CODEX_SESSION,
        )
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assertEqual(json.loads(repeated.stdout)["status"], "already_confirmed")
        self.assertEqual(len(journal.replay_records(binding)), 2)

        wrong_session = self.run_onboarding(
            "onboarding",
            "confirm",
            "--proposal-id",
            card["proposal_id"],
            "--confirmation-code",
            card["confirmation_code"],
            "--operator-reference",
            "confirmation replayed from a different session",
            vendor="codex",
            session_id="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
        )
        self.assertEqual(wrong_session.returncode, 2)
        self.assertEqual(
            json.loads(wrong_session.stderr)["error"]["code"],
            "onboarding.session_mismatch",
        )
        self.assertEqual(len(journal.replay_records(binding)), 2)

        self.assertEqual(self.git_status(), status_before)
        self.assertEqual(self.worktree_files(), files_before)
        self.assertFalse(self.has_head())
        self.assertTrue((binding.git_common_dir / "cam1" / "project.json").is_file())
        self.assertFalse((self.repo / ".cam1").exists())

    def test_confirmation_rechecks_current_session_before_binding(self) -> None:
        prepared = self.run_onboarding(
            "onboarding",
            "prepare",
            "--vendor",
            "codex",
            "--product-bin",
            str(self.codex_bin),
            vendor="codex",
            session_id=CODEX_SESSION,
        )
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        card = json.loads(prepared.stdout)["identity_card"]
        binding = project.resolve_project(self.repo, state_root=self.state_root)

        rejected = self.run_onboarding(
            "onboarding",
            "confirm",
            "--proposal-id",
            card["proposal_id"],
            "--confirmation-code",
            card["confirmation_code"],
            "--operator-reference",
            "direct confirmation in the wrong session",
            vendor="codex",
            session_id="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
        )

        self.assertEqual(rejected.returncode, 2)
        self.assertEqual(
            json.loads(rejected.stderr)["error"]["code"],
            "onboarding.session_mismatch",
        )
        snapshot = state.StateStore(binding).snapshot()
        self.assertEqual(len(snapshot.roster.participants), 0)
        self.assertEqual(len(journal.replay_records(binding)), 1)

    def test_prepare_requires_an_account_approved_absolute_product_path(
        self,
    ) -> None:
        missing = self.run_onboarding(
            "onboarding",
            "prepare",
            "--vendor",
            "codex",
            vendor="codex",
            session_id=CODEX_SESSION,
        )
        self.assertEqual(missing.returncode, 2)
        self.assertEqual(
            json.loads(missing.stderr)["error"]["code"],
            "onboarding.product_bin_missing",
        )

        relative = self.run_onboarding(
            "onboarding",
            "prepare",
            "--vendor",
            "codex",
            "--product-bin",
            "codex",
            vendor="codex",
            session_id=CODEX_SESSION,
        )
        self.assertEqual(relative.returncode, 2)
        self.assertEqual(
            json.loads(relative.stderr)["error"]["code"],
            "onboarding.product_bin_absolute",
        )

    def test_claude_prepare_discovers_its_own_agent_view_identity(self) -> None:
        claude_bin = self.bin_dir / "claude"
        row = {
            "cwd": str(self.repo),
            "kind": "interactive",
            "name": "cam-claude-2",
            "pid": 4321,
            "sessionId": CLAUDE_SESSION,
            "startedAt": 123456789,
            "status": "busy",
        }
        claude_bin.write_text(
            "#!/bin/sh\nprintf '%s\\n' '"
            + json.dumps([row], separators=(",", ":"))
            + "'\n",
            encoding="utf-8",
        )
        claude_bin.chmod(stat.S_IRWXU)
        prepared = self.run_onboarding(
            "onboarding",
            "prepare",
            "--vendor",
            "claude-code",
            "--product-bin",
            str(claude_bin),
            vendor="claude-code",
            session_id=CLAUDE_SESSION,
        )
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        card = json.loads(prepared.stdout)["identity_card"]
        participant = card["participant"]
        self.assertEqual(participant["common_name"], "cam-claude-2")
        self.assertEqual(participant["display_name"], "cam-claude-2")
        self.assertEqual(participant["session_kind"], "interactive")
        self.assertEqual(participant["session_label"], "cam-claude-2")
        self.assertEqual(participant["session_id"], CLAUDE_SESSION)
        self.assertNotIn('"pid"', json.dumps(card))
        self.assertEqual(
            card["execution_context"]["product_executable_source"],
            "explicit_candidate",
        )

        confirmed = self.run_onboarding(
            "onboarding",
            "confirm",
            "--proposal-id",
            card["proposal_id"],
            "--confirmation-code",
            card["confirmation_code"],
            "--operator-reference",
            "direct confirmation in the Claude session",
            vendor="claude-code",
            session_id=CLAUDE_SESSION,
        )
        self.assertEqual(confirmed.returncode, 0, confirmed.stderr)
        enrolled = json.loads(confirmed.stdout)
        self.assertEqual(enrolled["status"], "enrolled")
        binding = project.resolve_project(self.repo, state_root=self.state_root)
        rostered = state.StateStore(binding).snapshot().roster.select("cam-claude-2")
        self.assertEqual(rostered.status.value, "bound")
        self.assertIsNone(rostered.route)


if __name__ == "__main__":
    unittest.main()
