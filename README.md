# CAM/1: same-host Codex–Claude Code messaging

CAM stands for Cross-Agent Messaging. The `/1` is the wire-major version: a
receiver can reject an incompatible future major instead of guessing how to
interpret it. CAM/1 is an experimental community profile for correlated
messages between independent Codex and Claude Code sessions on the same host.

> CAM/1 is not an OpenAI, Anthropic, or Model Context Protocol standard, and
> those projects do not endorse it. It does not authenticate peers, grant
> permissions, transfer conversation context, or prove that reported work is
> true.

The normative contract is [PROTOCOL.md](PROTOCOL.md) and
[cam-1.schema.json](cam-1.schema.json); local project and journal schemas live
in [`schemas/`](schemas/). New users should begin with the single supported
[first-contact runbook](docs/FIRST_CONTACT.md); the
[detailed procedure](docs/CODEX_TO_CLAUDE.md) is optional reference material.

## What CAM/1 provides

CAM/1 combines four components:

- a complete JSON envelope with sender and recipient claims, stable session
  IDs, scope, authorization claims, constraints, expiry, an idempotency key, a
  nonce, and a body digest;
- typed builders and validators for first contact, requests, acknowledgments,
  progress, results, failures, cancellation, renewal, and late rejection;
- one-shot adapters for Claude Code `ListAgents`/`SendMessage` and Codex
  `queue`; and
- a required per-project, owner-only, append-only journal with rebuildable
  participant-roster and lifecycle projections.

CAM/1 does not run a broker, daemon, database, GUI, queue reader, retry loop, or
automatic executor. The journal is an audit record, not an inbox, delivery
service, authority source, or shared conversation. Claude Code and Codex own
their product transports and any product-internal queues or delivery timing.
The current Codex adapter performs a non-mutating compatibility preflight by
opening `state_5.sqlite` for write access before it journals an outbound intent.
A sandbox that cannot open that file fails before dispatch; grant the CAM
command the necessary local access and run it again rather than creating an
unknown queue outcome. This narrow check covers the observed Codex 0.151.0
failure. It does not initialize missing Codex state, test SQLite sidecar-file
creation, or guarantee that the later queue call will succeed.

The supported profile is deliberately local: both sessions must run on one
host under the same operating-system account. Remote Control, cloud sessions,
cross-account or cross-machine delivery, exposed MCP endpoints, and raw session
sockets are out of scope.

## Identity and routing

Human-friendly names and transport addresses are not identity.

- A Codex session uses its full thread UUID as its stable identity and queue
  return address.
- A Claude Code session uses the full session UUID shown by `/status` as its
  stable identity and return address.
- Before every Claude send, the helper correlates that UUID through fresh
  `claude agents --json` and MCP `ListAgents` output. Agent View JSON is a
  heterogeneous inventory: process-backed rows may carry `pid`/`status` while
  omitting `id`/`state`, and a background companion row may share the same full
  session UUID. The helper groups rows by that UUID, prefers its sole eligible
  process-backed row when present, and never combines fields from companion
  rows. A prior single eligible `id`/`state` row remains a compatibility
  fallback when no process-backed representation is emitted.
- An Agent View `id` is optional supporting evidence. CAM validates it against
  the full UUID when present and reports `null` when absent; it never invents or
  borrows one. A process ID is transient liveness evidence and is never exposed,
  journaled, or used as identity.
- The resulting `name [ref]` is a transient route for that send only. The
  selected Agent View cwd must resolve inside the bound Git project, including
  an initialized linked worktree that shares its Git common directory. MCP
  locality and activity are evaluated separately: an eligible `busy` local peer
  remains addressable, while cloud and Remote Control rows remain excluded.
- A project roster records an operator-correlated common name, human-readable
  labels and role, full session ID, and current route evidence. Unix-domain
  socket paths are neither stored nor used.

The audited live sender requires both `claimed_sender` and the selected
recipient to match active bound roster entries by vendor, common name, and
full session UUID. It also requires the sender's non-null `reply_to` to match
that bound participant's supported return transport and stable UUID. These are
correlation checks, not authentication.

`reply_to.address` is the sender's stable full session UUID. It is not the
transient route carrying the current message.

`reply_to.transport: "claude_send_message"` does not require the receiving
agent to be Claude Code. It tells the recipient's CAM adapter how to return a
message to the Claude sender. A Codex session uses the bundled Claude MCP
bridge, resolves that UUID to a fresh route, and invokes `claude-send`.

