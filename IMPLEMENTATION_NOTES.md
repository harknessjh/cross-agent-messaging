# CAM/1 implementation notes

- Status: non-normative compatibility notes
- Reference snapshot: 2026-08-30

These notes describe one tested same-host interoperability environment. They
are not CAM/1 requirements or vendor commitments, and product behavior may
change. Use current vendor documentation and fresh capability discovery before
relying on a transport.

The normative documents are [PROTOCOL.md](PROTOCOL.md) and
[cam-1.schema.json](cam-1.schema.json). The supported operator path is the
[first-contact runbook](docs/FIRST_CONTACT.md). Exact commands and
troubleshooting are in the optional
[detailed Codex-to-Claude procedure](docs/CODEX_TO_CLAUDE.md), and project-state
details are in [the journal guide](docs/PROJECT_JOURNAL.md).

## 1. Scope of the reference implementation

The public tools are intentionally narrow:

- `tools/cam1.py` builds and validates exact CAM/1 envelope bytes offline.
- `tools/cam1_project.py` creates and verifies one Git-bound project journal.
- `tools/cam1_transport.py` performs one local discovery or send operation and
  exits.
- Claude Code owns Agent View, `ListAgents`, `SendMessage`, local message
  delivery, and its inbound controls.
- Codex owns `codex queue`, product-internal pending state, and later-turn
  delivery.

CAM/1 does not use the earlier coordination-board experiment. It provides no
App Server controller, database, daemon, queue reader, polling loop, automatic
retry service, GUI, automatic executor, raw-socket client, or remote transport.
Those are not fallback paths.

The required project journal is durable audit history, not a transport. It
does not deliver, wake, or instruct a session. The reference implementation
stores it outside repositories and worktrees beneath `~/CAM/Journals`, with a
private pointer in the Git common directory.

The reference participant state records active bound sessions and
operator-correlated routes. It does not add a cryptographic or mandatory
challenge-based enrollment layer. The optional CAM challenge builders can
record reachability evidence, but that evidence neither authenticates a peer
nor authorizes work.

## 2. Reference environment

The live-transport compatibility pass used both products under one macOS
account. A later read-only discovery check captured the changed Claude field
shapes:

| Component | Observed value | Evidence |
|---|---|---|
| Codex CLI | `0.149.1` | local version and `codex queue --help` checks |
| Claude Code live transport | `2.1.246` | local version, Agent View, MCP capability checks, and prior round trips |
| Claude Code discovery fields | `2.1.251` | read-only Agent View JSON and MCP `ListAgents` capture plus synthetic fixtures; no live send |
| MCP Python SDK | `2.1.1` | installed distribution and fake-server round trip |
| MCP protocol selected with Claude | `2025-11-25` | local read-only MCP connection |
| Python | `3.11` | local test run |

The automated transport suite uses fake local Claude and Codex executables. Its
2.1.251 compatibility cases reproduce captured field shapes but do not send a
live cross-session message. CI covers Python 3.11 through 3.14 without requiring
either vendor product.

This snapshot does not establish behavior on native Windows, across accounts,
in containers, through Remote Control, in cloud sessions, or between machines.
Those cases are outside this project's supported scope.

## 3. Claude identity and route resolution

Two Claude Code discovery surfaces provide different evidence:

- `claude agents --json` is a heterogeneous inventory. A process-backed row may
  expose `pid`/`status` without `id`/`state`; a background lifecycle row may
  expose `id`/`state`, and both representations can share a full `sessionId`.
- MCP `ListAgents` exposes the current addressable `name [six-character-ref]`
  route and peer activity, but not the full session UUID.

The full session UUID shown by the target session's `/status` and Agent View is
the stable identity used in the project roster and `reply_to.address`. A product
name is mutable and may be shared. The MCP short ref is transient. A UDS peer
address is product-internal correlation metadata and is never used by CAM/1.

For each Claude send, the helper:

1. runs fresh `claude agents --json` discovery;
2. groups rows by exact full session UUID and, when process-backed evidence is
   present, selects its sole eligible process row without merging companion
   fields; one eligible legacy `id`/`state` row remains the fallback when no
   process row is emitted;
