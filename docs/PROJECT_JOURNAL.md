# Project binding, roster, and journal

The supported CAM/1 profile keeps one required local journal for each Git
project. The journal gives the operator a readable, chronological record of
messages and protocol events without turning that record into a delivery
service or a source of authority.

## Where project state lives

Run the project commands from the CAM/1 checkout and identify any worktree of
the target Git project explicitly:

```bash
.venv/bin/python tools/cam1_project.py \
  --project-root /absolute/path/to/target/project \
  project init

.venv/bin/python tools/cam1_project.py \
  --project-root /absolute/path/to/target/project \
  project status
```

Initialization creates two private bindings:

- `<git-common-dir>/cam1/project.json` points to the project's external state
  directory. It lives in Git's private administrative area and is not a
  tracked repository file.
- `<git-dir>/cam1/worktree-id` identifies the current worktree. Linked
  worktrees share the project but have distinct worktree IDs.

The required external state directory is:

```text
~/CAM/Journals/<project-slug>--<project-uuid>/
```

The project display name is the repository directory name at initialization.
It names the project, not an agent. The UUID prevents two repositories with the
same display name from colliding.

The reference tools create `~/CAM`, `~/CAM/Journals`, and the project directory
with mode `0700`. They create the binding, identity, journal, lock, and current
projection files with mode `0600`. They resolve the account home from the
operating-system account record rather than trusting the `HOME` environment
variable.

Do not copy the binding into the tracked worktree, commit the journal, or move
the journal into a temporary directory. A state-root override is intended for
tests and explicitly managed local installations; it must be an absolute,
owner-controlled private directory and must be supplied consistently to every
later command. The pointer and identity bind its canonical path. Copying the
project directory or choosing a different override fails rather than selecting
or forking another history.

## What the journal records

`journal.jsonl` is the source of truth for project-local CAM state. Each line is
one canonical `CAM-JOURNAL/1` record containing:

- a monotonically increasing sequence number and unique record ID;
- the project UUID and UTC recording time;
- an event type;
- the SHA-256 digest of the previous record;
- optional exact message bytes, stored as base64 with a byte count and digest;
- automatically captured worktree and Git provenance, including worktree ID,
  HEAD, tree, branch, and dirty state;
- bounded event attributes; and
- a digest of the complete record.

The hash chain detects editing, deletion, insertion, and reordering when a
later record is available. It does not prevent the same operating-system user,
a compromised local process, or an administrator from replacing the complete
chain. It is useful audit history, not cryptographic authorship,
non-repudiation, or independent tamper-proof evidence.

Records are append-only. Correct a mistake by appending a correction or
superseding event; never edit or delete the original line. The implementation
verifies the complete chain when a mutation transaction first reads it,
reopens and revalidates the locked file identity before each later operation in
that transaction, and advances the verified chain only from the exact record
bytes it appends. A new transaction performs a new complete verification. The
implementation fails closed on malformed, partial, substituted, or altered
history.

If a crash or storage failure leaves exactly one incomplete EOF record after a
fully verified prefix, the operator can inspect the bounded recovery evidence:

```bash
.venv/bin/python tools/cam1_project.py \
  --project-root /absolute/path/to/target/project \
  journal recovery-status
```

After independently confirming the reported full journal SHA-256 and project
UUID, the operator may recover that one damage class explicitly:

```bash
.venv/bin/python tools/cam1_project.py \
  --project-root /absolute/path/to/target/project \
  journal recover-partial-tail \
  --expected-journal-sha256 FULL_REPORTED_SHA256 \
  --confirm-project-id PROJECT_UUID \
  --reason "Power loss left an incomplete final record" \
  --operator-reference "How the operator confirmed this recovery"
```

The command first saves every damaged byte in a new mode-`0600` file beneath
the project's external `recovery/` directory. It then atomically installs the
unchanged verified prefix plus a hash-chained recovery record that identifies
the archive and discarded partial suffix. It never deletes the archive. It
refuses a clean journal and any complete record with invalid JSON, schema,
sequence, chain, or digest. Do not edit or remove a partial tail manually.