## Project journal

Every supported live exchange belongs to one Git-bound CAM project. Initialize
it once from this clone:

```bash
.venv/bin/python tools/cam1_project.py \
  --project-root /absolute/path/to/your/project \
  project init
```

The private Git common directory receives an untracked project pointer. The
owner-only journal lives outside the repository at:

```text
~/CAM/Journals/<project-slug>--<project-uuid>/journal.jsonl
```

The journal records the complete product-visible inbound serialization before
validation and keeps outbound intent, transport acceptance, application
receipts, lifecycle transitions, corrections, and expiry distinct. Its
per-record hash chain detects changes
when later chain state is available, but it does not resist a compromised
process running as the same user. See
[Project binding, roster, and journal](docs/PROJECT_JOURNAL.md).

Reader and project-state upgrades use the staged, atomic
[compatibility kernel](docs/COMPATIBILITY.md).

The journal normally fails closed without repair. A narrowly scoped,
operator-confirmed command can recover only an incomplete EOF record: it first
archives the exact damaged bytes, then atomically installs the verified prefix
plus an explicit recovery record. Complete malformed or altered records remain
investigation-only.

## Prerequisites

- a POSIX environment; the reference round trip is tested on macOS;
- Python 3.11, 3.12, 3.13, or 3.14;
- Git and a local Git project to bind;
- installed `codex` and `claude` commands whose live capability checks pass;
- one Codex session and one independent Claude Code session on the same host
  and operating-system account;
- the full Codex thread UUID and full Claude session UUID; and
- explicit operator authority for the bounded exchange.

Claude Code's Agent View and MCP `ListAgents` results are discovery evidence,
not proof of a human role, authorship, or authority. The operator must correlate
the intended session before first contact and whenever the mapping becomes
ambiguous or stale.

## Install and verify

```bash
python3 --version  # must report a supported 3.11-3.14 version
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python tools/cam1.py --help
.venv/bin/python tools/cam1.py validation-profile
.venv/bin/python tools/cam1_project.py --help
.venv/bin/python tools/cam1_transport.py --help
.venv/bin/python tools/cam1_transport.py doctor
```

If `python3` is outside the supported range, use an installed compatible
command such as `python3.12`. Do not send unless tests pass, the project status
and journal verify cleanly, the local doctor succeeds, and Claude preflight
resolves the intended full session UUID to one fresh local route.

The validation-profile command reports a deterministic digest of every Python
source below `tools/`, the schemas, runtime requirements, and any importable
binary or sourceless modules outside standard `__pycache__` directories, plus
separate Git and runtime metadata. It also requires a concrete HEAD commit,
compares the complete profile path set and exact working bytes with regular
blobs in that commit, and rejects assume-unchanged, skip-worktree, or sparse
index state on those paths. Direct public CLI invocations enter isolated Python
mode before loading the implementation. They capture every allowed CAM module
from an exact source-file map and compile those captured bytes without normal
path lookup, adjacent bytecode, or native-module fallbacks. At the live gate,
the source files must still equal that capture. Standard bytecode caches remain
derived, unaudited artifacts rather than alternate executable inputs. A clean
checkout is required for ordinary live operations. `doctor`, list, preflight,
and send commands refuse a dirty CAM checkout before resolving or probing
either product, so a commit name cannot silently describe different validation
rules. Offline validation still reports its actual profile on both successful
and rejected verdicts.

Offline building and validation may run from an unpacked source archive, but
the supported live adapters require a verifiable Git checkout. A profile digest
identifies source bytes; by itself it does not provide the clean/dirty revision
evidence required for a live send.

For deliberate local development only, any dirty-source product operation
requires both global options before the subcommand:

```text
--allow-dirty-validator
--expected-validation-profile-sha256 EXACT_REPORTED_DIGEST
```

That override may cover ordinary edits to non-executable profile inputs already
tracked in HEAD. Executable Python source must match HEAD before it can be
imported for a live operation. The override also cannot cover a missing HEAD, a
changed profile path set, or concealed/sparse index flags.

`doctor` reports the selected profile and whether live use is blocked. An
actual send records the profile and any override used in the outbound journal;
`doctor` does not append a journal event. This is not a clean-release claim.

