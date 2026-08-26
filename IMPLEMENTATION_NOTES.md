# CAM/1 implementation notes

- Status: non-normative compatibility notes
- Reference snapshot: 2026-08-26

These notes describe one tested same-host interoperability environment. They
are not CAM/1 requirements or vendor commitments, and product behavior may
change. Use current vendor documentation and fresh capability discovery before
relying on a transport.

The normative documents are [PROTOCOL.md](PROTOCOL.md) and
[cam-1.schema.json](cam-1.schema.json). The single supported operator path is
[docs/CODEX_TO_CLAUDE.md](docs/CODEX_TO_CLAUDE.md).

## 1. Scope of the reference implementation

The public helper is intentionally narrow:

- `tools/cam1.py` builds and validates exact CAM/1 envelope bytes offline.
- `tools/cam1_transport.py` performs one local discovery or send operation and
  exits.
- Claude Code owns `ListAgents`, `SendMessage`, local message delivery, and its
  inbound controls.
- Codex owns `codex queue`, any product-internal pending state, and later-turn
  delivery.

CAM/1 does not provide or require a coordination board, App Server controller,
queue reader, database, daemon, inbox, retry loop, raw-socket client, or remote
transport. Those are not fallback paths for this implementation.

The offline validator accepts a selected regular file or stdin so it can inspect
checked-in fixtures and pipelines. That is a conformance check, not a filesystem
provenance claim. Live transport rejects stdin and uses nonblocking acquisition
before accepting only regular files, but the generic path API does not pin or
authenticate an ancestor chain. The supported workflow therefore retains the
operator-verified private `0700` exchange directory as a prerequisite. CAM/1
does not claim integrity against another process running as the same OS user.

## 2. Reference environment

The 2026-08-26 compatibility pass used both products under one macOS account:

| Component | Observed value | Evidence |
|---|---|---|
| Codex CLI | `0.149.1` | local version and `codex queue --help` checks |
| Claude Code | `2.1.246` | local version and MCP capability checks |
| MCP Python SDK | `2.1.1` | installed distribution and offline fake-server round trip |
| MCP protocol selected with Claude | `2025-11-25` | local read-only MCP connection |
| Python | `3.11` | local test run |

The automated suite uses a fake local MCP server and fake Codex executable. It
does not send a live cross-session message. CI covers the declared Python 3.11
through 3.14 range without requiring either vendor product.

This snapshot does not establish behavior on Windows, across operating-system
accounts, in containers, through Remote Control, in cloud sessions, or between
machines. Those cases are outside this project's supported scope.

## 3. Claude stdio MCP client

The transport helper uses the official
[MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) to start
`claude mcp serve` as a child process over direct stdio pipes. The SDK manages
process startup, version negotiation, typed tool calls, and cleanup. Direct
stdio avoids terminal line buffering and shell-quoting hazards.

For each operation the helper:

1. starts a new local MCP child with Claude's cross-machine isolation setting
   enabled;
2. checks that the required MCP tool is present;
3. calls `ListAgents` for fresh discovery;
4. accepts only rows classified as known local session kinds;
5. requires the exact freshly returned qualified name/ref;
6. validates the exact CAM/1 envelope and recipient mapping;
7. for a reply, requires the live target to equal the preserved original
   `reply_to` route;
8. performs at most one `SendMessage` call;
9. requires `success:true` and a canonical transport `msg_id`; and
10. reports the tool result as transport acceptance only before exiting.

Unknown, cloud, and Remote Control rows fail closed. The implementation neither
connects to a session's runtime socket nor exposes an MCP URL.

Both one-shot send commands enforce a 65,536-byte whole-envelope transport
limit. Offline validation retains its 1 MiB parsing bound, but larger valid
documents cannot be passed through this reference path. This keeps the Codex
callback below common single-argument process limits and gives both adapters one
predictable live-message bound. Use an operator-approved local path plus a
digest for larger artifacts.

## 4. Codex callback behavior

In the tested build, `codex queue` is a send command. It does not expose a
supported receive, list, or wait operation. The observed callback path is:

1. Claude validates a reply against the preserved original envelope.
2. The helper invokes `codex queue` once with a literal Codex thread UUID and
   the exact reply bytes.
3. The helper requires the documented stdout queue receipt and confirms that
   its thread UUID equals the requested callback; stderr or an unrelated UUID
   cannot establish acceptance.
4. The originating Codex turn finishes and yields.
5. Codex may deliver the queued item as a later user turn.
6. The recipient preserves and validates that delivered serialization against
   the exact original.

A callback did not reliably interrupt a long active turn. CAM/1 therefore has
no active-turn receive workaround: finish and yield. Do not inspect internal
storage, poll transcripts, or resend merely because a callback has not yet
appeared.

## 5. Evidence boundaries

The following events remain distinct:

- fresh discovery proves that a transport address is currently listed;
- operator correlation maps that address to the intended human role and session;
- a send result proves only that the product transport accepted the call;
- later queue delivery proves that Codex surfaced content to the session;
- a conforming reply validated against the exact original proves application
  handling and protocol correlation; and
- only receiver-owned policy or trusted operator evidence can authorize work.

Neither `notify_when_idle` nor a successful transport call is an application
receipt. A schema-incomplete but correctly correlated callback may be recorded
as handling evidence, but it is not a conforming CAM/1 lifecycle receipt.

## 6. Lessons preserved in regression tests

### Validate preserved bytes, not reconstructed fields

In one interoperability test, a valid UUID was manually retyped with an extra
hexadecimal character and the delivered envelope was falsely rejected. The
preserved serialization still contained a valid UUID. The regression rule is:
serialize once, preserve the exact bytes, validate those bytes, and transport
them unchanged. Never retype or repair an identifier for validation.

### Build complete replies

Another callback matched the expected sender claim, recipient, request ID, and
nonce but omitted required envelope fields and `body_sha256`. That established
handling but could not complete a CAM/1 lifecycle. The supplied reply builder
and schema tests prevent an abbreviated acknowledgment from being presented as
conforming.

### Discovery is not identity

Claude `ListAgents` returns a live name/ref transport address. In the tested MCP
surface it did not expose the operator-supplied session UUID or human role.
Those values require separate operator correlation. A display name, free-text
sender claim, or transport receipt is not cryptographic authentication.

## 7. Current primary references

- [Claude Code cross-session messaging](https://code.claude.com/docs/en/cross-session-messaging)
- [Claude Code as an MCP server](https://code.claude.com/docs/en/mcp#use-claude-code-as-an-mcp-server)
- [Official MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)

These links document vendor capabilities, not CAM/1 conformance. Re-check them
and the installed command surfaces before a release or interoperability claim.
