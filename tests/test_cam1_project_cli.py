# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import datetime as dt
import json
import subprocess
import unittest

from tools.cam1lib import journal, project, state
from tools.cam1lib.participants import RouteStatus

if __package__:
    from .test_cam1_project import (
        CLAUDE_PARTICIPANT,
        CLAUDE_SESSION,
        CODEX_PARTICIPANT,
        CODEX_SESSION,
        ProjectTestCase,
    )
else:
    from test_cam1_project import (
        CLAUDE_PARTICIPANT,
        CLAUDE_SESSION,
        CODEX_PARTICIPANT,
        CODEX_SESSION,
        ProjectTestCase,
    )


class ProjectCliTests(ProjectTestCase):
    def test_cli_round_trip_and_concurrent_appends(self) -> None:
        init = self.run_tool("project", "init")
        self.assertEqual(init.returncode, 0, init.stderr)
        initialized = json.loads(init.stdout)
        self.assertEqual(initialized["journal"]["record_count"], 0)
        exchange = self.base / "exchange"
        exchange.mkdir(mode=0o700)
        message_path = exchange / "message.bin"
        message_path.write_bytes(b"CLI-MESSAGE-SECRET")
        message_path.chmod(0o600)
        attributes_path = exchange / "attributes.json"
        attributes_path.write_text(
            '{"token":"CLI-ATTRIBUTE-SECRET"}\n', encoding="utf-8"
        )
        attributes_path.chmod(0o600)

        append = self.run_tool(
            "journal",
            "append",
            "--event-type",
            "note.sent",
            "--message",
            str(message_path),
            "--attributes-file",
            str(attributes_path),
        )
        self.assertEqual(append.returncode, 0, append.stderr)
        self.assertNotIn("CLI-MESSAGE-SECRET", append.stdout)
        self.assertNotIn("CLI-ATTRIBUTE-SECRET", append.stdout)

        reserved = self.run_tool(
            "journal",
            "append",
            "--event-type",
            "state.typo",
        )
        self.assertEqual(reserved.returncode, 2)
        self.assertEqual(
            json.loads(reserved.stderr)["error"]["code"],
            "journal.event_reserved",
        )
        for reserved_event in (
            "message.outbound.intent",
            "message.inbound.observed",
            "transport.not_accepted",
            "journal.recovered_partial_tail",
        ):
            reserved = self.run_tool(
                "journal",
                "append",
                "--event-type",
                reserved_event,
            )
            self.assertEqual(reserved.returncode, 2)
            self.assertEqual(
                json.loads(reserved.stderr)["error"]["code"],
                "journal.event_reserved",
            )

        command = self.tool_command("journal", "append", "--event-type", "worker.event")
        first = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        second = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        first_stdout, first_stderr = first.communicate(timeout=20)
        second_stdout, second_stderr = second.communicate(timeout=20)
        self.assertEqual(first.returncode, 0, first_stderr or first_stdout)
        self.assertEqual(second.returncode, 0, second_stderr or second_stdout)

        verify = self.run_tool("journal", "verify")
        tail = self.run_tool("journal", "tail", "--limit", "3")
        self.assertEqual(json.loads(verify.stdout)["journal"]["record_count"], 3)
        self.assertEqual(len(json.loads(tail.stdout)["records"]), 3)
        self.assertNotIn("CLI-MESSAGE-SECRET", tail.stdout)
        self.assertNotIn("CLI-ATTRIBUTE-SECRET", tail.stdout)
        full_tail = self.run_tool("journal", "tail", "--limit", "3", "--show-content")
        self.assertIn("CLI-MESSAGE-SECRET", full_tail.stdout)
        self.assertIn("CLI-ATTRIBUTE-SECRET", full_tail.stdout)

    def test_participant_cli_codex_binding_is_ready_and_list_is_redacted(self) -> None:
        self.initialize()
        added = self.run_tool(
            "participant",
            "add",
            "--common-name",
            "bob-implementer",
            "--display-name",
            "BoB phase implementer",
            "--role",
            "implementation",
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
            "bob-implementer",
            "--session-id",
            CODEX_SESSION,
            "--session-label",
            "BoB phase-04 implementer",
            "--session-kind",
            "interactive",
            "--operator-reference",
            "operator inspected the active local session",
        )
        self.assertEqual(bound.returncode, 0, bound.stderr)
        bound_payload = json.loads(bound.stdout)
        self.assertEqual(
            bound_payload["participant"]["route"]["status"],
            "operator_correlated",
        )
        self.assertEqual(
            bound_payload["participant"]["binding"]["session_id"], "redacted"
        )

        redacted = self.run_tool("participant", "list")
        self.assertEqual(redacted.returncode, 0, redacted.stderr)
        self.assertNotIn(CODEX_SESSION, redacted.stdout)
        listed = json.loads(redacted.stdout)["roster"]["participants"][0]
        self.assertEqual(listed["binding"]["session_id"], "redacted")
        self.assertEqual(listed["route"]["address"], "redacted")

        revealed = self.run_tool("participant", "list", "--show-identifiers")
        self.assertEqual(revealed.returncode, 0, revealed.stderr)
        self.assertIn(CODEX_SESSION, revealed.stdout)
        full = json.loads(revealed.stdout)["roster"]["participants"][0]
        self.assertEqual(full["route"]["address"], CODEX_SESSION)

        binding = project.resolve_project(self.repo, state_root=self.state_root)
        event_types = [
            record["event_type"] for record in journal.replay_records(binding)
        ]
        self.assertEqual(
            event_types,
            [
                state.PARTICIPANT_ADDED,
                state.PARTICIPANT_BOUND,
                state.PARTICIPANT_ROUTE_OBSERVED,
                state.PARTICIPANT_ROUTE_CONFIRMED,
            ],
        )

        invalidated = self.run_tool(
            "participant",
            "invalidate",
            "--participant",
            "bob-implementer",
            "--reason",
            "session restarted",
        )
        retired = self.run_tool(
            "participant",
            "retire",
            "--participant",
            "bob-implementer",
            "--reason",
            "workstream ended",
        )
        self.assertEqual(invalidated.returncode, 0, invalidated.stderr)
        self.assertEqual(retired.returncode, 0, retired.stderr)
        self.assertEqual(json.loads(retired.stdout)["status"], "retired")

        status_result = self.run_tool("state", "status")
        rebuild_result = self.run_tool("state", "rebuild")
        self.assertEqual(status_result.returncode, 0, status_result.stderr)
        self.assertEqual(rebuild_result.returncode, 0, rebuild_result.stderr)
        self.assertEqual(
            json.loads(status_result.stdout)["state"]["participant_count"], 1
        )
        self.assertEqual(json.loads(rebuild_result.stdout)["status"], "rebuilt")

    def test_participant_cli_requires_kind_for_new_claude_binding(self) -> None:
        binding = self.initialize()
        added = self.run_tool(
            "participant",
            "add",
            "--common-name",
            "bob-reviewer",
            "--display-name",
            "Example code review",
            "--role",
            "review",
            "--vendor",
            "claude-code",
            "--participant-id",
            CLAUDE_PARTICIPANT,
        )
        self.assertEqual(added.returncode, 0, added.stderr)

        rejected = self.run_tool(
            "participant",
            "bind",
            "--participant",
            "bob-reviewer",
            "--session-id",
            CLAUDE_SESSION,
            "--session-label",
            "Example code review",
            "--operator-reference",
            "operator matched Claude status output",
        )

        self.assertEqual(rejected.returncode, 2, rejected.stderr)
        self.assertEqual(
            json.loads(rejected.stderr)["error"]["code"],
            "roster.session_kind_required",
        )
        records = journal.replay_records(binding)
        self.assertEqual(
            [record["event_type"] for record in records],
            [state.PARTICIPANT_ADDED],
        )

    def test_participant_cli_confirms_an_observed_claude_route(self) -> None:
        binding = self.initialize()
        add = self.run_tool(
            "participant",
            "add",
            "--common-name",
            "bob-reviewer",
            "--display-name",
            "Example code review",
            "--role",
            "review",
            "--vendor",
            "claude-code",
            "--participant-id",
            CLAUDE_PARTICIPANT,
        )
        bind = self.run_tool(
            "participant",
            "bind",
            "--participant",
            "bob-reviewer",
            "--session-id",
            CLAUDE_SESSION,
            "--session-label",
            "Example code review",
            "--session-kind",
            "interactive",
            "--operator-reference",
            "operator matched Claude status output",
        )
        self.assertEqual(add.returncode, 0, add.stderr)
        self.assertEqual(bind.returncode, 0, bind.stderr)

        store = state.StateStore(binding)
        observed = dt.datetime.now(dt.UTC)
        observed_text = observed.isoformat(timespec="microseconds").replace(
            "+00:00", "Z"
        )
        store.participant_observe_route(
            "bob-reviewer",
            transport="claude_send_message",
            address="example-code-review [abc123]",
            source="agent_view_and_list_agents",
            observed_at=observed_text,
            agent_view_id="00000000",
            list_agents_name="example-code-review",
            list_agents_ref="abc123",
            product_state="idle",
            now=observed,
        )

        confirmed = self.run_tool(
            "participant",
            "confirm-route",
            "--participant",
            "bob-reviewer",
            "--expected-address",
            "example-code-review [abc123]",
            "--operator-reference",
            "operator confirmed the discovered local route",
        )
        self.assertEqual(confirmed.returncode, 0, confirmed.stderr)
        snapshot = store.snapshot()
        participant = snapshot.roster.participants[CLAUDE_PARTICIPANT]
        self.assertEqual(participant.route.status, RouteStatus.OPERATOR_CORRELATED)


if __name__ == "__main__":
    unittest.main()
