# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Synthetic discovery facts; no live product, socket, or permission changes."""

from __future__ import annotations

import asyncio
import io
import json
import subprocess
import time
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest import mock

from mcp.types import TextContent

from tools import cam1_project, cam1_transport
from tools import cam1_transport_native as native
from tools.cam1lib import onboarding, routing
from tools.cam1lib.protocol import (
    DiagnosticUsageError,
    LocalDiagnostics,
    local_diagnostic_fields,
)

SESSION = "aaaaaaaa-0000-4000-8000-000000000001"
OTHER_SESSION = "bbbbbbbb-0000-4000-8000-000000000002"
PRIVATE_LABEL = "synthetic-private-label"


def session(**changes):
    return replace(
        routing.AgentViewSession(
            session_id=SESSION,
            agent_view_id=None,
            product_name=PRIVATE_LABEL,
            cwd="/synthetic/private/cwd",
            kind="interactive",
            state="idle",
            started_at_ms=1000,
            process_id=4242,
        ),
        **changes,
    )


def peer(**changes):
    return replace(
        routing.Peer(
            name=PRIVATE_LABEL,
            ref="abcdef",
            kind="interactive",
            state="idle",
            details=("uds:/synthetic/private.sock",),
            local=True,
            addressable=True,
        ),
        **changes,
    )