The current reference implementation accepts at most 100,000 records and 128
MiB of journal bytes. It fails closed at either limit and does not yet provide
rollover, compaction, or archival continuation. Those are implementation
capacity limits, not permission to trim or replace history. Plan retention and
export before a project approaches them.

Project mutations use short, bounded transactions. A live send records and
commits its intent, releases the project lock while the product transport is
running, then reacquires the lock to record the outcome and lifecycle update.
The journal lock therefore serializes state changes; it is not held across
Claude MCP or Codex queue I/O.

Provenance identifies the source state in which an event was recorded. It does
not assert that the tree was reviewed, that a dirty worktree's uncommitted
contents are recoverable, or that a reported claim is correct.

That record-level provenance describes the coordinated project. It does not
identify the separate CAM checkout whose code validated or dispatched a
message. New reference-tool inbound validation, rejection, duplicate, and
outbound-intent events therefore include a `validation_profile` attribute. It
contains a content digest for the reference tools, schemas, and runtime
requirements plus separate CAM source-control and runtime metadata. Outbound
intent also records `dirty_validator_override`.

Ordinary live sends require a clean CAM checkout. A development-only override
must repeat the exact current profile digest; recording it does not make the
checkout clean or turn uncommitted source into a reproducible release. Older
journal records without validation-profile attributes remain valid history.
Live source evidence also requires a concrete HEAD, matching regular profile
path sets, exact byte comparison, and unconcealed index state. The override may
cover ordinary tracked byte changes but not the other requirements.

HEAD, branch, and dirty state come from one Git status snapshot, while the tree
is derived from that snapshot's immutable HEAD object ID. Working files are not
locked, so dirty state is a point-in-time, best-effort observation.

The tooling invokes the project's bound absolute Git executable with a minimal
noninteractive environment, optional locks disabled, repository-configured
hooks and filesystem monitors disabled, and submodules ignored for status.
This keeps project discovery and provenance read-only without treating the Git
repository or same-user executable path as a security boundary.

`state-current.json` is a disposable atomic projection of the participant
roster and lifecycle state. It can be rebuilt from `journal.jsonl`; it must
never override or repair journal history.

`state status` is a non-mutating replay view. It does not append the aging
events that a later state mutation records, so a pending or held item whose
wall-clock deadline has just passed can remain displayed in its last recorded
state until the next mutation. Compare its recorded expiry when making an
operator decision; the send and ingest paths perform their own current-time
checks before committing protocol state.

Inspect or rebuild it from the canonical journal with:

```bash
.venv/bin/python tools/cam1_project.py \
  --project-root /absolute/path/to/target/project \
  state status

.venv/bin/python tools/cam1_project.py \
  --project-root /absolute/path/to/target/project \
  state rebuild
```

## Participant roster

The project roster is the project's address book. A participant entry keeps
these concepts separate:

- **common name**: the stable project-local name humans and agents use, such as
  `reviewer`;
- **display name**: the human-readable project label for the participant;
- **role**: the participant's stated project responsibility;
- **vendor**: `codex` or `claude-code`;
- **full session ID**: the stable product session UUID correlated by the
  operator;
- **session label**: the current human-readable product conversation or
  session name, which may change; and
- **route observation**: a fresh, transient transport address and the evidence
  used to observe it. Claude observations include the Agent View session kind
  and start time, the optional validated Agent View ID (null when absent), and
  the resolved Git worktree/common-directory context. They contain only the
  selected representation's evidence: a process ID and companion-row fields
  are never persisted. Raw peer sockets are also excluded.

Never use a display name, session label, short reference, working directory,
or Unix-domain socket as stable identity. Never store the Claude peer UDS in
the roster.

For Claude Code, the operator supplies the full session UUID shown by the
target session's `/status`. Before every send, the helper performs both forms
of current discovery:

1. `claude agents --json` groups representations by exact full `sessionId`.
   The same UUID can have a background `id`/`state` row and an interactive
   `pid`/`status` row. When a process-backed row exists, the helper requires
   one eligible such row; only when none exists may one eligible legacy
   `id`/`state` row be selected. It never merges companion fields.
