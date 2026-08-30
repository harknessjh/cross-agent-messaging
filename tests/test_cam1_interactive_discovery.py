# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import asyncio
import json
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest import mock

from tools import cam1_transport_native
from tools.cam1lib import routing

CLAUDE_SESSION = "aaaaaaaa-0000-4000-8000-000000000001"
ROOT = Path(__file__).resolve().parents[1]
MIXED_ROWS_FIXTURE = ROOT / "tests" / "fixtures" / "claude-agents-mixed-rows.json"


def background_row() -> dict[str, object]:
    return {
        "id": "aaaaaaaa",
        "cwd": "/example/background-checkout",
        "kind": "background",
        "startedAt": 1_000,
        "sessionId": CLAUDE_SESSION,
        "name": "background-job-label",
        "state": "blocked",
    }


def interactive_row(
    *,
    pid: int = 4_242,
    status: str = "busy",
    agent_view_id: str | None = None,
) -> dict[str, object]:
    row: dict[str, object] = {
        "pid": pid,
        "cwd": "/example/live-checkout",
        "kind": "interactive",
        "startedAt": 2_000,
        "sessionId": CLAUDE_SESSION,
        "name": "live-worker",
        "status": status,
    }
    if agent_view_id is not None:
        row["id"] = agent_view_id
    return row