class DiscoveryDiagnosticTests(unittest.TestCase):
    def assert_private_values_absent(self, facts):
        text = json.dumps(facts, separators=(",", ":"))
        self.assertLessEqual(len(text.encode()), 4096)
        for secret in (
            SESSION,
            OTHER_SESSION,
            PRIVATE_LABEL,
            "4242",
            "abcdef",
            "/synthetic",
            "stderr-secret",
        ):
            self.assertNotIn(secret, text)

    def selection_failure(self, sessions, code):
        with self.assertRaises(routing.RoutingError) as caught:
            routing.select_agent_view_session(sessions, SESSION)
        self.assertEqual(caught.exception.code, code)
        facts = caught.exception.diagnostics.as_dict()
        self.assert_private_values_absent(facts)
        return facts

    def test_absent_uuid_distinguishes_empty_from_nonempty_inventory(self):
        for sessions, count in (
            ({}, 0),
            ({OTHER_SESSION: (session(session_id=OTHER_SESSION),)}, 1),
        ):
            with self.subTest(count=count):
                facts = self.selection_failure(sessions, "claude.session_not_found")
                self.assertEqual(facts["agent_view"]["row_count"], count)
                self.assertEqual(facts["agent_view"]["distinct_session_count"], count)
                self.assertFalse(facts["agent_view"]["selected_uuid_present"])
                self.assertEqual(facts["list_agents"]["observation"], "not_observed")

    def test_excluded_rows_and_process_precedence_are_explained_not_relaxed(self):
        cases = [
            ((session(state="shell"),), "state_not_addressable", 1),
            ((session(state="unknown-state"),), "state_not_addressable", 1),
            ((session(kind="remote"),), "kind_not_local", 1),
            ((session(process_id=None),), "idless_nonprocess_row", 0),
            (
                (
                    session(process_id=None, agent_view_id="aaaaaaaa"),
                    session(state="shell"),
                ),
                "shadowed_by_process_row",
                1,
            ),
        ]
        for rows, reason, candidates in cases:
            with self.subTest(reason=reason):
                facts = self.selection_failure(
                    {SESSION: rows}, "claude.session_not_local"
                )
                view = facts["agent_view"]
                self.assertTrue(view["selected_uuid_present"])
                self.assertEqual(view["candidate_count"], candidates)
                self.assertEqual(view["rows"][0]["selection_reason"], reason)
        selected = session(state="busy")
        self.assertIs(
            routing.select_agent_view_session({SESSION: (selected,)}, SESSION), selected
        )

    def test_multiple_processes_and_duplicate_names_still_fail(self):
        facts = self.selection_failure(
            {SESSION: (session(), session(process_id=4243))}, "claude.session_ambiguous"
        )
        self.assertEqual(facts["agent_view"]["candidate_count"], 2)
        facts = self.selection_failure(
            {
                SESSION: (session(),),
                OTHER_SESSION: (session(session_id=OTHER_SESSION),),
            },
            "claude.agent_name_ambiguous",
        )
        self.assertEqual(facts["same_name_eligible_session_count"], 2)

    def test_hostile_labels_and_many_rows_are_bounded_and_escaped(self):
        hostile = "\x1b\u202e\u2066" * 30
        rows = tuple(
            session(kind=hostile, state=hostile, process_id=4000 + i) for i in range(20)
        )
        facts = self.selection_failure({SESSION: rows}, "claude.session_ambiguous")
        view = facts["agent_view"]
        self.assertLessEqual(len(view["rows"]), 8)
        self.assertEqual(len(view["rows"]) + view["rows_omitted"], 20)
        self.assertTrue(view["rows"])
        for row in view["rows"]:
            self.assertTrue(row["kind_truncated"])
            for char in ("\x1b", "\u202e", "\u2066"):
                self.assertNotIn(char, row["kind"])
        self.assertIn("\\u202e", view["rows"][0]["kind"])

    def test_list_agents_keeps_excluded_rows_only_as_diagnostics(self):
        observed = (
            peer(state="shell", addressable=False),
            peer(name="other", ref="aaaaaa", local=False, addressable=False),
            peer(name="other2", ref="bbbbbb"),
        )
        eligible = tuple(row for row in observed if row.local and row.addressable)
        with self.assertRaises(native.TransportError) as caught:
            native._correlate_route(
                session(),
                eligible,
                requested_target=None,
                observed_peers=observed,
                observed_sessions={SESSION: (session(),)},
            )
        self.assertEqual(caught.exception.code, "claude.route_not_found")
        facts = caught.exception.diagnostics.as_dict()
        self.assert_private_values_absent(facts)
        listing = facts["list_agents"]
        self.assertEqual(
            (
                listing["row_count"],
                listing["local_count"],
                listing["local_addressable_count"],
                listing["excluded_count"],
                listing["name_match_count"],
            ),
            (3, 2, 1, 2, 1),
        )
        self.assertEqual(listing["rows"][0]["state"], "shell")
        self.assertFalse(listing["rows"][0]["addressable"])
        self.assertEqual(facts["agent_view"]["row_count"], 1)
        with self.assertRaises(native.TransportError) as absent:
            native._correlate_route(
                session(), (), requested_target=None, observed_peers=()
            )
        self.assertEqual(
            absent.exception.diagnostics.as_dict()["list_agents"]["name_match_count"], 0
        )

    def test_route_ambiguity_kind_and_target_mismatch_keep_codes(self):
        for peers, target, code in (
            ((peer(), peer(ref="bbbbbb")), None, "claude.route_ambiguous"),
            ((peer(kind="background"),), None, "claude.route_kind_mismatch"),
            ((peer(),), "other [bbbbbb]", "claude.target_session_mismatch"),
        ):
            with (
                self.subTest(code=code),
                self.assertRaises(native.TransportError) as caught,
            ):
                native._correlate_route(session(), peers, requested_target=target)
            self.assertEqual(caught.exception.code, code)
            self.assert_private_values_absent(caught.exception.diagnostics.as_dict())
        self.assertEqual(
            routing.correlate_route(session(), (peer(state="busy"),)).peer.state, "busy"
        )

    def test_subprocess_failure_diagnostics_without_private_output(self):
        failures = [
            subprocess.TimeoutExpired(["private"], 1, stderr=b"stderr-secret"),
            PermissionError("stderr-secret"),
            subprocess.CompletedProcess([], 7, b"private stdout", b"stderr-secret"),
            subprocess.CompletedProcess([], 0, b"not json", b"stderr-secret"),
        ]
        for failure in failures:
            options = (
                {"side_effect": failure}
                if isinstance(failure, BaseException)
                else {"return_value": failure}
            )
            with (
                self.subTest(failure=type(failure).__name__),
                mock.patch.object(native.subprocess, "run", **options) as run,
            ):
                with self.assertRaises(native.TransportError) as caught:
                    native._discover_agent_view_sessions(
                        claude_bin="/synthetic/claude", timeout_seconds=1
                    )
                facts = caught.exception.diagnostics.as_dict()
                self.assert_private_values_absent(facts)
                self.assertEqual(facts["agent_view"]["observation"], "not_parsed")
                self.assertEqual(facts["list_agents"]["observation"], "not_observed")
                if isinstance(failure, BaseException):
                    self.assertEqual(caught.exception.code, "claude.agents_failure")
                    self.assertEqual(facts["error_class"], type(failure).__name__)
                else:
                    self.assertEqual(facts["exit_code"], failure.returncode)
                    self.assertEqual(
                        facts["stderr"]["byte_length"], len(b"stderr-secret")
                    )
                run.assert_called_once()

    def test_doctor_empty_inventory_is_a_warning_not_a_failed_probe(self):
        with mock.patch.object(
            native.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0, b"[]", b""),
        ) as run:
            probe = native._agent_view_probe_before(
                "/synthetic/claude", time.monotonic() + 30
            )
        run.assert_called_once()
        self.assertEqual(
            (probe["sessions"], probe["row_count"], probe["distinct_session_count"]),
            (0, 0, 0),
        )
        self.assertTrue(probe["ok"])
        self.assertEqual(probe["warnings"][0]["code"], "claude.empty_agent_view")
        with (
            mock.patch.object(
                native, "_approved_binary", return_value=("/synthetic/bin", {})
            ),
            mock.patch.object(native, "_require_product_metadata"),
            mock.patch.object(
                cam1_transport, "_run_probe_before", return_value={"ok": True}
            ),
            mock.patch.object(
                cam1_transport, "_agent_view_probe_before", return_value=probe
            ),
            mock.patch.object(
                cam1_transport, "_mcp_sdk_check", return_value=(True, "2.1.1")
            ),
        ):
            result = cam1_transport.doctor(
                claude_bin="/synthetic/claude",
                codex_bin="/synthetic/codex",
                timeout_seconds=1,
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "ready")

    def test_refresh_names_changed_fields_without_values_or_policy_change(self):
        selected = session()
        changed = replace(
            selected,
            cwd="/other/private",
            product_name="private-new-name",
            process_id=9999,
            kind="background",
            state="busy",
            started_at_ms=2000,
        )
        with mock.patch.object(
            native, "_discover_agent_view_sessions", return_value={SESSION: (changed,)}
        ):
            with self.assertRaises(native.TransportError) as caught:
                asyncio.run(
                    native._refresh_agent_view_session(
                        selected, claude_bin="/synthetic/claude", timeout_seconds=1
                    )
                )
        facts = caught.exception.diagnostics.as_dict()
        self.assertEqual(caught.exception.code, "claude.session_changed")
        self.assertEqual(
            set(facts["changed_fields"]),
            {"cwd", "name", "process_identity", "kind", "start_time"},
        )
        self.assertTrue(facts["process_identity_changed"])
        self.assertEqual(facts["observed_kind_state"]["state"], "busy")
        self.assert_private_values_absent(facts)
        self.assertNotIn("private-new-name", json.dumps(facts))
        # Activity-only drift stays permitted.
        with mock.patch.object(
            native,
            "_discover_agent_view_sessions",
            return_value={SESSION: (replace(selected, state="busy"),)},
        ):
            self.assertEqual(
                asyncio.run(
                    native._refresh_agent_view_session(
                        selected, claude_bin="/synthetic/claude", timeout_seconds=1
                    )
                ).state,
                "busy",
            )

    def test_refresh_missing_uuid_does_not_claim_list_agents_was_unobserved(self):
        with mock.patch.object(
            native, "_discover_agent_view_sessions", return_value={}
        ):
            with self.assertRaises(native.TransportError) as caught:
                asyncio.run(
                    native._refresh_agent_view_session(
                        session(), claude_bin="/synthetic/claude", timeout_seconds=1
                    )
                )
        facts = caught.exception.diagnostics.as_dict()
        self.assertEqual(facts["phase"], "agent_view_refresh")
        self.assertEqual(facts["list_agents"]["observation"], "correlated")

    def test_onboarding_carries_selection_probe_and_parse_diagnostics(self):
        results = [
            subprocess.CompletedProcess([], 0, b"[]", None),
            subprocess.CompletedProcess([], 3, b"", None),
            subprocess.CompletedProcess([], 0, b"invalid", None),
            PermissionError("private"),
            subprocess.TimeoutExpired(["private"], 10),
        ]
        for value in results:
            options = (
                {"side_effect": value}
                if isinstance(value, BaseException)
                else {"return_value": value}
            )
            with (
                self.subTest(value=type(value).__name__),
                mock.patch.object(
                    onboarding.product_approvals, "require_approved_executable"
                ),
                mock.patch.object(
                    onboarding.product_approvals, "require_approved_metadata"
                ),
                mock.patch.object(onboarding.subprocess, "run", **options) as run,
            ):
                with self.assertRaises(DiagnosticUsageError) as caught:
                    onboarding._claude_agent_view("/synthetic/claude", SESSION)
                facts = caught.exception.diagnostics.as_dict()
                self.assert_private_values_absent(facts)
                self.assertIs(run.call_args.kwargs["stderr"], subprocess.DEVNULL)
                if caught.exception.code == "claude.session_not_found":
                    self.assertFalse(facts["agent_view"]["selected_uuid_present"])
                else:
                    self.assertEqual(facts["stderr"]["observation"], "not_captured")

    def test_project_cli_exports_only_the_typed_diagnostic_carrier(self):
        error = DiagnosticUsageError(
            "claude.session_not_found",
            "No observed row",
            routing.agent_view_diagnostics({}, SESSION),
        )
        output = io.StringIO()
        with (
            mock.patch.object(cam1_project, "_resolve", side_effect=error),
            mock.patch("sys.stderr", output),
        ):
            # The parser does not need a real project when dispatch is replaced.
            result = cam1_project.main(["state", "status"])
        self.assertEqual(result, 2)
        self.assertEqual(
            json.loads(output.getvalue())["error"]["diagnostics"],
            error.diagnostics.as_dict(),
        )
        arbitrary = ValueError("private")
        arbitrary.diagnostics = {"body": "private"}
        self.assertEqual(local_diagnostic_fields(arbitrary), {})
        self.assertEqual(
            LocalDiagnostics({"oversized": "x" * 5000}).as_dict(),
            {"omitted": "diagnostic_size_limit"},
        )