2. A selected Agent View `id` is validated against the UUID when present and
   remains null when absent. A PID is transient selection and refresh evidence
   and is never serialized or recorded.
3. MCP `ListAgents` supplies the current addressable `name [ref]` route.
   Locality is independent of activity: local `busy` peers remain addressable;
   local terminal or unknown states are unavailable; cloud and Remote Control
   rows remain excluded as nonlocal.
4. The helper requires a unique name correlation between those fresh results
   and verifies that Agent View cwd resolves inside the bound Git project,
   including an initialized linked worktree sharing its Git common directory.

The full session UUID remains the identity and the envelope callback address.
The `name [ref]` value is only the route for that send. If discovery is missing,
has multiple process-backed or eligible fallback representations, is
ambiguous, nonlocal, stale, or inconsistent, stop and obtain operator
correlation; do not guess or silently retarget.

For Codex, the full thread UUID is both the stable session identity and the
`codex queue` address. A Codex session should receive that literal UUID from the
operator or its trusted session metadata, not from a shell variable that is
expected to expand in another session.

Create and bind participants from the CAM/1 checkout. For example:

```bash
.venv/bin/python tools/cam1_project.py \
  --project-root /absolute/path/to/target/project \
  participant add \
  --common-name coordinator \
  --display-name "Project coordinator" \
  --role "coordination" \
  --vendor codex

.venv/bin/python tools/cam1_project.py \
  --project-root /absolute/path/to/target/project \
  participant bind \
  --participant coordinator \
  --session-id "FULL CODEX THREAD UUID" \
  --session-label "CURRENT CODEX SESSION LABEL" \
  --session-kind interactive \
  --operator-reference "HOW THE OPERATOR CONFIRMED THIS SESSION"

.venv/bin/python tools/cam1_project.py \
  --project-root /absolute/path/to/target/project \
  participant add \
  --common-name reviewer \
  --display-name "Example reviewer" \
  --role "code review" \
  --vendor claude-code

.venv/bin/python tools/cam1_project.py \
  --project-root /absolute/path/to/target/project \
  participant bind \
  --participant reviewer \
  --session-id "FULL CLAUDE SESSION UUID" \
  --session-label "CURRENT CLAUDE SESSION NAME" \
  --session-kind interactive \
  --operator-reference "HOW THE OPERATOR CONFIRMED /status"
```

Binding a Codex participant also records its UUID as the operator-correlated
`codex_queue` route. A Claude binding deliberately does not guess a live route.
Run project-aware `claude-preflight --participant reviewer`, inspect its
fresh Agent View and `ListAgents` correlation, then explicitly record the exact
returned route:

```bash
.venv/bin/python tools/cam1_project.py \
  --project-root /absolute/path/to/target/project \
  participant confirm-route \
  --participant reviewer \
  --expected-address "EXACT FRESH NAME [REF]" \
  --operator-reference "HOW THE OPERATOR CORRELATED THIS ROUTE"
```

Routine roster output redacts identifiers. Reveal them only for an explicit
local operator check:

```bash
.venv/bin/python tools/cam1_project.py \
  --project-root /absolute/path/to/target/project \
  participant list

.venv/bin/python tools/cam1_project.py \
  --project-root /absolute/path/to/target/project \
  participant list --show-identifiers
```

Use `participant invalidate` when a binding or route becomes questionable and
`participant retire` when its project role ends. Both append history; neither
deletes prior records or permits reuse of a retired common name.

Every audited live send resolves two roster entries: the selected recipient
and the envelope's claimed sender. Each must be active and bound, and its
vendor, project-local common name, and stable full session UUID must match the
envelope exactly. The sender's non-null `reply_to` must also match that bound
participant's supported return transport and stable UUID. A one-way envelope
can remain valid CAM/1 wire data, but the reference live path refuses it.

## Inspecting the record

Verify the complete chain and inspect a redacted recent summary with:

