# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import datetime as dt
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from typing import Any
from unittest import mock

from tools.cam1lib import compatibility, journal, project, state

NOW = dt.datetime(2026, 9, 2, 18, 0, tzinfo=dt.UTC)
PARTICIPANT_ID = "00000000-0000-4000-8000-000000000201"
SESSION_ID = "00000000-0000-4000-8000-000000000202"


class CompatibilityKernelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name).resolve()
        self.repo = self.base / "example-project"
        self.repo.mkdir(mode=0o700)
        subprocess.run(
            [project.DEFAULT_GIT_BIN, "-C", str(self.repo), "init", "--quiet"],
            check=True,
        )
        self.binding = project.initialize_project(
            self.repo,
            state_root=self.base / "state",
            now=NOW,
        )
        self.store = state.StateStore(self.binding)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def plan_attributes(
        self,
        *,
        plan_id: str | None = None,
        feature_version: int = 1,
        required_reader_epoch: int = compatibility.CURRENT_READER_EPOCH,
        required_capabilities: list[str] | None = None,
        frozen_participants: list[dict[str, Any]] | None = None,
        feature_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "format": compatibility.COMPATIBILITY_FORMAT,
            "plan_id": plan_id or str(uuid.uuid4()),
            "feature_id": "compatibility.kernel",
            "feature_version": feature_version,
            "feature_config": feature_config or {},
            "validation_profile_sha256": "a" * 64,
            "required_reader_epoch": required_reader_epoch,
            "required_capabilities": required_capabilities
            or sorted(
                {
                    compatibility.COMPATIBILITY_KERNEL_CAPABILITY,
                    f"compatibility.kernel/{feature_version}",
                }
            ),
            "frozen_participants": frozen_participants or [],
            "expires_at": "2026-09-02T19:00:00Z",
            "operator_reference": "operator approved the bounded compatibility plan",
        }

    def readiness_attributes(
        self,
        plan: dict[str, Any],
        plan_record: dict[str, Any],
        *,
        participant_id: str = PARTICIPANT_ID,
        binding_generation: int = 1,
    ) -> dict[str, Any]:
        return {
            "format": compatibility.COMPATIBILITY_FORMAT,
            "plan_id": plan["plan_id"],
            "plan_record_id": plan_record["record_id"],
            "plan_record_sha256": plan_record["record_sha256"],
            "participant_id": participant_id,
            "binding_generation": binding_generation,
            "reader_epoch": compatibility.CURRENT_READER_EPOCH,
            "capabilities": sorted(compatibility.SUPPORTED_READER_CAPABILITIES),
            "validation_profile_sha256": "a" * 64,
            "ready_at": "2026-09-02T18:10:00Z",
            "operator_reference": "operator confirmed participant compatibility",
        }

    def activation_attributes(
        self,
        plan: dict[str, Any],
        plan_record: dict[str, Any],
        readiness_records: list[tuple[dict[str, Any], dict[str, Any]]],
        *,
        activated_at: str = "2026-09-02T18:20:00Z",
    ) -> dict[str, Any]:
        return {
            "format": compatibility.COMPATIBILITY_FORMAT,
            "plan_id": plan["plan_id"],
            "plan_record_id": plan_record["record_id"],
            "plan_record_sha256": plan_record["record_sha256"],
            "feature_id": plan["feature_id"],
            "feature_version": plan["feature_version"],
            "validation_profile_sha256": plan["validation_profile_sha256"],
            "required_reader_epoch": plan["required_reader_epoch"],
            "required_capabilities": plan["required_capabilities"],
            "readiness": [
                {
                    "participant_id": ready["participant_id"],
                    "record_id": record["record_id"],
                    "record_sha256": record["record_sha256"],
                }
                for ready, record in readiness_records
            ],
            "activated_at": activated_at,
            "operator_reference": "operator activated the fully staged gate",
        }

    def append_plan(self, attributes: dict[str, Any]) -> dict[str, Any]:
        return journal.append_record(
            self.binding,
            event_type=compatibility.COMPATIBILITY_PLAN_EVENT,
            attributes=attributes,
            now=NOW + dt.timedelta(minutes=1),
        )

    def append_readiness(self, attributes: dict[str, Any]) -> dict[str, Any]:
        return journal.append_record(
            self.binding,
            event_type=compatibility.COMPATIBILITY_READINESS_EVENT,
            attributes=attributes,
            now=dt.datetime.fromisoformat(
                attributes["ready_at"].removesuffix("Z") + "+00:00"
            ),
        )

    def append_activation(self, attributes: dict[str, Any]) -> dict[str, Any]:
        return journal.append_record(
            self.binding,
            event_type=compatibility.COMPATIBILITY_GATE_ACTIVATED_EVENT,
            attributes=attributes,
            now=dt.datetime.fromisoformat(
                attributes["activated_at"].removesuffix("Z") + "+00:00"
            ),
        )

    def enroll_participant(self) -> None:
        self.store.participant_add(
            participant_id=PARTICIPANT_ID,
            common_name="reviewer",
            display_name="Example Reviewer",
            role="review",
            vendor="claude-code",
            now=NOW,
        )
        self.store.participant_bind(
            "reviewer",
            session_id=SESSION_ID,
            session_label="example-reviewer",
            session_kind="interactive",
            operator_reference="operator matched the session",
            bound_at="2026-09-02T18:00:01Z",
            now=NOW + dt.timedelta(seconds=1),
        )

    def test_empty_project_can_activate_kernel_gate_and_project_it(self) -> None:
        plan = self.plan_attributes(feature_config={"mode": "strict"})
        plan_record = self.append_plan(plan)
        activation = self.activation_attributes(plan, plan_record, [])
        self.append_activation(activation)

        snapshot = self.store.rebuild()

        self.assertEqual(
            snapshot.compatibility.active_gates["compatibility.kernel"].feature_version,
            1,
        )
        self.assertEqual(
            snapshot.compatibility.active_feature_config("compatibility.kernel"),
            {"mode": "strict"},
        )
        self.assertEqual(
            snapshot.compatibility.staged_plan(plan["plan_id"]).record_id,
            plan_record["record_id"],
        )
        projection = project.read_private_json(
            state.state_projection_path(self.binding)
        )
        self.assertEqual(
            projection["compatibility"]["reader"],
            {
                "epoch": 1,
                "capabilities": [compatibility.COMPATIBILITY_KERNEL_CAPABILITY],
            },
        )
        self.assertEqual(
            projection["compatibility"]["active_gates"][0]["plan_id"],
            plan["plan_id"],
        )
        self.assertNotIn(
            "feature_config", projection["compatibility"]["active_gates"][0]
        )

    def test_complete_frozen_roster_and_readiness_activate_atomically(self) -> None:
        self.enroll_participant()
        plan = self.plan_attributes(
            frozen_participants=[
                {"participant_id": PARTICIPANT_ID, "binding_generation": 1}
            ]
        )
        plan_record = self.append_plan(plan)
        ready = self.readiness_attributes(plan, plan_record)
        ready_record = self.append_readiness(ready)
        self.append_activation(
            self.activation_attributes(plan, plan_record, [(ready, ready_record)])
        )

        snapshot = self.store.rebuild()

        self.assertIn("compatibility.kernel", snapshot.compatibility.active_gates)

    def test_unsupported_epoch_is_actionable_upgrade_required(self) -> None:
        plan = self.plan_attributes(required_reader_epoch=2)
        plan_record = self.append_plan(plan)
        self.append_activation(self.activation_attributes(plan, plan_record, []))
        journal.append_record(
            self.binding,
            event_type="state.causal.future_event",
            attributes={"opaque": True},
            now=NOW + dt.timedelta(minutes=21),
        )

        with self.assertRaises(compatibility.CompatibilityUpgradeRequired) as context:
            self.store.rebuild()

        error = context.exception
        self.assertEqual(error.code, "compatibility.upgrade_required")
        self.assertEqual(error.required_reader_epoch, 2)
        self.assertEqual(error.current_reader_epoch, 1)
        self.assertEqual(error.journal_sequence, 2)
        self.assertIn("Upgrade the CAM checkout", error.detail)

    def test_unsupported_capability_is_actionable_upgrade_required(self) -> None:
        plan = self.plan_attributes(
            required_capabilities=[
                compatibility.COMPATIBILITY_KERNEL_CAPABILITY,
                "causal.ordering/1",
            ]
        )
        plan_record = self.append_plan(plan)
        self.append_activation(self.activation_attributes(plan, plan_record, []))
        journal.append_record(
            self.binding,
            event_type="state.causal.future_event",
            attributes={"opaque": True},
            now=NOW + dt.timedelta(minutes=21),
        )

        with self.assertRaises(compatibility.CompatibilityUpgradeRequired) as context:
            self.store.rebuild()

        self.assertEqual(context.exception.missing_capabilities, ("causal.ordering/1",))
        self.assertEqual(
            context.exception.as_dict()["feature_id"], "compatibility.kernel"
        )
        projection_path = state.state_projection_path(self.binding)
        before_projection = (
            projection_path.read_bytes() if projection_path.exists() else None
        )
        inspection = state.inspect_compatibility(self.binding)
        self.assertEqual(inspection.journal_sequence, 2)
        self.assertEqual(inspection.verified_journal_sequence, 3)
        self.assertEqual(
            inspection.upgrade_required.code, "compatibility.upgrade_required"
        )
        self.assertEqual(
            inspection.compatibility.active_gate("compatibility.kernel").plan_id,
            plan["plan_id"],
        )
        after_projection = (
            projection_path.read_bytes() if projection_path.exists() else None
        )
        self.assertEqual(after_projection, before_projection)

    def test_plan_must_require_its_feature_specific_capability(self) -> None:
        plan = self.plan_attributes()
        plan["feature_id"] = "causal.ordering"

        with self.assertRaises(compatibility.CompatibilityEventError) as context:
            compatibility.validate_plan(plan)

        self.assertEqual(
            context.exception.code,
            "compatibility.feature_capability",
        )

    def test_feature_identifier_and_version_bounds_form_a_legal_capability(
        self,
    ) -> None:
        feature_id = "a" * 54
        feature_version = 999_999_999
        feature_capability = f"{feature_id}/{feature_version}"
        plan = self.plan_attributes(
            feature_version=feature_version,
            required_capabilities=[
                feature_capability,
                compatibility.COMPATIBILITY_KERNEL_CAPABILITY,
            ],
        )
        plan["feature_id"] = feature_id

        validated = compatibility.validate_plan(plan)

        self.assertEqual(len(feature_capability), 64)
        self.assertEqual(validated.feature_id, feature_id)
        too_long = dict(plan)
        too_long["feature_id"] = f"{feature_id}a"
        too_long["required_capabilities"] = [
            compatibility.COMPATIBILITY_KERNEL_CAPABILITY,
            f"{feature_id}a/{feature_version}",
        ]
        with self.assertRaises(compatibility.CompatibilityEventError) as context:
            compatibility.validate_plan(too_long)
        self.assertEqual(context.exception.code, "compatibility.event_schema")

    def test_unsupported_gate_can_be_inspected_without_enabling_state_replay(
        self,
    ) -> None:
        plan = self.plan_attributes(
            required_capabilities=[
                compatibility.COMPATIBILITY_KERNEL_CAPABILITY,
                "causal.ordering/1",
            ]
        )
        plan_record = self.append_plan(plan)
        activation = self.activation_attributes(plan, plan_record, [])
        projection = compatibility.CompatibilityProjection()
        projection.observe_plan(
            plan,
            record_id=plan_record["record_id"],
            record_sha256=plan_record["record_sha256"],
            sequence=plan_record["sequence"],
            recorded_at=plan_record["recorded_at"],
        )

        gate = projection.inspect_activation(
            activation,
            participants={},
            recorded_at=activation["activated_at"],
        )

        self.assertIn("causal.ordering/1", gate.required_capabilities)
        with self.assertRaises(compatibility.CompatibilityUpgradeRequired):
            compatibility.require_reader_support(gate)

    def test_malformed_staging_event_fails_state_replay(self) -> None:
        malformed = self.plan_attributes()
        malformed.pop("operator_reference")
        self.append_plan(malformed)

        with self.assertRaises(state.StateError) as context:
            self.store.rebuild()

        self.assertEqual(context.exception.code, "state.event_invalid")
        self.assertIn("compatibility.event_schema", context.exception.detail)

    def test_staging_event_with_message_bytes_fails_as_invalid_event(self) -> None:
        journal.append_record(
            self.binding,
            event_type=compatibility.COMPATIBILITY_PLAN_EVENT,
            attributes=self.plan_attributes(),
            exact_message=b"not part of a compatibility marker",
            now=NOW + dt.timedelta(minutes=1),
        )

        with self.assertRaises(state.StateError) as context:
            self.store.rebuild()

        self.assertEqual(context.exception.code, "state.event_invalid")
        self.assertIn("state.event_message", context.exception.detail)

    def test_plan_must_expire_after_its_authoritative_journal_time(self) -> None:
        plan = self.plan_attributes()
        plan["expires_at"] = "2026-09-02T18:01:00Z"
        self.append_plan(plan)

        with self.assertRaises(state.StateError) as context:
            self.store.rebuild()

        self.assertEqual(context.exception.code, "state.event_invalid")
        self.assertIn("compatibility.plan_expired", context.exception.detail)

    def test_plan_lifetime_is_bounded_from_authoritative_journal_time(self) -> None:
        plan = self.plan_attributes()
        plan["expires_at"] = "2026-09-09T18:01:01Z"
        self.append_plan(plan)

        with self.assertRaises(state.StateError) as context:
            self.store.rebuild()

        self.assertEqual(context.exception.code, "state.event_invalid")
        self.assertIn("compatibility.plan_lifetime", context.exception.detail)

    def test_operator_references_are_nonblank_single_line_text(self) -> None:
        plan = self.plan_attributes()
        invalid_plan = dict(plan)
        invalid_plan["operator_reference"] = " "
        with self.assertRaises(compatibility.CompatibilityEventError) as context:
            compatibility.validate_plan(invalid_plan)
        self.assertEqual(context.exception.code, "compatibility.event_schema")
        invalid_plan["operator_reference"] = "operator approved\n"
        with self.assertRaises(compatibility.CompatibilityEventError) as context:
            compatibility.validate_plan(invalid_plan)
        self.assertEqual(context.exception.code, "compatibility.event_schema")

        plan_record = self.append_plan(plan)
        readiness = self.readiness_attributes(plan, plan_record)
        readiness["operator_reference"] = "operator confirmation\r"
        with self.assertRaises(compatibility.CompatibilityEventError) as context:
            compatibility.validate_readiness(readiness)
        self.assertEqual(context.exception.code, "compatibility.event_schema")

        activation = self.activation_attributes(plan, plan_record, [])
        activation["operator_reference"] = "operator confirmation\n"
        with self.assertRaises(compatibility.CompatibilityEventError) as context:
            compatibility.validate_activation(activation)
        self.assertEqual(context.exception.code, "compatibility.event_schema")

        separators = ("\u2028", "\u2029", "\u0085", "\v", "\f")
        validators = (
            (compatibility.validate_plan, self.plan_attributes()),
            (
                compatibility.validate_readiness,
                self.readiness_attributes(plan, plan_record),
            ),
            (
                compatibility.validate_activation,
                self.activation_attributes(plan, plan_record, []),
            ),
        )
        for separator in separators:
            for validator, candidate in validators:
                with self.subTest(
                    separator=ord(separator), validator=validator.__name__
                ):
                    invalid = dict(candidate)
                    invalid["operator_reference"] = f"operator{separator}confirmation"
                    with self.assertRaises(compatibility.CompatibilityEventError):
                        validator(invalid)

    def test_readiness_must_follow_and_match_exact_plan_record(self) -> None:
        plan = self.plan_attributes(
            frozen_participants=[
                {"participant_id": PARTICIPANT_ID, "binding_generation": 1}
            ]
        )
        fake_plan_record = {"record_id": str(uuid.uuid4()), "record_sha256": "b" * 64}
        self.append_readiness(self.readiness_attributes(plan, fake_plan_record))

        with self.assertRaises(state.StateError) as context:
            self.store.rebuild()

        self.assertEqual(context.exception.code, "state.event_invalid")
        self.assertIn("compatibility.event_order", context.exception.detail)

    def test_activation_refuses_incomplete_readiness(self) -> None:
        self.enroll_participant()
        plan = self.plan_attributes(
            frozen_participants=[
                {"participant_id": PARTICIPANT_ID, "binding_generation": 1}
            ]
        )
        plan_record = self.append_plan(plan)
        self.append_activation(self.activation_attributes(plan, plan_record, []))

        with self.assertRaises(state.StateError) as context:
            self.store.rebuild()

        self.assertEqual(context.exception.code, "state.event_invalid")
        self.assertIn("compatibility.readiness_incomplete", context.exception.detail)

    def test_readiness_must_use_the_planned_validation_profile(self) -> None:
        self.enroll_participant()
        plan = self.plan_attributes(
            frozen_participants=[
                {"participant_id": PARTICIPANT_ID, "binding_generation": 1}
            ]
        )
        plan_record = self.append_plan(plan)
        readiness = self.readiness_attributes(plan, plan_record)
        readiness["validation_profile_sha256"] = "b" * 64
        self.append_readiness(readiness)

        with self.assertRaises(state.StateError) as context:
            self.store.rebuild()

        self.assertEqual(context.exception.code, "state.event_invalid")
        self.assertIn(
            "compatibility.validation_profile_mismatch", context.exception.detail
        )

    def test_activation_header_must_use_the_planned_validation_profile(self) -> None:
        plan = self.plan_attributes()
        plan_record = self.append_plan(plan)
        activation = self.activation_attributes(plan, plan_record, [])
        activation["validation_profile_sha256"] = "b" * 64
        self.append_activation(activation)

        with self.assertRaises(state.StateError) as context:
            self.store.rebuild()

        self.assertEqual(context.exception.code, "state.event_invalid")
        self.assertIn("compatibility.plan_mismatch", context.exception.detail)

    def test_activation_cannot_backdate_past_authoritative_journal_time(self) -> None:
        plan = self.plan_attributes()
        plan_record = self.append_plan(plan)
        activation = self.activation_attributes(plan, plan_record, [])
        journal.append_record(
            self.binding,
            event_type=compatibility.COMPATIBILITY_GATE_ACTIVATED_EVENT,
            attributes=activation,
            now=NOW + dt.timedelta(minutes=21),
        )

        with self.assertRaises(state.StateError) as context:
            self.store.rebuild()

        self.assertEqual(context.exception.code, "state.event_invalid")
        self.assertIn("compatibility.record_chronology", context.exception.detail)

    def test_empty_plan_cannot_activate_when_roster_is_not_empty(self) -> None:
        self.enroll_participant()
        plan = self.plan_attributes(frozen_participants=[])
        plan_record = self.append_plan(plan)
        self.append_activation(self.activation_attributes(plan, plan_record, []))

        with self.assertRaises(state.StateError) as context:
            self.store.rebuild()

        self.assertEqual(context.exception.code, "state.event_invalid")
        self.assertIn("compatibility.roster_drift", context.exception.detail)

    def test_empty_plan_cannot_activate_after_participant_retirement(self) -> None:
        self.enroll_participant()
        self.store.participant_retire(
            "reviewer",
            reason="workstream ended",
            now=NOW + dt.timedelta(minutes=5),
        )
        plan = self.plan_attributes(frozen_participants=[])
        plan_record = self.append_plan(plan)
        self.append_activation(self.activation_attributes(plan, plan_record, []))

        with self.assertRaises(state.StateError) as context:
            self.store.rebuild()

        self.assertEqual(context.exception.code, "state.event_invalid")
        self.assertIn("compatibility.roster_drift", context.exception.detail)

    def test_activation_refuses_roster_or_binding_drift(self) -> None:
        self.enroll_participant()
        plan = self.plan_attributes(
            frozen_participants=[
                {"participant_id": PARTICIPANT_ID, "binding_generation": 1}
            ]
        )
        plan_record = self.append_plan(plan)
        ready = self.readiness_attributes(plan, plan_record)
        ready_record = self.append_readiness(ready)
        self.store.participant_bind(
            "reviewer",
            session_id="00000000-0000-4000-8000-000000000203",
            session_label="replacement-reviewer",
            session_kind="interactive",
            operator_reference="operator rebound the session",
            bound_at="2026-09-02T18:15:00Z",
            now=NOW + dt.timedelta(minutes=15),
        )
        self.append_activation(
            self.activation_attributes(plan, plan_record, [(ready, ready_record)])
        )

        with self.assertRaises(state.StateError) as context:
            self.store.rebuild()

        self.assertEqual(context.exception.code, "state.event_invalid")
        self.assertIn("compatibility.roster_drift", context.exception.detail)

    def test_activation_refuses_participant_invalidated_after_readiness(self) -> None:
        self.enroll_participant()
        plan = self.plan_attributes(
            frozen_participants=[
                {"participant_id": PARTICIPANT_ID, "binding_generation": 1}
            ]
        )
        plan_record = self.append_plan(plan)
        ready = self.readiness_attributes(plan, plan_record)
        ready_record = self.append_readiness(ready)
        self.store.participant_invalidate(
            "reviewer",
            reason="session identity drifted",
            now=NOW + dt.timedelta(minutes=15),
        )
        self.append_activation(
            self.activation_attributes(plan, plan_record, [(ready, ready_record)])
        )

        with self.assertRaises(state.StateError) as context:
            self.store.rebuild()

        self.assertEqual(context.exception.code, "state.event_invalid")
        self.assertIn("compatibility.roster_unbound", context.exception.detail)

    def test_exact_duplicate_activation_is_idempotent(self) -> None:
        plan = self.plan_attributes()
        plan_record = self.append_plan(plan)
        activation = self.activation_attributes(plan, plan_record, [])
        self.append_activation(activation)
        self.append_activation(activation)

        snapshot = self.store.rebuild()

        self.assertEqual(len(snapshot.compatibility.active_gates), 1)

    def test_supported_gate_does_not_admit_arbitrary_unknown_state(self) -> None:
        plan = self.plan_attributes()
        plan_record = self.append_plan(plan)
        self.append_activation(self.activation_attributes(plan, plan_record, []))
        journal.append_record(
            self.binding,
            event_type="state.unrelated_future_event",
            attributes={},
            now=NOW + dt.timedelta(minutes=21),
        )

        with self.assertRaises(state.StateError) as context:
            self.store.rebuild()

        self.assertEqual(context.exception.code, "state.event_type")

    def test_feature_gate_cannot_bypass_kernel_bootstrap(self) -> None:
        plan = self.plan_attributes(
            required_capabilities=[
                "causal.ordering/1",
                compatibility.COMPATIBILITY_KERNEL_CAPABILITY,
            ]
        )
        plan["feature_id"] = "causal.ordering"
        plan_record = self.append_plan(plan)
        self.append_activation(self.activation_attributes(plan, plan_record, []))

        with self.assertRaises(state.StateError) as context:
            self.store.rebuild()

        self.assertEqual(context.exception.code, "state.event_invalid")
        self.assertIn("compatibility.kernel_inactive", context.exception.detail)

    def test_conflicting_same_or_lower_feature_activation_fails(self) -> None:
        first = self.plan_attributes(feature_version=1)
        first_record = self.append_plan(first)
        self.append_activation(self.activation_attributes(first, first_record, []))
        second = self.plan_attributes(feature_version=1)
        second_record = self.append_plan(second)
        self.append_activation(self.activation_attributes(second, second_record, []))

        with self.assertRaises(state.StateError) as context:
            self.store.rebuild()

        self.assertEqual(context.exception.code, "state.event_invalid")
        self.assertIn("compatibility.gate_conflict", context.exception.detail)

    def test_lower_feature_activation_fails(self) -> None:
        first = self.plan_attributes(feature_version=1)
        first_record = self.append_plan(first)
        self.append_activation(self.activation_attributes(first, first_record, []))
        second = self.plan_attributes(feature_version=2)
        second_record = self.append_plan(second)
        self.append_activation(self.activation_attributes(second, second_record, []))
        third = self.plan_attributes(feature_version=1)
        third_record = self.append_plan(third)
        self.append_activation(self.activation_attributes(third, third_record, []))

        with (
            mock.patch.object(compatibility, "require_reader_support"),
            self.assertRaises(state.StateError) as context,
        ):
            self.store.rebuild()

        self.assertEqual(context.exception.code, "state.event_invalid")
        self.assertIn("compatibility.gate_conflict", context.exception.detail)

    def test_feature_config_is_bounded_and_not_part_of_activation_header(self) -> None:
        config = {"deep": {"value": 1}}
        attributes = self.plan_attributes(feature_config=config)
        plan = compatibility.validate_plan(attributes)
        config["deep"]["value"] = 2
        self.assertEqual(plan.feature_config, {"deep": {"value": 1}})
        exported = plan.as_dict()
        exported["feature_config"]["deep"]["value"] = 3
        self.assertEqual(plan.feature_config, {"deep": {"value": 1}})

        too_deep: dict[str, Any] = {}
        cursor = too_deep
        for _ in range(10):
            nested: dict[str, Any] = {}
            cursor["next"] = nested
            cursor = nested
        attributes["feature_config"] = too_deep
        with self.assertRaises(compatibility.CompatibilityEventError) as context:
            compatibility.validate_plan(attributes)
        self.assertEqual(context.exception.code, "compatibility.feature_config_depth")


if __name__ == "__main__":
    unittest.main()