class InteractiveAgentViewDiscoveryTests(unittest.TestCase):
    def assert_pid_not_serialized(self, value: Any) -> None:
        if isinstance(value, dict):
            self.assertNotIn("pid", value)
            self.assertNotIn("process_id", value)
            for nested in value.values():
                self.assert_pid_not_serialized(nested)
        elif isinstance(value, (list, tuple)):
            for nested in value:
                self.assert_pid_not_serialized(nested)

    def test_mixed_representations_select_live_row_independent_of_order(self) -> None:
        for rows in (
            [background_row(), interactive_row()],
            [interactive_row(), background_row()],
        ):
            with self.subTest(order=[row["kind"] for row in rows]):
                grouped = routing.parse_agent_view_sessions(json.dumps(rows).encode())

                self.assertEqual(len(grouped[CLAUDE_SESSION]), 2)
                selected = routing.select_agent_view_session(grouped, CLAUDE_SESSION)

                self.assertEqual(selected.product_name, "live-worker")
                self.assertEqual(selected.cwd, "/example/live-checkout")
                self.assertEqual(selected.kind, "interactive")
                self.assertEqual(selected.state, "busy")
                self.assertEqual(selected.started_at_ms, 2_000)
                self.assertEqual(selected.process_id, 4_242)
                self.assertTrue(selected.process_backed)
                self.assertIsNone(selected.agent_view_id)

                identity = selected.as_dict()
                self.assertIsNone(identity["agent_view_id"])
                self.assertTrue(identity["process_backed"])
                self.assert_pid_not_serialized(identity)

                peer = routing.Peer(
                    name="live-worker",
                    ref="abcdef",
                    kind="interactive",
                    state="busy",
                    details=("started now",),
                    local=True,
                    addressable=True,
                )
                route = routing.ClaudeRoute(session=selected, peer=peer).as_dict()
                self.assertIsNone(route["agent_view_id"])
                self.assert_pid_not_serialized(route)

    def test_seven_row_product_shape_selects_each_interactive_representation(
        self,
    ) -> None:
        grouped = routing.parse_agent_view_sessions(MIXED_ROWS_FIXTURE.read_bytes())

        self.assertEqual(sum(len(rows) for rows in grouped.values()), 7)
        self.assertEqual(len(grouped), 4)
        expected = {
            "aaaaaaaa-0000-4000-8000-000000000001": "project-a-worker",
            "cccccccc-0000-4000-8000-000000000003": "project-c-worker",
            "dddddddd-0000-4000-8000-000000000004": "project-d-worker",
        }
        for session_id, product_name in expected.items():
            with self.subTest(session_id=session_id):
                selected = routing.select_agent_view_session(grouped, session_id)
                self.assertEqual(selected.product_name, product_name)
                self.assertTrue(selected.process_backed)
                self.assertIsNone(selected.agent_view_id)

    def test_duplicate_live_process_representations_are_rejected(self) -> None:
        rows = [interactive_row(pid=4_242), interactive_row(pid=4_243)]
        grouped = routing.parse_agent_view_sessions(json.dumps(rows).encode())

        with self.assertRaises(routing.RoutingError) as context:
            routing.select_agent_view_session(grouped, CLAUDE_SESSION)

        self.assertEqual(context.exception.code, "claude.session_ambiguous")

    def test_optional_agent_view_id_is_validated_when_present(self) -> None:
        grouped = routing.parse_agent_view_sessions(
            json.dumps([interactive_row()]).encode()
        )
        selected = routing.select_agent_view_session(grouped, CLAUDE_SESSION)
        self.assertIsNone(selected.agent_view_id)

        with self.assertRaises(routing.RoutingError) as context:
            routing.parse_agent_view_sessions(
                json.dumps([interactive_row(agent_view_id="deadbeef")]).encode()
            )

        self.assertEqual(context.exception.code, "claude.agent_id_mismatch")

    def test_legacy_fallback_requires_id_and_process_status_cannot_be_null(
        self,
    ) -> None:
        missing_id = interactive_row()
        missing_id.pop("pid")
        missing_id.pop("status")
        missing_id["state"] = "idle"
        grouped = routing.parse_agent_view_sessions(json.dumps([missing_id]).encode())
        with self.assertRaises(routing.RoutingError) as fallback:
            routing.select_agent_view_session(grouped, CLAUDE_SESSION)
        self.assertEqual(fallback.exception.code, "claude.session_not_local")

        invalid_process = interactive_row()
        invalid_process["status"] = None
        invalid_process["state"] = "idle"
        with self.assertRaises(routing.RoutingError) as status:
            routing.parse_agent_view_sessions(json.dumps([invalid_process]).encode())
        self.assertEqual(status.exception.code, "claude.agents_format")

    def test_stale_companion_name_does_not_create_false_cross_uuid_ambiguity(
        self,
    ) -> None:
        other_session = "bbbbbbbb-0000-4000-8000-000000000002"
        rows = [
            interactive_row(),
            {
                "id": "bbbbbbbb",
                "cwd": "/example/other-background",
                "kind": "background",
                "startedAt": 3_000,
                "sessionId": other_session,
                "name": "live-worker",
                "state": "idle",
            },
            {
                "cwd": "/example/other-live",
                "kind": "interactive",
                "startedAt": 4_000,
                "sessionId": other_session,
                "name": "other-live-worker",
                "pid": 4_244,
                "status": "busy",
            },
        ]
        grouped = routing.parse_agent_view_sessions(json.dumps(rows).encode())

        selected = routing.select_agent_view_session(grouped, CLAUDE_SESSION)

        self.assertEqual(selected.product_name, "live-worker")

    def test_list_agents_separates_locality_from_addressability(self) -> None:
        listing = """Peer sessions (3):
  busy-worker [aaaaaa]  ·  interactive  ·  busy  ·  started now
  exited-worker [bbbbbb]  ·  interactive  ·  exited  ·  started earlier
  remote-worker [cccccc]  ·  interactive  ·  idle  ·  Remote Control
"""

        peers = {peer.name: peer for peer in routing.parse_list_agents_peers(listing)}

        self.assertTrue(peers["busy-worker"].local)
        self.assertTrue(peers["busy-worker"].addressable)
        self.assertTrue(peers["exited-worker"].local)
        self.assertFalse(peers["exited-worker"].addressable)
        self.assertFalse(peers["remote-worker"].local)
        self.assertFalse(peers["remote-worker"].addressable)
        self.assertTrue(peers["busy-worker"].as_dict()["addressable"])

    def test_refresh_allows_status_change_but_rejects_pid_change(self) -> None:
        selected = routing.AgentViewSession(
            session_id=CLAUDE_SESSION,
            agent_view_id=None,
            product_name="live-worker",
            cwd="/example/live-checkout",
            kind="interactive",
            state="busy",
            started_at_ms=2_000,
            process_id=4_242,
        )
        idle = replace(selected, state="idle")
        with mock.patch.object(
            cam1_transport_native,
            "_discover_agent_view_sessions",
            return_value={CLAUDE_SESSION: (idle,)},
        ):
            refreshed = asyncio.run(
                cam1_transport_native._refresh_agent_view_session(
                    selected,
                    claude_bin="/not/executed/claude",
                    timeout_seconds=1,
                )
            )
        self.assertEqual(refreshed.state, "idle")

        different_process = replace(selected, process_id=4_243)
        with (
            mock.patch.object(
                cam1_transport_native,
                "_discover_agent_view_sessions",
                return_value={CLAUDE_SESSION: (different_process,)},
            ),
            self.assertRaises(cam1_transport_native.TransportError) as context,
        ):
            asyncio.run(
                cam1_transport_native._refresh_agent_view_session(
                    selected,
                    claude_bin="/not/executed/claude",
                    timeout_seconds=1,
                )
            )
        self.assertEqual(context.exception.code, "claude.session_changed")


if __name__ == "__main__":
    unittest.main()
