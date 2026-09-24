# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Regress Claude receipt preservation and characterize deferred rejection timing.

All product I/O is replaced with an in-memory MCP peer. Project state uses the
existing disposable Git/journal fixture. Approval/source gates are mocked only
for that synthetic peer; these tests do not qualify a live executable or source
checkout. Production receipt parsing, send orchestration, journal outcomes,
retry/slot checks, builders, validation and lifecycle replay remain real.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import signal
from contextlib import contextmanager
from types import SimpleNamespace
from unittest import mock

from mcp.types import TextContent

from tools import cam1, cam1_transport, cam1_transport_native
from tools.cam1lib import journal, project, routing, state, transport_audit

if __package__:
    from .test_cam1_transport import (
        CLAUDE_SESSION,
        CODEX_THREAD,
        ProjectBoundTransportTestCase,
    )
else:
    from test_cam1_transport import (
        CLAUDE_SESSION,
        CODEX_THREAD,
        ProjectBoundTransportTestCase,
    )

RECEIPT_ID = "00000000-0000-4000-8000-000000000901"
FIXED_TIME = dt.datetime(2026, 9, 24, 0, 0, tzinfo=dt.UTC)
CLAUDE_SENDER = {
    "sender_vendor": "claude-code",
    "sender_name": "local-worker",
    "sender_session": CLAUDE_SESSION,
    "reply_transport": "claude_send_message",
    "reply_address": CLAUDE_SESSION,
}
CODEX_SENDER = {
    "sender_vendor": "codex",
    "sender_name": "example-coordinator",
    "sender_session": CODEX_THREAD,
    "reply_transport": "codex_queue",
    "reply_address": CODEX_THREAD,
}


def request(*, now: dt.datetime, expires_in: int = 600) -> bytes:
    return cam1.build_request(
        **CLAUDE_SENDER,
        recipient_vendor="codex",
        recipient_name="example-coordinator",
        recipient_session=CODEX_THREAD,
        risk_class="informational",
        operation="discuss_synthetic_fixture",
        intent="Discuss one synthetic fixture",
        body="No project work or live action is requested.",
        authorization_basis="none",
        expires_in=expires_in,
        now=now,
    )


class SyntheticClient:
    """Real _claude_client wrapper, but no process, socket or product call."""

    protocol_version = "2025-11-25"

    def __init__(
        self,
        *,
        cleanup: str,
        success: bool = True,
        malformed: bool = False,
        call_failure: str | None = None,
        mask_body_error: bool = False,
    ) -> None:
        self.cleanup = cleanup
        self.success = success
        self.malformed = malformed
        self.call_failure = call_failure
        self.mask_body_error = mask_body_error
        self.deadlines: list[asyncio.Timeout] = []
        self.sent: list[dict[str, object]] = []
        self.returned: list[dict[str, object]] = []
        self.cleanup_entered = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, kind, error, traceback):
        self.cleanup_entered = True
        # Never hide a failure in the operation body with the injected fault.
        if error is not None and not self.mask_body_error:
            return False
        if self.cleanup == "error":
            raise OSError("private synthetic cleanup details must not be journaled")
        if self.cleanup == "group":
            raise ExceptionGroup("synthetic cleanup", [OSError("private details")])
        if self.cleanup == "timeout":
            await self.expire_timeout()
        if self.cleanup == "cancel":
            await self.cancel_task()
        if self.cleanup == "interrupt":
            raise KeyboardInterrupt("synthetic interrupt")
        if self.cleanup == "sigint":
            signal.raise_signal(signal.SIGINT)
            await asyncio.sleep(0)
        if self.cleanup == "cancel_group":
            raise BaseExceptionGroup(
                "synthetic cleanup cancellation",
                [asyncio.CancelledError(), OSError("private details")],
            )
        return False

    async def expire_timeout(self) -> None:
        # Expire the actual send timeout deterministically, with no wall-clock
        # delay. Fail loudly if a nested timeout changes the test boundary.
        assert len(self.deadlines) == 1
        self.deadlines[0].reschedule(asyncio.get_running_loop().time() - 1)
        await asyncio.sleep(0)

    async def cancel_task(self) -> None:
        task = asyncio.current_task()
        assert task is not None
        task.cancel()
        await asyncio.sleep(0)

    async def list_tools(self):
        return SimpleNamespace(
            tools=[
                SimpleNamespace(name=name, input_schema={})
                for name in ("ListAgents", "SendMessage")
            ]
        )

    async def call_tool(self, name, arguments):
        if name == "ListAgents":
            content = "Peer sessions (1):\n  local-worker [abcdef]  ·  interactive  ·  idle  ·  started now"
        elif name == "SendMessage":
            self.sent.append(arguments)
            if self.call_failure == "timeout":
                await self.expire_timeout()
            if self.call_failure == "cancel":
                await self.cancel_task()
            response = {"success": self.success, "msg_id": RECEIPT_ID}
            if self.malformed:
                response.pop("msg_id")
            self.returned.append(response)
            content = json.dumps(response)
        else:
            raise AssertionError(f"Unexpected synthetic tool: {name}")
        return SimpleNamespace(
            content=[TextContent(type="text", text=content)],
            structured_content=None,
            is_error=False,
        )