class SyntheticDiscoveryClient:
    """Exercise the real MCP wrapper, optionally wrapping its body error."""

    protocol_version = "2025-11-25"

    def __init__(self, *, group_error=False):
        self.group_error = group_error
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, kind, error, traceback):
        if error is not None and self.group_error:
            raise ExceptionGroup("private wrapper text", [error])
        return False

    async def list_tools(self):
        return SimpleNamespace(
            tools=[
                SimpleNamespace(name=name, input_schema={})
                for name in ("ListAgents", "SendMessage")
            ]
        )

    async def call_tool(self, name, arguments):
        self.calls.append(name)
        if name != "ListAgents":
            raise AssertionError("No send is permitted in a discovery failure test")
        return SimpleNamespace(
            is_error=False,
            structured_content=None,
            content=[
                TextContent(
                    type="text",
                    text=f"Peer sessions (1):\n  {PRIVATE_LABEL} [abcdef]  ·  interactive  ·  shell  ·  private detail",
                )
            ],
        )


class DiscoveryWrapperTests(unittest.TestCase):
    def test_preflight_send_and_exception_group_preserve_same_facts_before_intent(self):
        outcomes = []
        for operation in ("preflight", "send"):
            for group in (False, True):
                with self.subTest(operation=operation, group=group):
                    client = SyntheticDiscoveryClient(group_error=group)
                    before_send, before_dispatch = mock.Mock(), mock.Mock()
                    with (
                        mock.patch("mcp.Client", return_value=client),
                        mock.patch(
                            "mcp.client.stdio.stdio_client", return_value=object()
                        ),
                        mock.patch.object(
                            native, "_resolve_binary", return_value="/synthetic/claude"
                        ),
                        mock.patch.object(native, "_require_product_metadata"),
                        mock.patch.object(
                            native,
                            "_discover_agent_view_sessions",
                            return_value={SESSION: (session(),)},
                        ) as discover,
                        mock.patch.object(
                            native,
                            "_validate_envelope",
                            return_value=native.ValidatedEnvelope(
                                raw=b"{}", envelope={}, original=None, original_raw=None
                            ),
                        ),
                    ):
                        arguments = dict(
                            claude_bin="/synthetic/claude",
                            session_id=SESSION,
                            target=None,
                            timeout_seconds=5,
                        )
                        coro = (
                            native._preflight_claude_session(**arguments)
                            if operation == "preflight"
                            else native._send_to_claude(
                                **arguments,
                                envelope_path="/unused",
                                against_path=None,
                                summary="Synthetic discovery failure",
                                before_send=before_send,
                                before_dispatch=before_dispatch,
                            )
                        )
                        with self.assertRaises(native.TransportError) as caught:
                            asyncio.run(coro)
                    self.assertEqual(caught.exception.code, "claude.route_not_found")
                    outcomes.append(caught.exception.diagnostics.as_dict())
                    discover.assert_called_once()
                    before_send.assert_not_called()
                    before_dispatch.assert_not_called()
                    self.assertEqual(client.calls, ["ListAgents"])
        self.assertTrue(all(facts == outcomes[0] for facts in outcomes))


if __name__ == "__main__":
    unittest.main()