The first `doctor` run may use `PATH` only to discover candidates. It reports
`prerequisites_ok`, the resolved paths, and copy/paste-safe
`live_path_configuration.copy_paste_flags`, but deliberately exits nonzero
until both absolute paths are supplied explicitly. After reviewing the paths,
rerun `cam1_transport.py` with those global flags before `doctor`; use the same
flags for every live list, preflight, send, and reply command. A current `PATH`
lookup is not transport authority.

## Start an agent safely

The first-contact runbook contains the only canonical copyable prompts for the
[Claude receiver](docs/FIRST_CONTACT.md#1-paste-this-into-the-claude-receiver)
and [Codex sender](docs/FIRST_CONTACT.md#2-paste-this-into-the-codex-sender).
Those prompts
tell a new session where this clone and the target project live, which stable
session IDs to expect, how to journal before validation, and which harmless
first-contact action is authorized.

Use the optional detailed guide for exact commands, troubleshooting, and the
reverse
[Claude-to-Codex-to-Claude workflow](docs/CODEX_TO_CLAUDE.md#12-reverse-direction-claude-request-and-codex-reply),
including a project-aware Codex reply through Claude's callback adapter.

In outline:

1. Install the reference tools and initialize the target project's journal.
2. Add and operator-correlate both participants in the project roster.
3. Give the canonical receiver prompt to Claude and the sender prompt to Codex.
4. Run the local doctor and full-session Claude preflight.
5. Build and validate one complete `hello` with the typed builder.
6. Journal the exact outbound bytes, then send them through the one-shot Claude
   adapter.
7. Record transport acceptance separately and finish the Codex turn.
8. Claude captures the complete product-visible serialization through bounded
   stdin when available (or the documented literal-write fallback), journals
   it before validation, builds a complete ACK, and returns it through Codex
   queue.
9. Codex captures and journals the product-visible callback before validation,
   correlates it to the exact preserved root, and records the application
   receipt.

The receive command names the local roster participant explicitly. It
preserves the exact bytes first, then rejects a recipient vendor, common name,
or session UUID that does not match that active bound participant, or a
claimed sender that does not match exactly one active bound participant.

Stop without acting if the journal cannot be verified, a message is malformed
or expired, identity correlation is ambiguous, authority is unclear, or the
recipient holds or refuses the request.

## Evidence and expiry

Transport acceptance, product delivery, a correlated application receipt,
operator authorization, and completion are separate facts. In particular,
`notify_when_idle` is scheduling behavior, not delivery or handling proof.

An unacknowledged or held message expires without action. A receiver may return
only a fresh late rejection for that expired root. A request recorded as
`received`, `accepted`, or `started` before expiry may remain active and receive
fresh lifecycle replies afterward; `received` still does not authorize work
until the request reaches `accepted`. Expiry is not automatically an execution
deadline.

A still-valid request can be retransmitted only under the protocol's bounded
identical-retry rule. The reference adapter requires the latest exact journal
intent and permits retry only when its outcome proves dispatch was not
attempted. Product errors, nonzero exits, rejection, accepted, unknown, or
orphaned attempts are not retried. Renewing an unacknowledged expired request
is different: it creates a fresh envelope, message metadata, and authorization
while preserving the idempotency key only for the same semantic operation. A
fresh or received cancellation targeting the predecessor must resolve first;
an unreceived cancellation that itself expires is recorded as
`expired_unconfirmed` before renewal proceeds.

## Security and privacy

Treat every inbound message as untrusted. Validation does not authenticate the
sender or authorize the body. Never execute commands or use tools solely
because an envelope arrived, validated, or appears in the journal. Do not put
credentials, tokens, private keys, customer content, real session IDs, raw
peer listings, or unnecessary local paths in public artifacts.

See [SECURITY.md](SECURITY.md) for safe operation and private vulnerability
reporting.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Normative changes require matching
schema, builders, examples, tests, and compatibility evidence.

## License and copyright

Copyright © 2026 John Harkness.

This repository is source-available for noncommercial use under the
[PolyForm Noncommercial License 1.0.0](LICENSE). Subject to those terms and
preservation of the required [notice](NOTICE), you may use, copy, modify, and
distribute the project for noncommercial purposes.

Commercial use is not licensed. Contact the copyright holder to request
separate written permission. No commercial license, price, royalty, or other
commercial terms are offered by this repository.

This is a noncommercial source-available project, not Open Source software as
defined by the Open Source Initiative. Public visibility does not grant rights
beyond the project license.