3. starts a local `claude mcp serve` child through the official MCP Python SDK;
4. checks the live `ListAgents` and `SendMessage` tool schemas;
5. obtains a fresh `ListAgents` result;
6. requires one addressable local peer whose name uniquely matches the selected
   Agent View row;
7. requires the Agent View cwd to resolve inside the bound Git project,
   including initialized linked worktrees sharing its Git common directory;
8. optionally verifies an operator-supplied exact `name [ref]` guard;
9. validates the envelope and its intended recipient session;
10. performs at most one `SendMessage` call; and
11. reports `success:true` plus a canonical `msg_id` as transport acceptance
    only.

An Agent View `id` is optional. When present, the helper validates that it is
eight hexadecimal characters matching the full UUID prefix; when absent, the
public identity and route retain `agent_view_id: null`. The process ID is used
only transiently for selection and refresh-incarnation checking. It is neither
serialized nor stored in the journal or roster.

The helper restarts and rediscovers for each operation; it never caches an MCP
route across sends. It treats locality separately from activity: an eligible
same-host `busy` peer is addressable, local terminal or unknown states are
reported as unavailable, and cloud or Remote Control rows are nonlocal.
Duplicate-name, multiple-live-representation, ambiguous, and mismatched rows
fail closed. It neither connects to the session UDS nor exposes an MCP URL.

Direct child-process stdio through the MCP SDK avoids the terminal canonical
line-buffering and shell-quoting failures observed with hand-written long
JSON-RPC lines.

## 4. Codex callback behavior

In the tested build, `codex queue` sends but does not expose a supported
receive, list, or wait operation. The supported callback path is:

1. Claude appends the complete product-visible request serialization to the
   project journal before parsing it; hidden product framing is not observable.
2. Claude validates it and builds a complete reply against the exact root.
3. The helper invokes `codex queue` once with the sender's literal full thread
   UUID and exact reply bytes.
4. The helper requires the documented stdout receipt and confirms the returned
   thread UUID.
5. The originating Codex turn finishes and yields.
6. Codex may surface the queued reply as a later user turn.
7. Codex appends those delivered bytes before validation and then validates
   them against the exact root.

A callback did not reliably interrupt a long active Codex turn. CAM/1 therefore
does not inspect an internal queue database or poll a transcript as a receive
workaround. The sender yields and the operator checks the target session if a
reply remains absent.

Claude peer activity is scheduling state, not a receipt. A local `busy` peer
remains addressable and can accept a send, but `busy` does not prove that the
target displayed or processed the message. Idle notifications can arrange a
future notification or turn and likewise do not prove processing.

## 5. Required project journal

Initialization derives one project UUID from a private binding in the Git
common directory and one worktree UUID from the current Git directory. Linked
worktrees share a project journal while remaining distinguishable.

The external project directory contains:

- `identity.json`, the project identity;
- `journal.jsonl`, the append-only canonical history;
- `transaction.lock`, the project mutation lock; and
- `state-current.json`, the disposable atomic participant and lifecycle
  projection once the journal contains state events.

Each journal record stores exact optional message bytes as base64, links to the
prior record digest, and is automatically stamped with worktree ID plus Git
HEAD, tree, branch, and dirty state. A mutation transaction verifies the
complete chain once, revalidates the journal identity before each operation,
and advances its verified view only from the exact appended record bytes.
Appends serialize under the project lock, write complete records, and fail
closed rather than repairing damaged history automatically. An explicit
operator-only partial-tail recovery requires the exact current journal digest
and project UUID, archives every damaged byte, and atomically preserves the
verified prefix plus a recovery record. Complete malformed, altered, or
chain-invalid records remain unrecoverable by the tool. The current projection
is rebuilt from the journal and cannot override it.

The reference journal currently stops at 100,000 records or 128 MiB and has no
rollover or compaction operation. A read-only `state status` is a pure replay
of recorded events and may show an overdue pending or held item in its last
recorded state until a later mutation appends the corresponding aging event.
Live send and ingest paths still recheck time at their commit boundaries.