class ClaudeReceiptPreservationTests(ProjectBoundTransportTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.add_claude_participant()
        self.add_codex_participant()
        self.root = request(now=dt.datetime.now(dt.UTC))
        self.root_path = self.private_envelope("root.json", self.root)
        state.StateStore(self.binding).lifecycle_root(self.root)
        self.reply = cam1.build_ack(self.root, **CODEX_SENDER, status_value="accepted")
        self.reply_path = self.private_envelope("reply.json", self.reply)

    @contextmanager
    def synthetic_product(self, client: SyntheticClient):
        selected = routing.AgentViewSession(
            session_id=CLAUDE_SESSION,
            agent_view_id=None,
            product_name="local-worker",
            cwd=str(self.repo),
            kind="interactive",
            state="idle",
            started_at_ms=1_784_241_375_111,
            process_id=4242,
        )
        original_timeout = asyncio.timeout

        def tracked_timeout(seconds):
            deadline = original_timeout(seconds)
            client.deadlines.append(deadline)
            return deadline

        with (
            mock.patch("mcp.Client", return_value=client),
            mock.patch("mcp.client.stdio.stdio_client", return_value=object()),
            mock.patch.object(asyncio, "timeout", side_effect=tracked_timeout),
            mock.patch.object(
                cam1_transport_native,
                "_discover_agent_view_sessions",
                return_value={CLAUDE_SESSION: (selected,)},
            ),
            mock.patch.object(
                cam1_transport_native,
                "_resolve_binary",
                return_value=str(self.approved_claude_bin),
            ),
            mock.patch.object(
                cam1_transport_native, "_require_product_metadata", return_value={}
            ),
            mock.patch.object(
                cam1_transport,
                "_require_live_validation_profile",
                return_value=({"synthetic_test_only": True}, False),
            ),
            mock.patch.object(
                cam1_transport_native,
                "_accepted_claude_message_id",
                wraps=cam1_transport_native._accepted_claude_message_id,
            ) as receipt_parser,
        ):
            yield receipt_parser

    def send(self):
        return asyncio.run(
            cam1_transport.send_project_claude(
                self.binding,
                claude_bin=str(self.approved_claude_bin),
                participant_selector="local-worker",
                session_id_guard=CLAUDE_SESSION,
                target_guard=None,
                envelope_path=str(self.reply_path),
                against_path=str(self.root_path),
                renewal_of=None,
                retry_after_intent=None,
                summary="Synthetic receipt cleanup reproduction",
                timeout_seconds=30,
            )
        )

    def test_control_success_and_clean_exit_preserves_receipt(self) -> None:
        client = SyntheticClient(cleanup="none")
        with self.synthetic_product(client) as receipt_parser:
            result = self.send()
            receipt_parser.assert_called_once()
        self.assertTrue(client.cleanup_entered)
        self.assertEqual(len(client.sent), 1)
        self.assertEqual(client.sent[0]["message"].encode(), self.reply)
        self.assertEqual(result["status"], "transport_accepted")
        self.assertEqual(result["transport_message_id"], RECEIPT_ID)
        self.assertEqual(result["lifecycle"]["state"], "accepted")
        self.assertFalse(result["application_ack"])
        self.assertNotIn("post_send_cleanup", result)
        records = journal.replay_records(self.binding)
        self.assertEqual(records[-1]["event_type"], "transport.accepted")
        self.assertIn(RECEIPT_ID, json.dumps(records[-1]))
        self.assertNotIn("post_send_cleanup", records[-1]["attributes"])

    def test_cleanup_error_preserves_returned_success_receipt(self) -> None:
        self.assert_preserved_success(
            cleanup="error", error_code="claude.mcp_failure", error_class="OSError"
        )

    def test_cleanup_group_preserves_returned_success_receipt(self) -> None:
        self.assert_preserved_success(
            cleanup="group",
            error_code="claude.mcp_failure",
            error_class="ExceptionGroup",
        )

    def test_cleanup_timeout_preserves_returned_success_receipt(self) -> None:
        self.assert_preserved_success(
            cleanup="timeout", error_code="claude.timeout", error_class="TimeoutError"
        )

    def assert_preserved_success(
        self, *, cleanup: str, error_code: str, error_class: str
    ) -> None:
        client = SyntheticClient(cleanup=cleanup)
        with self.synthetic_product(client) as receipt_parser:
            result = self.send()
            receipt_parser.assert_called_once()
        self.assertTrue(client.cleanup_entered)
        self.assertEqual(len(client.sent), 1)
        self.assertEqual(client.sent[0]["message"].encode(), self.reply)
        self.assertEqual(client.returned, [{"success": True, "msg_id": RECEIPT_ID}])
        if cleanup == "timeout":
            self.assertEqual(len(client.deadlines), 1)
            self.assertTrue(client.deadlines[0].expired())
        self.assertEqual(result["status"], "transport_accepted")
        self.assertEqual(result["transport_message_id"], RECEIPT_ID)
        self.assertFalse(result["application_ack"])
        self.assertEqual(result["post_send_cleanup"]["code"], error_code)
        self.assertEqual(result["post_send_cleanup"]["error_class"], error_class)
        self.assertEqual(set(result["post_send_cleanup"]), {"code", "error_class"})
        records = journal.replay_records(self.binding)
        self.assertEqual(records[-1]["event_type"], "transport.accepted")
        self.assertEqual(records[-1]["attributes"]["transport_receipt_id"], RECEIPT_ID)
        self.assertEqual(
            records[-1]["attributes"]["post_send_cleanup"],
            result["post_send_cleanup"],
        )
        self.assertTrue(records[-1]["attributes"]["lifecycle_state_committed"])
        diagnostic_text = json.dumps(records[-1]["attributes"]["post_send_cleanup"])
        self.assertNotIn("private", diagnostic_text)
        self.assertFalse(
            any(r["event_type"] == "transport.not_accepted" for r in records)
        )
        entry = (
            state.StateStore(self.binding)
            .snapshot()
            .lifecycle.entries[json.loads(self.root)["message_id"]]
        )
        self.assertEqual(entry.state.value, "accepted")

        # Known acceptance forbids retry, but releases the uncertain reply slot.
        intent = next(
            r for r in records if r["event_type"] == "message.outbound.intent"
        )
        validated = cam1_transport._validate_envelope(
            str(self.reply_path), str(self.root_path)
        )
        with self.assertRaises(cam1_transport.TransportError) as retry:
            transport_audit._require_safe_retry(
                self.binding,
                validated,
                retry_after_intent=intent["record_id"],
                known_renewal_roots=frozenset(),
            )
        self.assertEqual(retry.exception.code, "transport.already_accepted")
        following = cam1.build_status(
            self.root,
            **CODEX_SENDER,
            status_value="started",
            body="Synthetic follow-up; no live work.",
        )
        following_path = self.private_envelope("following.json", following)
        transport_audit._require_reply_slot_available(
            self.binding,
            cam1_transport._validate_envelope(str(following_path), str(self.root_path)),
        )
        self.assertEqual(journal.replay_records(self.binding), records)
        self.assertEqual(len(client.sent), 1)

    def assert_unknown(self, client: SyntheticClient, error_code: str) -> None:
        with self.synthetic_product(client):
            with self.assertRaises(cam1_transport.TransportError) as context:
                self.send()
        self.assertEqual(context.exception.code, error_code)
        self.assertEqual(context.exception.audit["delivery_state"], "unknown")
        records = journal.replay_records(self.binding)
        self.assertEqual(records[-1]["event_type"], "transport.not_accepted")
        self.assertEqual(records[-1]["attributes"]["delivery_state"], "unknown")
        self.assertFalse(any(r["event_type"] == "transport.accepted" for r in records))
        self.assertEqual(len(client.sent), 1)
        with self.assertRaises(cam1_transport.TransportError) as slot:
            transport_audit._require_reply_slot_available(
                self.binding,
                cam1_transport._validate_envelope(
                    str(self.reply_path), str(self.root_path)
                ),
            )
        self.assertEqual(slot.exception.code, "transport.reply_transition_reserved")

    def test_negative_receipt_wins_over_cleanup_error(self) -> None:
        self.assert_unknown(
            SyntheticClient(cleanup="error", success=False, mask_body_error=True),
            "claude.send_failed",
        )

    def test_negative_receipt_wins_over_cleanup_timeout(self) -> None:
        self.assert_unknown(
            SyntheticClient(cleanup="timeout", success=False, mask_body_error=True),
            "claude.send_failed",
        )

    def test_malformed_receipt_wins_over_cleanup_error(self) -> None:
        self.assert_unknown(
            SyntheticClient(cleanup="error", malformed=True, mask_body_error=True),
            "claude.receipt_unrecognized",
        )

    def test_malformed_receipt_wins_over_cleanup_timeout(self) -> None:
        self.assert_unknown(
            SyntheticClient(cleanup="timeout", malformed=True, mask_body_error=True),
            "claude.receipt_unrecognized",
        )

    def test_timeout_during_call_stays_unknown(self) -> None:
        client = SyntheticClient(cleanup="none", call_failure="timeout")
        self.assert_unknown(client, "claude.timeout")
        self.assertEqual(client.returned, [])

    def assert_interruption_recorded(self, cleanup: str, exception_type: type) -> None:
        client = SyntheticClient(cleanup=cleanup)
        with self.synthetic_product(client):
            with self.assertRaises(exception_type) as context:
                self.send()
        self.assertIn(RECEIPT_ID, " ".join(context.exception.__notes__))
        self.assertEqual(len(client.sent), 1)
        records = journal.replay_records(self.binding)
        self.assertEqual(records[-1]["event_type"], "transport.accepted")
        attrs = records[-1]["attributes"]
        self.assertEqual(attrs["transport_receipt_id"], RECEIPT_ID)
        self.assertTrue(attrs["lifecycle_state_committed"])
        self.assertEqual(
            attrs["post_send_cleanup"],
            {"code": "claude.send_interrupted", "error_class": exception_type.__name__},
        )
        entry = (
            state.StateStore(self.binding)
            .snapshot()
            .lifecycle.entries[json.loads(self.root)["message_id"]]
        )
        self.assertEqual(entry.state.value, "accepted")

    def test_external_cancellation_records_acceptance_then_propagates(self) -> None:
        self.assert_interruption_recorded("cancel", asyncio.CancelledError)

    def test_keyboard_interrupt_records_acceptance_then_propagates(self) -> None:
        self.assert_interruption_recorded("interrupt", KeyboardInterrupt)

    def test_base_exception_group_records_acceptance_then_propagates(self) -> None:
        self.assert_interruption_recorded("cancel_group", BaseExceptionGroup)

    def test_unexpected_finalization_error_does_not_replace_cancellation(self) -> None:
        client = SyntheticClient(cleanup="cancel")
        failure = RuntimeError("private synthetic finalization details")
        with (
            self.synthetic_product(client),
            mock.patch.object(
                cam1_transport, "_finalize_accepted_attempt", side_effect=failure
            ),
        ):
            with self.assertRaises(asyncio.CancelledError) as context:
                self.send()
        self.assertIs(context.exception.__cause__, failure)
        notes = " ".join(context.exception.__notes__)
        self.assertIn(RECEIPT_ID, notes)
        self.assertIn("RuntimeError", notes)
        self.assertIn("do not resend", notes)
        self.assertNotIn("private", notes)
        self.assertEqual(len(client.sent), 1)
        records = journal.replay_records(self.binding)
        self.assertFalse(any(r["event_type"] == "transport.accepted" for r in records))

    def assert_sigint_capture(self) -> asyncio.CancelledError:
        # Exercise asyncio.run's actual first-SIGINT path in this disposable
        # test process. Do not signal a terminal process group or another agent.
        prior_handler = signal.signal(signal.SIGINT, signal.default_int_handler)
        try:
            with self.assertRaises(KeyboardInterrupt) as context:
                self.send()
        finally:
            signal.signal(signal.SIGINT, prior_handler)
        cancelled = context.exception.__context__
        self.assertIsInstance(cancelled, asyncio.CancelledError)
        self.assertIn(RECEIPT_ID, " ".join(cancelled.__notes__))
        return cancelled

    def test_sigint_records_acceptance_before_runner_keyboard_interrupt(self) -> None:
        client = SyntheticClient(cleanup="sigint")
        with self.synthetic_product(client):
            self.assert_sigint_capture()
        records = journal.replay_records(self.binding)
        self.assertEqual(records[-1]["event_type"], "transport.accepted")
        attrs = records[-1]["attributes"]
        self.assertEqual(attrs["transport_receipt_id"], RECEIPT_ID)
        self.assertTrue(attrs["lifecycle_state_committed"])
        self.assertEqual(attrs["post_send_cleanup"]["error_class"], "CancelledError")
        self.assertEqual(len(client.sent), 1)

    def test_sigint_and_lock_failure_preserve_receipt_in_exception_chain(self) -> None:
        client = SyntheticClient(cleanup="sigint")
        with (
            self.synthetic_product(client),
            mock.patch.object(
                project,
                "project_transaction",
                self._post_attempt_lock_failure_transaction(),
            ),
        ):
            cancelled = self.assert_sigint_capture()
        cause = cancelled.__cause__
        self.assertIsInstance(cause, cam1_transport.TransportError)
        self.assertEqual(cause.audit["delivery_state"], "accepted")
        self.assertEqual(cause.audit["transport_receipt_id"], RECEIPT_ID)
        self.assertEqual(len(client.sent), 1)

    def test_cancellation_without_receipt_does_not_invent_acceptance(self) -> None:
        client = SyntheticClient(cleanup="none", call_failure="cancel")
        with self.synthetic_product(client):
            with self.assertRaises(asyncio.CancelledError):
                self.send()
        self.assertEqual(client.returned, [])
        records = journal.replay_records(self.binding)
        self.assertTrue(
            any(r["event_type"] == "message.outbound.intent" for r in records)
        )
        self.assertFalse(any(r["event_type"] == "transport.accepted" for r in records))

    def test_cleanup_failure_and_lock_failure_preserve_known_receipt(self) -> None:
        client = SyntheticClient(cleanup="error")
        with (
            self.synthetic_product(client),
            mock.patch.object(
                project,
                "project_transaction",
                self._post_attempt_lock_failure_transaction(),
            ),
        ):
            with self.assertRaises(cam1_transport.TransportError) as context:
                self.send()
        self.assertEqual(context.exception.code, "transport.acceptance_unjournaled")
        self.assertEqual(context.exception.audit["delivery_state"], "accepted")
        self.assertEqual(context.exception.audit["transport_receipt_id"], RECEIPT_ID)
        self.assertEqual(len(client.sent), 1)

    def test_cancellation_and_lock_failure_preserve_receipt_and_cancellation(
        self,
    ) -> None:
        client = SyntheticClient(cleanup="cancel")
        with (
            self.synthetic_product(client),
            mock.patch.object(
                project,
                "project_transaction",
                self._post_attempt_lock_failure_transaction(),
            ),
        ):
            with self.assertRaises(asyncio.CancelledError) as context:
                self.send()
        cause = context.exception.__cause__
        self.assertIsInstance(cause, cam1_transport.TransportError)
        self.assertEqual(cause.code, "transport.acceptance_unjournaled")
        self.assertEqual(cause.audit["delivery_state"], "accepted")
        self.assertEqual(cause.audit["transport_receipt_id"], RECEIPT_ID)
        self.assertIn(RECEIPT_ID, " ".join(context.exception.__notes__))
        self.assertEqual(len(client.sent), 1)

    def test_failed_lifecycle_commit_retains_acceptance_and_cleanup_diagnostic(
        self,
    ) -> None:
        client = SyntheticClient(cleanup="error")
        with (
            self.synthetic_product(client),
            mock.patch.object(
                transport_audit,
                "_settle_accepted_lifecycle",
                side_effect=cam1.CamUsageError("synthetic.state_failure", "fixture"),
            ),
        ):
            with self.assertRaises(cam1_transport.TransportError) as context:
                self.send()
        self.assertEqual(context.exception.code, "transport.accepted_state_incomplete")
        self.assertEqual(context.exception.audit["transport_receipt_id"], RECEIPT_ID)
        attrs = journal.replay_records(self.binding)[-1]["attributes"]
        self.assertFalse(attrs["lifecycle_state_committed"])
        self.assertEqual(attrs["post_send_cleanup"]["code"], "claude.mcp_failure")
        self.assertEqual(len(client.sent), 1)

    def test_failed_receipt_append_retains_known_acceptance(self) -> None:
        append = journal.append_record

        def fail_acceptance(*args, **kwargs):
            if kwargs.get("event_type") == "transport.accepted":
                raise OSError("synthetic receipt append failure")
            return append(*args, **kwargs)

        client = SyntheticClient(cleanup="error")
        with (
            self.synthetic_product(client),
            mock.patch.object(journal, "append_record", side_effect=fail_acceptance),
        ):
            with self.assertRaises(cam1_transport.TransportError) as context:
                self.send()
        self.assertEqual(context.exception.code, "transport.acceptance_unjournaled")
        self.assertEqual(context.exception.audit["delivery_state"], "accepted")
        self.assertTrue(context.exception.audit["lifecycle_state_committed"])
        self.assertEqual(context.exception.audit["transport_receipt_id"], RECEIPT_ID)
        self.assertEqual(len(client.sent), 1)

    def test_control_unsuccessful_receipt_never_becomes_accepted(self) -> None:
        client = SyntheticClient(cleanup="none", success=False)
        with self.synthetic_product(client) as receipt_parser:
            with self.assertRaises(cam1_transport.TransportError) as context:
                self.send()
            receipt_parser.assert_called_once()
        self.assertEqual(context.exception.code, "claude.send_failed")
        self.assertEqual(context.exception.audit["delivery_state"], "unknown")
        self.assertEqual(len(client.sent), 1)


class DelayedRejectionReproductions(ProjectBoundTransportTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.root = request(now=FIXED_TIME, expires_in=60)
        self.sent = FIXED_TIME + dt.timedelta(seconds=59)
        self.arrived = FIXED_TIME + dt.timedelta(seconds=61)
        self.reply = cam1.build_ack(
            self.root,
            **CODEX_SENDER,
            status_value="rejected",
            expires_in=600,
            now=self.sent,
        )
        self.store = state.StateStore(self.binding)
        self.store.lifecycle_root(self.root, now=FIXED_TIME)

    def assert_arrival_rejected(self) -> None:
        # The very same serialized bytes remain fresh and body-hash-valid.
        standalone = cam1.validate_exact_bytes(self.reply, now=self.arrived)
        self.assertTrue(standalone.fresh)
        self.assertTrue(standalone.body_hash_valid)
        self.assertEqual(
            json.loads(self.reply)["nonce"], json.loads(self.root)["nonce"]
        )
        with self.assertRaises(cam1.CamValidationError) as context:
            cam1.validate_exact_bytes(
                self.reply, against_raw=self.root, now=self.arrived
            )
        self.assertEqual(
            [problem.code for problem in context.exception.problems],
            ["correlation.late_rejection_nonce"],
        )

    def test_same_rejection_correlates_before_but_not_after_root_expiry(self) -> None:
        valid = cam1.validate_exact_bytes(
            self.reply, against_raw=self.root, now=self.sent
        )
        self.assertTrue(valid.correlated)
        self.assert_arrival_rejected()

    def test_prior_journaled_timely_rejection_does_not_save_delayed_duplicate(
        self,
    ) -> None:
        applied = self.store.lifecycle_reply(self.reply, now=self.sent)
        self.assertEqual(applied.state.value, "rejected")
        records_before = journal.replay_records(self.binding)
        with self.assertRaises(cam1.CamValidationError) as context:
            self.store.lifecycle_reply(self.reply, now=self.arrived)
        self.assertEqual(
            [problem.code for problem in context.exception.problems],
            ["correlation.late_rejection_nonce"],
        )
        # Historical replay still succeeds: the earlier terminal outcome is NOT
        # downgraded to expired-unconfirmed by this failed incoming observation.
        entry = self.store.snapshot().lifecycle.entries[
            json.loads(self.root)["message_id"]
        ]
        self.assertEqual(entry.state.value, "rejected")
        self.assertTrue(entry.terminal)
        self.assertEqual(journal.replay_records(self.binding), records_before)

    def test_control_genuine_late_rejection_has_null_nonce(self) -> None:
        late = cam1.build_late_rejection(self.root, **CODEX_SENDER, now=self.arrived)
        self.assertIsNone(json.loads(late)["nonce"])
        validated = cam1.validate_exact_bytes(
            late, against_raw=self.root, now=self.arrived
        )
        self.assertTrue(validated.correlated)
        applied = self.store.lifecycle_reply(late, now=self.arrived)
        self.assertEqual(applied.state.value, "late_rejected")

    def test_control_delayed_acceptance_cannot_revive_unconfirmed_root(self) -> None:
        acceptance = cam1.build_ack(
            self.root, **CODEX_SENDER, status_value="accepted", now=self.sent
        )
        self.assertTrue(
            cam1.validate_exact_bytes(
                acceptance, against_raw=self.root, now=self.arrived
            ).correlated
        )
        records_before = journal.replay_records(self.binding)
        with self.assertRaises(cam1.CamUsageError) as context:
            self.store.lifecycle_reply(acceptance, now=self.arrived)
        self.assertEqual(context.exception.code, "lifecycle.root_expired_before_reply")
        self.assertEqual(journal.replay_records(self.binding), records_before)
