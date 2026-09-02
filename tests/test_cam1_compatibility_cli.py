# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import datetime as dt
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from argparse import Namespace
from pathlib import Path
from unittest import mock

from tools.cam1lib import compatibility, compatibility_cli, journal, project, state

if __package__:
    from .test_cam1_project import (
        CODEX_PARTICIPANT,
        CODEX_SESSION,
        ProjectTestCase,
    )
else:
    from test_cam1_project import (
        CODEX_PARTICIPANT,
        CODEX_SESSION,
        ProjectTestCase,
    )

FUTURE = (
    (dt.datetime.now(dt.UTC).replace(microsecond=0) + dt.timedelta(days=1))
    .isoformat()
    .replace("+00:00", "Z")
)
SOURCE_ROOT = Path(__file__).resolve().parents[1]


class CompatibilityCliTests(ProjectTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tool_checkout_temporary = tempfile.TemporaryDirectory()
        cls.tool_checkout = (
            Path(cls.tool_checkout_temporary.name).resolve() / "clean-cam-checkout"
        )
        shutil.copytree(
            SOURCE_ROOT,
            cls.tool_checkout,
            ignore=shutil.ignore_patterns(
                ".git",
                ".venv",
                "__pycache__",
                "*.pyc",
            ),
        )
        subprocess.run(
            [project.DEFAULT_GIT_BIN, "-C", str(cls.tool_checkout), "init", "--quiet"],
            check=True,
        )
        subprocess.run(
            [
                project.DEFAULT_GIT_BIN,
                "-C",
                str(cls.tool_checkout),
                "add",
                ".",
            ],
            check=True,
        )
        subprocess.run(
            [
                project.DEFAULT_GIT_BIN,
                "-C",
                str(cls.tool_checkout),
                "-c",
                "user.name=CAM Test",
                "-c",
                "user.email=cam-test@example.invalid",
                "commit",
                "--quiet",
                "-m",
                "test fixture",
            ],
            check=True,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tool_checkout_temporary.cleanup()

    def tool_command(self, *arguments: str) -> list[str]:
        return [
            sys.executable,
            str(self.tool_checkout / "tools" / "cam1_project.py"),
            "--project-root",
            str(self.repo),
            "--state-root",
            str(self.state_root),
            *arguments,
        ]

    def run_source_tool(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(SOURCE_ROOT / "tools" / "cam1_project.py"),
                "--project-root",
                str(self.repo),
                "--state-root",
                str(self.state_root),
                *arguments,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )

    def enroll_codex(self) -> None:
        added = self.run_tool(
            "participant",
            "add",
            "--common-name",
            "reviewer",
            "--display-name",
            "Example Reviewer",
            "--role",
            "review",
            "--vendor",
            "codex",
            "--participant-id",
            CODEX_PARTICIPANT,
        )
        self.assertEqual(added.returncode, 0, added.stderr)
        bound = self.run_tool(
            "participant",
            "bind",
            "--participant",
            "reviewer",
            "--operator-reference",
            "operator confirmed participant compatibility",
            "--session-id",
            CODEX_SESSION,
            "--session-label",
            "example-reviewer",
            "--session-kind",
            "interactive",
            "--operator-reference",
            "operator matched the active local session",
        )
        self.assertEqual(bound.returncode, 0, bound.stderr)

    def plan(
        self,
        *,
        plan_id: str | None = None,
        feature_id: str = "compatibility.kernel",
        feature_version: int = 1,
    ) -> subprocess.CompletedProcess[str]:
        arguments = [
            "compatibility",
            "plan",
            "--feature-id",
            feature_id,
            "--feature-version",
            str(feature_version),
            "--expires-at",
            FUTURE,
            "--operator-reference",
            "operator approved the bounded compatibility plan",
        ]
        if plan_id is not None:
            arguments.extend(("--plan-id", plan_id))
        return self.run_tool(*arguments)

    def test_cli_plan_ready_and_concurrent_activation_are_atomic(self) -> None:
        self.initialize()
        self.enroll_codex()
        planned = self.plan()
        self.assertEqual(planned.returncode, 0, planned.stderr)
        plan = json.loads(planned.stdout)["plan"]
        self.assertEqual(
            plan["frozen_participants"],
            [
                {
                    "participant_id": CODEX_PARTICIPANT,
                    "binding_generation": 1,
                }
            ],
        )

        ready = self.run_tool(
            "compatibility",
            "ready",
            "--plan-id",
            plan["plan_id"],
            "--participant",
            "reviewer",
            "--operator-reference",
            "operator confirmed participant compatibility",
        )
        self.assertEqual(ready.returncode, 0, ready.stderr)
        readiness = json.loads(ready.stdout)["readiness"]
        self.assertEqual(readiness["participant_id"], CODEX_PARTICIPANT)
        self.assertEqual(
            readiness["capabilities"],
            sorted(compatibility.SUPPORTED_READER_CAPABILITIES),
        )
        self.assertEqual(len(readiness["validation_profile_sha256"]), 64)
        self.assertEqual(
            readiness["validation_profile_sha256"],
            plan["validation_profile_sha256"],
        )

        command = self.tool_command(
            "compatibility",
            "activate",
            "--plan-id",
            plan["plan_id"],
            "--operator-reference",
            "operator activated the fully staged gate",
        )
        first = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        second = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        first_stdout, first_stderr = first.communicate(timeout=30)
        second_stdout, second_stderr = second.communicate(timeout=30)
        self.assertEqual(first.returncode, 0, first_stderr or first_stdout)
        self.assertEqual(second.returncode, 0, second_stderr or second_stdout)
        statuses = {
            json.loads(first_stdout)["status"],
            json.loads(second_stdout)["status"],
        }
        self.assertEqual(statuses, {"activated", "already_active"})

        binding = project.resolve_project(self.repo, state_root=self.state_root)
        records = journal.replay_records(binding)
        self.assertEqual(
            sum(
                record["event_type"] == compatibility.COMPATIBILITY_GATE_ACTIVATED_EVENT
                for record in records
            ),
            1,
        )
        snapshot = state.StateStore(binding).snapshot()
        self.assertEqual(
            snapshot.compatibility.active_gate("compatibility.kernel").plan_id,
            plan["plan_id"],
        )
        status = self.run_tool("compatibility", "status")
        self.assertEqual(status.returncode, 0, status.stderr)
        status_payload = json.loads(status.stdout)
        self.assertEqual(status_payload["status"], "compatible")
        self.assertEqual(len(status_payload["compatibility"]["plans"]), 1)

    def test_future_feature_is_automatically_bound_to_its_capability(self) -> None:
        binding = self.initialize()
        self.enroll_codex()
        planned = self.plan(feature_id="causal.ordering")
        self.assertEqual(planned.returncode, 0, planned.stderr)
        plan = json.loads(planned.stdout)["plan"]
        self.assertEqual(
            plan["required_capabilities"],
            [
                "causal.ordering/1",
                compatibility.COMPATIBILITY_KERNEL_CAPABILITY,
            ],
        )
        before = journal.verify_journal(binding).record_count

        ready = self.run_tool(
            "compatibility",
            "ready",
            "--plan-id",
            plan["plan_id"],
            "--participant",
            "reviewer",
            "--operator-reference",
            "operator confirmed participant compatibility",
        )

        self.assertEqual(ready.returncode, 2)
        self.assertEqual(
            json.loads(ready.stderr)["error"]["code"],
            "compatibility.readiness_insufficient",
        )
        self.assertEqual(journal.verify_journal(binding).record_count, before)

    def test_activation_reports_committed_when_projection_refresh_fails(self) -> None:
        binding = self.initialize()
        planned = self.plan()
        self.assertEqual(planned.returncode, 0, planned.stderr)
        plan_id = json.loads(planned.stdout)["plan"]["plan_id"]
        projection_path = state.state_projection_path(binding)
        if projection_path.exists():
            projection_path.unlink()
        projection_path.mkdir(mode=0o700)

        activated = self.run_tool(
            "compatibility",
            "activate",
            "--plan-id",
            plan_id,
            "--operator-reference",
            "operator activated the empty-project gate",
        )

        self.assertEqual(activated.returncode, 0, activated.stderr)
        payload = json.loads(activated.stdout)
        self.assertEqual(payload["status"], "activated_projection_stale")
        self.assertFalse(payload["projection_current"])
        self.assertEqual(payload["warning"]["code"], "state.projection_refresh")
        self.assertRegex(payload["record"]["record_sha256"], r"^[0-9a-f]{64}$")
        records = journal.replay_records(binding)
        self.assertEqual(
            sum(
                record["event_type"] == compatibility.COMPATIBILITY_GATE_ACTIVATED_EVENT
                for record in records
            ),
            1,
        )

    def test_plan_and_readiness_retries_reuse_the_original_records(self) -> None:
        binding = self.initialize()
        self.enroll_codex()
        plan_id = str(uuid.uuid4())
        first_plan = self.plan(plan_id=plan_id)
        repeated_plan = self.plan(plan_id=plan_id)
        self.assertEqual(first_plan.returncode, 0, first_plan.stderr)
        self.assertEqual(repeated_plan.returncode, 0, repeated_plan.stderr)
        first_plan_payload = json.loads(first_plan.stdout)
        repeated_plan_payload = json.loads(repeated_plan.stdout)
        self.assertEqual(repeated_plan_payload["status"], "already_planned")
        self.assertEqual(repeated_plan_payload["record"], first_plan_payload["record"])
        after_plan = journal.verify_journal(binding).record_count

        conflict = self.plan(plan_id=plan_id, feature_version=2)
        self.assertEqual(conflict.returncode, 2)
        self.assertEqual(
            json.loads(conflict.stderr)["error"]["code"],
            "compatibility.plan_conflict",
        )
        self.assertEqual(journal.verify_journal(binding).record_count, after_plan)

        ready_arguments = (
            "compatibility",
            "ready",
            "--plan-id",
            plan_id,
            "--participant",
            "reviewer",
            "--operator-reference",
            "operator confirmed participant compatibility",
        )
        first_ready = self.run_tool(*ready_arguments)
        repeated_ready = self.run_tool(*ready_arguments)
        self.assertEqual(first_ready.returncode, 0, first_ready.stderr)
        self.assertEqual(repeated_ready.returncode, 0, repeated_ready.stderr)
        first_ready_payload = json.loads(first_ready.stdout)
        repeated_ready_payload = json.loads(repeated_ready.stdout)
        self.assertEqual(repeated_ready_payload["status"], "already_ready")
        self.assertEqual(
            repeated_ready_payload["record"], first_ready_payload["record"]
        )
        self.assertEqual(
            journal.verify_journal(binding).record_count,
            after_plan + 1,
        )

    def test_failures_append_nothing_and_manual_namespace_is_reserved(self) -> None:
        binding = self.initialize()
        added = self.run_tool(
            "participant",
            "add",
            "--common-name",
            "reviewer",
            "--display-name",
            "Example Reviewer",
            "--role",
            "review",
            "--vendor",
            "codex",
            "--participant-id",
            CODEX_PARTICIPANT,
        )
        self.assertEqual(added.returncode, 0, added.stderr)
        before = journal.verify_journal(binding).record_count

        unbound = self.plan()
        self.assertEqual(unbound.returncode, 2)
        self.assertEqual(
            json.loads(unbound.stderr)["error"]["code"],
            "compatibility.roster_unbound",
        )
        self.assertEqual(journal.verify_journal(binding).record_count, before)

        reserved = self.run_tool(
            "journal",
            "append",
            "--event-type",
            compatibility.COMPATIBILITY_PLAN_EVENT,
        )
        self.assertEqual(reserved.returncode, 2)
        self.assertEqual(
            json.loads(reserved.stderr)["error"]["code"],
            "journal.event_reserved",
        )
        self.assertEqual(journal.verify_journal(binding).record_count, before)

    def test_mutations_require_clean_profile_but_status_remains_available(self) -> None:
        binding = self.initialize()
        readme = self.tool_checkout / "README.md"
        original = readme.read_bytes()
        try:
            readme.write_bytes(original + b"\n")
            planned = self.plan()
            status = self.run_tool("compatibility", "status")
        finally:
            readme.write_bytes(original)

        self.assertEqual(planned.returncode, 2)
        self.assertEqual(
            json.loads(planned.stderr)["error"]["code"],
            "profile.dirty_source",
        )
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(json.loads(status.stdout)["status"], "compatible")
        self.assertEqual(journal.verify_journal(binding).record_count, 0)

    def test_activation_refuses_current_profile_drift_without_appending(self) -> None:
        binding = self.initialize()
        self.enroll_codex()
        planned = self.plan()
        self.assertEqual(planned.returncode, 0, planned.stderr)
        plan_id = json.loads(planned.stdout)["plan"]["plan_id"]
        ready = self.run_tool(
            "compatibility",
            "ready",
            "--plan-id",
            plan_id,
            "--participant",
            "reviewer",
            "--operator-reference",
            "operator confirmed participant compatibility",
        )
        self.assertEqual(ready.returncode, 0, ready.stderr)
        before = journal.verify_journal(binding).record_count
        store = state.StateStore(binding)
        args = Namespace(
            compatibility_command="activate",
            plan_id=plan_id,
            operator_reference="operator activated the fully staged gate",
        )

        with (
            mock.patch.object(
                compatibility_cli,
                "_current_profile_digest",
                return_value="f" * 64,
            ),
            self.assertRaises(compatibility.CompatibilityEventError) as context,
        ):
            compatibility_cli.handle(args, binding, store)

        self.assertEqual(context.exception.code, "compatibility.profile_drift")
        self.assertEqual(journal.verify_journal(binding).record_count, before)

    def test_empty_plan_allows_notes_but_not_retired_participant_history(self) -> None:
        self.initialize()
        note = self.run_tool("journal", "append", "--event-type", "note.context")
        self.assertEqual(note.returncode, 0, note.stderr)
        planned = self.plan()
        self.assertEqual(planned.returncode, 0, planned.stderr)
        plan = json.loads(planned.stdout)["plan"]
        self.assertEqual(plan["frozen_participants"], [])
        activated = self.run_tool(
            "compatibility",
            "activate",
            "--plan-id",
            plan["plan_id"],
            "--operator-reference",
            "operator activated the empty-project gate",
        )
        self.assertEqual(activated.returncode, 0, activated.stderr)

        other = self.base / "other"
        other.mkdir(mode=0o700)
        subprocess.run(
            [project.DEFAULT_GIT_BIN, "-C", str(other), "init", "--quiet"],
            check=True,
        )
        other_state = self.base / "other-state"
        other_binding = project.initialize_project(other, state_root=other_state)
        alternate_prefix = [
            self.tool_command()[0],
            self.tool_command()[1],
            "--project-root",
            str(other),
            "--state-root",
            str(other_state),
        ]
        added = subprocess.run(
            [
                *alternate_prefix,
                "participant",
                "add",
                "--common-name",
                "retired",
                "--display-name",
                "Retired participant",
                "--role",
                "historical",
                "--vendor",
                "codex",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(added.returncode, 0, added.stderr)
        retired = subprocess.run(
            [
                *alternate_prefix,
                "participant",
                "retire",
                "--participant",
                "retired",
                "--reason",
                "participant left before compatibility planning",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(retired.returncode, 0, retired.stderr)
        result = subprocess.run(
            [
                *alternate_prefix,
                "compatibility",
                "plan",
                "--feature-id",
                "causal.ordering",
                "--feature-version",
                "1",
                "--expires-at",
                FUTURE,
                "--operator-reference",
                "operator approved the bounded compatibility plan",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(
            json.loads(result.stderr)["error"]["code"],
            "compatibility.roster_empty",
        )
        self.assertEqual(journal.verify_journal(other_binding).record_count, 2)

    def test_status_reports_unsupported_gate_without_ordinary_replay(self) -> None:
        binding = self.initialize()
        plan_now = dt.datetime.now(dt.UTC).replace(microsecond=0)
        kernel_plan = {
            "format": compatibility.COMPATIBILITY_FORMAT,
            "plan_id": str(uuid.uuid4()),
            "feature_id": compatibility.COMPATIBILITY_KERNEL_FEATURE_ID,
            "feature_version": compatibility.COMPATIBILITY_KERNEL_FEATURE_VERSION,
            "feature_config": {},
            "required_reader_epoch": compatibility.CURRENT_READER_EPOCH,
            "required_capabilities": [compatibility.COMPATIBILITY_KERNEL_CAPABILITY],
            "validation_profile_sha256": "a" * 64,
            "frozen_participants": [],
            "expires_at": FUTURE,
            "operator_reference": "operator approved kernel bootstrap",
        }
        kernel_record = journal.append_record(
            binding,
            event_type=compatibility.COMPATIBILITY_PLAN_EVENT,
            attributes=kernel_plan,
            now=plan_now,
        )
        kernel_activation_now = plan_now + dt.timedelta(seconds=1)
        journal.append_record(
            binding,
            event_type=compatibility.COMPATIBILITY_GATE_ACTIVATED_EVENT,
            attributes={
                "format": compatibility.COMPATIBILITY_FORMAT,
                "plan_id": kernel_plan["plan_id"],
                "plan_record_id": kernel_record["record_id"],
                "plan_record_sha256": kernel_record["record_sha256"],
                "feature_id": kernel_plan["feature_id"],
                "feature_version": kernel_plan["feature_version"],
                "required_reader_epoch": kernel_plan["required_reader_epoch"],
                "required_capabilities": kernel_plan["required_capabilities"],
                "validation_profile_sha256": kernel_plan["validation_profile_sha256"],
                "readiness": [],
                "activated_at": kernel_activation_now.isoformat().replace(
                    "+00:00", "Z"
                ),
                "operator_reference": "operator activated kernel bootstrap",
            },
            now=kernel_activation_now,
        )
        plan = {
            "format": compatibility.COMPATIBILITY_FORMAT,
            "plan_id": str(uuid.uuid4()),
            "feature_id": "causal.ordering",
            "feature_version": 1,
            "feature_config": {},
            "required_reader_epoch": compatibility.CURRENT_READER_EPOCH + 1,
            "required_capabilities": [
                compatibility.COMPATIBILITY_KERNEL_CAPABILITY,
                "causal.ordering/1",
            ],
            "validation_profile_sha256": "a" * 64,
            "frozen_participants": [],
            "expires_at": FUTURE,
            "operator_reference": "operator approved the future reader gate",
        }
        plan_now += dt.timedelta(seconds=2)
        plan_record = journal.append_record(
            binding,
            event_type=compatibility.COMPATIBILITY_PLAN_EVENT,
            attributes=plan,
            now=plan_now,
        )
        activation_now = plan_now + dt.timedelta(seconds=1)
        activation = {
            "format": compatibility.COMPATIBILITY_FORMAT,
            "plan_id": plan["plan_id"],
            "plan_record_id": plan_record["record_id"],
            "plan_record_sha256": plan_record["record_sha256"],
            "feature_id": plan["feature_id"],
            "feature_version": plan["feature_version"],
            "required_reader_epoch": plan["required_reader_epoch"],
            "required_capabilities": plan["required_capabilities"],
            "validation_profile_sha256": plan["validation_profile_sha256"],
            "readiness": [],
            "activated_at": activation_now.isoformat().replace("+00:00", "Z"),
            "operator_reference": "operator activated the future reader gate",
        }
        journal.append_record(
            binding,
            event_type=compatibility.COMPATIBILITY_GATE_ACTIVATED_EVENT,
            attributes=activation,
            now=activation_now,
        )
        projection_path = state.state_projection_path(binding)
        self.assertFalse(projection_path.exists())

        # Status remains available from the intentionally dirty development
        # checkout; unlike mutations, it does not pass the live-profile gate.
        result = self.run_source_tool("compatibility", "status")

        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stderr)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "upgrade_required")
        self.assertEqual(
            payload["upgrade_required"]["code"],
            "compatibility.upgrade_required",
        )
        self.assertEqual(payload["compatibility"]["journal_sequence"], 4)
        self.assertEqual(payload["compatibility"]["verified_journal_sequence"], 4)
        self.assertFalse(projection_path.exists())
        ordinary = self.run_source_tool("state", "status")
        self.assertEqual(ordinary.returncode, 2)
        ordinary_payload = json.loads(ordinary.stderr)
        self.assertEqual(ordinary_payload["status"], "upgrade_required")
        self.assertEqual(
            ordinary_payload["error"]["code"], "compatibility.upgrade_required"
        )
        self.assertEqual(
            ordinary_payload["error"]["validation_profile_sha256"],
            plan["validation_profile_sha256"],
        )
        self.assertEqual(
            ordinary_payload["error"]["missing_capabilities"],
            ["causal.ordering/1"],
        )
        self.assertEqual(
            ordinary_payload["recovery"]["command"], "compatibility status"
        )


if __name__ == "__main__":
    unittest.main()