Transport operations use two short project transactions: the first resolves
current state and commits outbound intent; the second records the transport
outcome and lifecycle change after product I/O returns. The project lock is not
held while Claude MCP or Codex queue is running. Lock acquisition is bounded;
contention reports `transaction.busy` instead of waiting indefinitely. That
journal-lock condition is unrelated to a Claude peer whose activity is
`busy`; the latter remains an addressable local discovery result.

All project and provenance Git probes invoke the bound absolute executable
with a minimal noninteractive environment, `--no-optional-locks`,
`core.fsmonitor=false`, and `core.hooksPath` disabled. Status ignores
submodules. These probes do not consult user or system Git configuration and
do not execute repository hooks.

The default redacted tail command does not emit bodies or event attributes. The
complete journal remains available to the local operator for audit. This is a
same-user record: hash chaining detects inconsistencies but cannot prevent a
same-account attacker from replacing all state.

Inbound ingestion requires the caller's project-local participant name. It
appends the exact bytes first, then checks that the envelope recipient vendor,
common name, and full session UUID match that active bound participant and that
the claimed sender matches exactly one active bound participant. A mismatch is
preserved and rejected rather than silently routed or repaired.

Successful ingest is recorded and reported as `validated`, not `accepted`.
The result explicitly says that authorization was not evaluated and no action
was authorized; lifecycle acceptance remains a separate correlated event.

## 6. Typed builders and lifecycle

`tools/cam1.py` supplies builders for:

- hello, challenge, and verification;
- typed work or information requests;
- received, held, accepted, and rejected acknowledgments;
- accepted and started status;
- completed result and failed error;
- operator-confirmed cancellation;
- status inquiry;
- safe renewal of an expired request; and
- a fresh rejection of a root that arrived after expiry.

Reply builders take the exact root file and derive correlation fields rather
than asking the caller to reconstruct them. Status, result, and error builders
can take a previous response so they can check stateful ordering before writing
new bytes.

The local lifecycle projection rejects regressions, responses after a terminal
state, duplicate semantic execution, and result-before-acceptance sequences.
One root nonce can appear in only one non-interim ACK or verification. After
`ack: received`, a request or cancel advances to nonce-null `status: accepted`,
not a second ACK. After `ack: needs_human_confirmation`, a request advances to
`ack: accepted` or `ack: rejected` because the interim ACK did not consume the
nonce.

Pending and held roots expire unconfirmed. A request recorded as received,
accepted, or started before expiry may continue, although received still must
advance to accepted before work. An expired root remains in history. Renewal
creates a new root and fresh authorization while retaining the operation
idempotency key only when it is the same action. A receiver that first sees an
expired message does not process its request; it may emit the typed late
rejection.

The reference transport adapter distinguishes a retry from a renewal. A retry
must name the latest exact outbound intent and is allowed only when its
journaled outcome proves dispatch was not attempted. It reuses the identical
envelope. Product errors, nonzero exits, explicit rejection, accepted, unknown,
orphaned, superseded, or older attempts cannot be retried. A renewal after
expiry is a fresh envelope with current authorization and an explicit link to
the expired root.

## 7. Evidence boundaries

The following events remain distinct:

- journal append proves that local bytes and attributes were recorded;
- fresh discovery proves that a current transport address was listed;
- operator correlation maps that address to an intended human role and stable
  session UUID;
- a send result proves only that a product transport accepted a call;
- product delivery proves that content surfaced to a session;
- a complete reply validated against the exact root proves application
  handling and protocol correlation; and
- only receiver-owned policy or trusted operator evidence can authorize work.

A correlated but schema-incomplete callback may be recorded as handling
evidence. It is not a conforming CAM/1 lifecycle receipt. A result body may be
substantively useful while still failing protocol validation; preserve the
exact bytes and request a complete typed response when another exchange is
appropriate.

## 8. Interoperability incidents preserved in tests

### Validate preserved bytes, not reconstructed fields

One recipient manually retyped a valid UUID with an extra hexadecimal
character and falsely rejected the delivered envelope. Validation now runs
against the preserved serialization. Never repair or reconstruct an identifier
before testing it.

### Build complete replies

Multiple callbacks correlated by sender, recipient, request ID, and nonce but
omitted required fields, body digests, or legal receipt values. One result also
invented a field to answer multiple roots. The typed reply builders and strict
schema prevent abbreviated or ad hoc wrappers from being presented as
conforming. Each lifecycle reply correlates to one root request.

