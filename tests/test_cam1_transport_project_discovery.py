# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import asyncio
import datetime as dt
import json
import subprocess
import unittest
from unittest import mock

from tools import cam1_transport
from tools.cam1lib import journal, project, state

if __package__:
    from .test_cam1_transport import (
        CLAUDE_SESSION,
        ProjectBoundTransportTestCase,
        build_first_contact,
    )
else:
    from test_cam1_transport import (
        CLAUDE_SESSION,
        ProjectBoundTransportTestCase,
        build_first_contact,
    )


class ProjectTransportDiscoveryTests(ProjectBoundTransportTestCase):
    def test_background_alias_passes_project_preflight_and_send(self) -> None:
        self.add_claude_participant(kind="background")
        self.add_codex_participant()
        raw = build_first_contact()
        envelope = self.private_envelope("background-alias.json", raw)
        marker = self.base / "background-alias.called"
        binary = self.fake_claude(
            returned={
                "success": True,
                "msg_id": "00000000-0000-4000-8000-000000000900",
            },
            peer_kind="background",
            peer_state="busy",
            peer_listing="Peer sessions (1):\n  local-worker [abcdef]  ·  bg  ·  busy  ·  synthetic",
            expected_message=raw,
            marker=marker,
        )
        preflight = self.run_transport(
            "claude-preflight", "--participant", "local-worker", claude_bin=binary
        )
        self.assertEqual(preflight.returncode, 0, preflight.stderr)
        payload = json.loads(preflight.stdout)
        self.assertEqual(payload["status"], "route_preflight")
        self.assertEqual(payload["identity"]["session_id"], CLAUDE_SESSION)
        self.assertEqual(payload["identity"]["kind"], "background")
        self.assertTrue(payload["identity"]["process_backed"])
        self.assertEqual(payload["route"]["kind"], "bg")
        self.assertEqual(payload["participant"]["route_status"], "tool_correlated")
        self.assertFalse(marker.exists())
        self.assertNotIn(
            "message.outbound.intent",
            [record["event_type"] for record in journal.replay_records(self.binding)],
        )

        sent = self.run_transport(
            "claude-send",
            "--participant",
            "local-worker",
            "--envelope",
            str(envelope),
            claude_bin=binary,
        )
        self.assertEqual(sent.returncode, 0, sent.stderr)
        self.assertEqual(json.loads(sent.stdout)["status"], "transport_accepted")
        self.assertEqual(marker.read_text(encoding="utf-8"), "called\n")
        participant = (
            state.StateStore(self.binding).snapshot().roster.select("local-worker")
        )
        self.assertEqual(participant.binding.session_kind, "background")
        self.assertEqual(participant.binding.generation, 1)
        self.assertEqual(participant.route.agent_view_kind, "background")
        self.assertEqual(participant.route.address, "local-worker [abcdef]")
        events = [
            record["event_type"] for record in journal.replay_records(self.binding)
        ]
        self.assertEqual(events.count("message.outbound.intent"), 1)
        self.assertEqual(events.count("transport.accepted"), 1)

    def test_background_alias_does_not_admit_remote_or_unavailable_peers(self) -> None:
        self.add_claude_participant(kind="background")
        self.add_codex_participant()
        marker = self.base / "background-alias-must-not-send"
        envelope = self.private_envelope("excluded-bg.json", build_first_contact())
        before = journal.replay_records(self.binding)
        for activity, detail in (
            ("busy", "Remote Control"),
            ("busy", "cloud"),
            ("busy", "other machine"),
            ("exited", "synthetic"),
            ("unknown-state", "synthetic"),
        ):
            with self.subTest(activity=activity, detail=detail):
                binary = self.fake_claude(
                    returned={
                        "success": True,
                        "msg_id": "00000000-0000-4000-8000-000000000900",
                    },
                    peer_kind="background",
                    peer_state="busy",
                    peer_listing=(
                        "Peer sessions (1):\n"
                        f"  local-worker [abcdef]  ·  bg  ·  {activity}  ·  {detail}"
                    ),
                    marker=marker,
                )
                for command in ("claude-preflight", "claude-send"):
                    extra = (
                        ["--envelope", str(envelope)]
                        if command == "claude-send"
                        else []
                    )
                    completed = self.run_transport(
                        command,
                        "--participant",
                        "local-worker",
                        *extra,
                        claude_bin=binary,
                    )
                    self.assertEqual(completed.returncode, 2, completed.stderr)
                    self.assertEqual(
                        json.loads(completed.stderr)["error"]["code"],
                        "claude.route_not_found",
                    )
                    self.assertFalse(marker.exists())
                    self.assertEqual(journal.replay_records(self.binding), before)

    def test_excluded_list_agents_row_diagnostics_reach_both_clis_without_intent(
        self,
    ) -> None:
        self.add_claude_participant()
        self.add_codex_participant()
        marker = self.base / "must-not-send"
        binary = self.fake_claude(
            returned={
                "success": True,
                "msg_id": "00000000-0000-4000-8000-000000000900",
            },
            marker=marker,
            peer_listing="Peer sessions (1):\n  local-worker [abcdef]  ·  interactive  ·  shell  ·  synthetic",
        )
        envelope = self.private_envelope(
            "discovery-failure.json", build_first_contact()
        )
        diagnostics = []
        before = journal.replay_records(self.binding)
        for command in ("claude-preflight", "claude-send"):
            extra = ["--envelope", str(envelope)] if command == "claude-send" else []
            completed = self.run_transport(
                command, "--participant", "local-worker", *extra, claude_bin=binary
            )
            self.assertEqual(completed.returncode, 2, completed.stderr)
            payload = json.loads(completed.stderr)
            self.assertEqual(payload["error"]["code"], "claude.route_not_found")
            diagnostics.append(payload["error"]["diagnostics"])
            self.assertNotIn("audit", payload)
        self.assertEqual(diagnostics[0], diagnostics[1])
        self.assertEqual(diagnostics[0]["list_agents"]["rows"][0]["state"], "shell")
        self.assertFalse(marker.exists())
        self.assertEqual(journal.replay_records(self.binding), before)

    def test_agent_view_cwd_outside_project_is_rejected(self) -> None:
        self.add_claude_participant()
        outside = self.base / "different-project"
        outside.mkdir(mode=0o700)
        nested_unrelated = self.repo / "nested-unrelated"
        nested_unrelated.mkdir(mode=0o700)
        subprocess.run(
            [
                project.DEFAULT_GIT_BIN,
                "-C",
                str(nested_unrelated),
                "init",
                "--quiet",
            ],
            check=True,
            capture_output=True,
            text=True,
        )

        for cwd in (outside, nested_unrelated):
            with self.subTest(cwd=cwd):
                claude_bin = self.fake_claude(
                    returned={
                        "success": True,
                        "msg_id": "00000000-0000-4000-8000-000000000900",
                    },
                    cwd=cwd,
                )
                completed = self.run_transport(
                    "claude-preflight",
                    "--participant",
                    "local-worker",
                    claude_bin=claude_bin,
                )
                self.assertEqual(completed.returncode, 2, completed.stderr)
                self.assertEqual(
                    json.loads(completed.stderr)["error"]["code"],
                    "claude.project_mismatch",
                )
                facts = json.loads(completed.stderr)["error"]["diagnostics"]
                self.assertEqual(facts["phase"], "project_check")
                self.assertIn(
                    facts["reason"], {"git_probe_failure", "different_common_dir"}
                )
                self.assertNotIn(str(cwd), json.dumps(facts))
        self.assertEqual(
            [record["event_type"] for record in journal.replay_records(self.binding)],
            [state.PARTICIPANT_ADDED, state.PARTICIPANT_BOUND],
        )

    def test_changed_claude_ref_is_freshly_tool_correlated(self) -> None:
        self.add_claude_participant()
        self.add_codex_participant()
        initial = self.fake_claude(
            returned={
                "success": True,
                "msg_id": "00000000-0000-4000-8000-000000000900",
            }
        )
        self.preflight_tool_correlated_route(initial)
        confirmed = self.run_project(
            "participant",
            "confirm-route",
            "--participant",
            "local-worker",
            "--expected-address",
            "local-worker [abcdef]",
            "--operator-reference",
            "historical operator-confirmed route",
        )
        self.assertEqual(confirmed.returncode, 0, confirmed.stderr)
        marker = self.base / "changed-route.called"
        changed = self.fake_claude(
            returned={
                "success": True,
                "msg_id": "00000000-0000-4000-8000-000000000901",
            },
            peer_ref="fedcba",
            marker=marker,
        )
        envelope = self.private_envelope("changed-route.json", build_first_contact())

        completed = self.run_transport(
            "claude-send",
            "--participant",
            "local-worker",
            "--envelope",
            str(envelope),
            claude_bin=changed,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["status"], "transport_accepted")
        self.assertTrue(marker.exists())
        participant = (
            state.StateStore(self.binding).snapshot().roster.select("local-worker")
        )
        self.assertEqual(participant.route.status.value, "tool_correlated")
        self.assertEqual(participant.route.address, "local-worker [fedcba]")
        self.assertIn(
            "message.outbound.intent",
            [record["event_type"] for record in journal.replay_records(self.binding)],
        )

    def test_changed_claude_product_name_requires_stable_rebinding(self) -> None:
        self.add_claude_participant()
        self.add_codex_participant()
        marker = self.base / "changed-name.called"
        changed = self.fake_claude(
            returned={
                "success": True,
                "msg_id": "00000000-0000-4000-8000-000000000901",
            },
            peer_name="renamed-worker",
            marker=marker,
        )
        envelope = self.private_envelope("changed-name.json", build_first_contact())

        completed = self.run_transport(
            "claude-send",
            "--participant",
            "local-worker",
            "--envelope",
            str(envelope),
            claude_bin=changed,
        )

        self.assertEqual(completed.returncode, 2, completed.stderr)
        self.assertEqual(
            json.loads(completed.stderr)["error"]["code"],
            "claude.session_label_mismatch",
        )
        self.assertFalse(marker.exists())
        self.assertIsNone(
            state.StateStore(self.binding)
            .snapshot()
            .roster.select("local-worker")
            .route
        )
        self.assertNotIn(
            "message.outbound.intent",
            [record["event_type"] for record in journal.replay_records(self.binding)],
        )

    def test_changed_claude_session_kind_requires_stable_rebinding(self) -> None:
        self.add_claude_participant()
        self.add_codex_participant()
        marker = self.base / "changed-kind.called"
        changed = self.fake_claude(
            returned={
                "success": True,
                "msg_id": "00000000-0000-4000-8000-000000000901",
            },
            peer_kind="background",
            marker=marker,
        )
        envelope = self.private_envelope("changed-kind.json", build_first_contact())

        completed = self.run_transport(
            "claude-send",
            "--participant",
            "local-worker",
            "--envelope",
            str(envelope),
            claude_bin=changed,
        )

        self.assertEqual(completed.returncode, 2, completed.stderr)
        self.assertEqual(
            json.loads(completed.stderr)["error"]["code"],
            "claude.session_kind_mismatch",
        )
        self.assertFalse(marker.exists())
        self.assertIsNone(
            state.StateStore(self.binding)
            .snapshot()
            .roster.select("local-worker")
            .route
        )
        self.assertNotIn(
            "message.outbound.intent",
            [record["event_type"] for record in journal.replay_records(self.binding)],
        )

    def test_same_uuid_rebind_during_preflight_fails_closed(self) -> None:
        self.add_claude_participant()

        async def discover_after_rebind(**_kwargs):
            event_now = dt.datetime.now(dt.UTC)
            observed_at = event_now.isoformat(timespec="microseconds").replace(
                "+00:00", "Z"
            )
            state.StateStore(self.binding).participant_bind(
                "local-worker",
                session_id=CLAUDE_SESSION,
                session_label="local-worker",
                session_kind="interactive",
                operator_reference="test operator refreshed the stable binding",
                bound_at=observed_at,
                now=event_now,
            )
            return {
                "ok": True,
                "status": "route_preflight",
                "local_only": True,
                "mcp_protocol": "2025-06-18",
                "identity": {
                    "session_id": CLAUDE_SESSION,
                    "agent_view_id": None,
                    "product_name": "local-worker",
                    "cwd": str(self.repo),
                    "kind": "interactive",
                    "state": "idle",
                    "started_at_ms": 1_784_241_375_111,
                },
                "route": {
                    "list_agents_name": "local-worker",
                    "list_agents_ref": "abcdef",
                    "kind": "interactive",
                    "state": "idle",
                },
                "notify_when_idle_supported": True,
                "operator_correlation_required": False,
            }

        with (
            mock.patch.object(
                cam1_transport,
                "_preflight_claude_session",
                new=discover_after_rebind,
            ),
            self.assertRaises(cam1_transport.TransportError) as context,
        ):
            asyncio.run(
                cam1_transport.preflight_project_claude(
                    self.binding,
                    claude_bin=str(self.approved_claude_bin),
                    participant_selector="local-worker",
                    session_id_guard=CLAUDE_SESSION,
                    target_guard=None,
                    timeout_seconds=1,
                )
            )

        self.assertEqual(context.exception.code, "claude.binding_changed")
        participant = (
            state.StateStore(self.binding).snapshot().roster.select("local-worker")
        )
        self.assertEqual(participant.binding.generation, 2)
        self.assertIsNone(participant.route)


if __name__ == "__main__":
    unittest.main()