```bash
.venv/bin/python tools/cam1_project.py \
  --project-root /absolute/path/to/target/project \
  journal verify
.venv/bin/python tools/cam1_project.py \
  --project-root /absolute/path/to/target/project \
  journal tail --limit 20
```

The default tail output redacts exact message content and event attributes so a
routine status check does not spill routing metadata or message bodies into a
transcript. For an explicit local operator review, use:

```bash
.venv/bin/python tools/cam1_project.py \
  --project-root /absolute/path/to/target/project \
  journal tail \
  --limit 20 \
  --show-content
```

The command verifies the chain first. It decodes valid UTF-8 JSON messages for
review and uses bounded base64 and metadata for malformed bytes that cannot be
rendered safely. Full output may contain message bodies and routing metadata;
do not paste it into a public issue or unrelated transcript. The
owner-controlled `journal.jsonl` file remains the complete local audit record.

`journal append` is a low-level diagnostic and integration command. Normal
message tools are responsible for recording their own protocol and transport
events; users should not have to reconstruct routine events manually.

The outbound intent/outcome pair also constrains retries. The reference
adapter accepts `--retry-after-intent` only for the latest exact intent whose
outcome proves dispatch was not attempted. Product errors, nonzero exits,
rejection, accepted, unknown, orphaned, superseded, and older attempts are
non-retriable. A retry preserves the identical envelope; a post-expiry renewal
is a new envelope linked with `--renewal-of`.

## Event and delivery boundaries

The journal is neither an inbox nor a message board. Appending a record does not
deliver a message, wake a session, prove product acceptance, establish that a
recipient handled content, or authorize work. Keep the following observations
separate:

1. **journaled intent**: exact outbound bytes and the planned target were
   recorded before a send attempt;
2. **transport acceptance**: Claude `SendMessage` or Codex `queue` accepted the
   payload;
3. **product delivery**: the receiving product surfaced the message to its
   session;
4. **application receipt**: a correlated CAM/1 reply reports receipt or a
   lifecycle state; and
5. **completion evidence**: a correlated result reports an outcome, still
   subject to evidence review.

On receipt, append the complete product-visible envelope serialization before
validation. This preserves a malformed or expired message for audit without
treating it as valid; it does not claim access to hidden transport framing.
Then validate, correlate, update lifecycle state, and decide whether
receiver-owned policy permits any action. A failed validation or unavailable
journal must fail closed; it must not be bypassed by acting first and recording
later.

The supported receive-side command performs that ordering in one transaction:

```bash
.venv/bin/python tools/cam1_project.py \
  --project-root /absolute/path/to/target/project \
  message ingest \
  --message /absolute/path/to/exact-delivered-envelope.cam1.json \
  --as-participant receiver-common-name
```

It records an observation before attempting validation, then appends a
separate validated or rejected event. A malformed, expired, conflicting, or
illegal envelope therefore returns nonzero and appends a rejection without
disappearing from the audit history. `message ingest`
does not read a product inbox, send a reply, authorize the body, or execute the
requested action. After preserving the bytes, it also rejects a recipient
vendor, common name, or full session UUID that does not match the named active
bound participant, or a claimed sender that does not match exactly one active
bound participant. Use `--renewal-of PRIOR_ROOT_UUID` only for an explicit
typed renewal of the same semantic operation.

A successful ingest reports `status: validated`, lifecycle state,
`authorization_evaluated: false`, and `action_authorized: false`. Validation is
not a recipient acceptance decision and never authorizes the message body.

## Privacy, retention, and moderation

The journal contains message bodies and capability-like routing metadata. Keep
it private to the operating-system account, exclude secrets from messages, and
apply the operator's retention and backup policy. Ordinary filesystem deletion
is not guaranteed secure erasure, and product transcripts or queues may retain
additional copies. The tools do not automatically delete envelope working
files or journal records.

A future read-only moderator may watch journal appends and alert the operator.
That facility is deliberately deferred. It must not become a daemon required
for delivery, an automatic executor, a source of authority, or a way to widen
CAM/1 beyond the same host and operating-system account.