### Discovery is not identity

`ListAgents` alone supplied a name and short ref, not the operator-provided
session UUID or human role. Conversely, the UUID shown by `/status` was not the
literal MCP route. Fresh two-surface discovery plus operator correlation keeps
identity, human role, and route distinct.

Claude Code 2.1.251 field captures also showed that Agent View is not one row
per logical session: a background lifecycle row with `id`/`state` and an
interactive process row with `pid`/`status` can share a full `sessionId`. CAM
groups rows by that full UUID. When a process-backed representation exists it
must yield one eligible selected row; only when none exists may one eligible
legacy `id`/`state` row be used. CAM never fills a selected row from companion
evidence. An Agent View ID is validated when present and stays null when
absent, while the PID is transient selection/refresh evidence and is never
serialized or persisted. This behavior is covered by captured field evidence
and synthetic fixtures; no live 2.1.251 send or round trip has been claimed.

### A successful send does not keep correspondence alive

Messages accepted while the receiving session was busy did not always surface
until a later tool or turn boundary. By that time, short-lived messages could
be expired. The protocol preserves expiry because stale instructions must not
be revived. Senders record acceptance, yield, and use status inquiry or a fresh
authorized renewal rather than treating silence as permission to retry.

### Return routes must be usable

Participants emitted callback addresses that the named receiver could not
use, including a writer-only Codex thread and product-specific addresses on the
wrong side. The current profile requires `reply_to` to identify the sender's
supported transport and stable full session UUID. Claude route resolution
happens at reply time through current discovery; mutable `name [ref]` values are
not persisted as callback identity.

The project-aware live adapter also requires the selected recipient and
claimed sender to match active bound roster entries by vendor, common name,
and full session UUID. A Codex binding confirms its queue route; a Claude
binding records the full UUID while a fresh `name [ref]` route is resolved by
explicit preflight and again whenever Claude is the current send target. This
is consistency checking, not cryptographic authentication.

### A commit name does not identify dirty validator bytes

Two validations described as using the same checkout revision produced
different callback verdicts because the later process loaded uncommitted
schema and validator changes. The journal's existing Git provenance described
the coordinated project, not the separate CAM checkout, so it could not
reconstruct which rules produced either verdict.

The reference tools now report a deterministic validation-profile digest on
successful and rejected verdicts and record it on inbound validation and
outbound-intent events. Source-control and runtime versions remain adjacent
evidence. Ordinary live sends refuse a dirty CAM checkout; an explicit
development override must repeat the exact digest and is journaled. The
current changes form one coherent release and must not be reduced to the six
semantic diagnostic codes observed in one comparison.

### Pipeline success is not validator success

The validator already returned exit status 2 for invalid input, but a command
that piped its output through another program and then used `&&` observed the
downstream program's successful status. It subsequently invoked native
`codex queue`, bypassing the project-aware adapter's final validation and
journal gate.

The first-contact runbook now forbids validation-to-send pipelines and direct
native transport calls in the audited workflow. Regression tests require exact
exit status 2 for invalid input and prove that schema- or semantic-invalid
envelopes reach neither product transport nor outbound intent. The validator
exit contract itself did not change.

### Claude callbacks are cross-vendor through the adapter

A `claude_send_message` callback does not mean that only another Claude Code
session can answer. A Codex participant invokes the CAM transport adapter,
which resolves the Claude sender's full session UUID through fresh Agent View
and `ListAgents` evidence before calling Claude's MCP `SendMessage`. The reverse
Claude-to-Codex-to-Claude walkthrough and project-aware round-trip test make
that bridge explicit. Filesystem inboxes and callback alternates were not
added.

## 9. Current primary references

- [Claude Code cross-session messaging](https://code.claude.com/docs/en/cross-session-messaging)
- [Claude Code as an MCP server](https://code.claude.com/docs/en/mcp#use-claude-code-as-an-mcp-server)
- [Official MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)

These links document vendor capabilities, not CAM/1 conformance. Re-check them
and the installed command surfaces before a release or interoperability claim.
