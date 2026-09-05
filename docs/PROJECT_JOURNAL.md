# Project binding, roster, and journal

> **Audience:** operators auditing CAM history and administrators managing
> roster or recovery state. This is optional reference material; new users
> should begin with [START HERE](../START_HERE.md).

The supported CAM/1 profile keeps one required local journal for each Git
project. The journal gives the operator a readable, chronological record of
messages and protocol events without turning that record into a delivery
service or a source of authority.

## Where project state lives

The normal interactive path starts the agent inside the target Git worktree and
uses the current-directory default:

```bash
"/CONFIRMED/CAM/REPO/.venv/bin/python" \
  "/CONFIRMED/CAM/REPO/tools/cam1_project.py" project init
"/CONFIRMED/CAM/REPO/.venv/bin/python" \
  "/CONFIRMED/CAM/REPO/tools/cam1_project.py" project status
```

From a different working directory or in automation, select the worktree
explicitly:

```bash
"/CONFIRMED/CAM/REPO/.venv/bin/python" \
  "/CONFIRMED/CAM/REPO/tools/cam1_project.py" \
  --project-root /absolute/path/to/target/project \
  project status
```

The target directory must have been initialized with `git init`, but it does
not need an initial commit. The normal `onboarding prepare` command performs
project initialization idempotently when no CAM binding exists.

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

Executable approvals live separately in the account-scoped, owner-private
`~/CAM/Approvals/product-executables-v1.jsonl` ledger. That ledger is reused by
projects under the same operating-system account. It is not part of this
project journal, participant roster, or Git worktree.

Do not copy the binding into the tracked worktree, commit the journal, or move
the journal into a temporary directory. A state-root override is intended for
tests and explicitly managed local installations; it must be an absolute,
owner-controlled private directory and must be supplied consistently to every
later command. The pointer and identity bind its canonical path. Copying the
project directory or choosing a different override fails rather than selecting
or forking another history.

Project initialization and onboarding create no files in the application
worktree. The project pointer stays under `<git-common-dir>/cam1/`, each
worktree identifier stays under its private `<git-dir>/cam1/`, and journal
state stays under `~/CAM/Journals/`. Appending a CAM journal event is not a Git
commit and does not stage, commit, or otherwise change application files.

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

Self-enrollment uses these journal event types:

- `state.participant.enrollment_proposed` records a pending, non-routable
  proposal and its exact digest. If the same session prepares changed values,
  the new event's `supersedes` field identifies every earlier pending proposal;
  there is no separate supersession event and no earlier record is rewritten.
- `state.participant.enrollment_confirmed` correlates direct operator
  confirmation to the exact proposal digest and atomically creates its roster
  participant and full-session binding. For a Codex participant, that same
  state transition creates and operator-correlates the matching `codex_queue`
  route. A Claude participant remains without a route until fresh Agent View
  and `ListAgents` discovery correlate one.
- `state.participant.metadata_updated` records an operator-confirmed change to
  descriptive display name, nullable role, or associated product executable.
  It does not change stable participant identity or the session binding.

These state events contain no CAM envelope bytes and grant no message or task
authority. The confirmation code is a short correlation value derived from the
proposal digest, not authentication or a signature.

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

Self-enrollment and ordinary live sends require a clean CAM checkout.
Self-enrollment has no dirty-source override. A development-only override for
other supported product operations must repeat the exact current profile
digest; recording it does not make the checkout clean or turn uncommitted
source into a reproducible release. Older
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

`state-current.json` is a disposable atomic projection of pending and confirmed
enrollment proposals, the participant roster, lifecycle state, and active
compatibility-gate state. It can be rebuilt from `journal.jsonl`; it must never
override or repair journal history. See
[Compatibility upgrades](COMPATIBILITY.md) for the staged plan, readiness, and
activation workflow.

### Conversation links

A fresh request sent with `--continues-message UUID` records an optional
`attributes.conversation_link` on its `message.outbound.intent`:

```json
{
  "format": "CAM-CONVERSATION/1",
  "conversation_id": "00000000-0000-4000-8000-000000000101",
  "parent_message_id": "00000000-0000-4000-8000-000000000102"
}
```

The adapter derives the first ID from earlier journal ancestry; the caller
supplies only the received parent's ID. It requires exact validated inbound
evidence, not merely an outbound intent or an unparsed observation. Each edge
must stay within the same two session endpoints and point backward in the
same project journal. Eligible retries copy the original link unchanged.

This optional audit attribute needs no compatibility gate. Older journal
readers may ignore it; missing or null links remain ordinary ungrouped history.
It does not change the wire envelope, hash calculation, lifecycle projection,
authority checks, or `CAM-CAUSAL/1` context. Generic journal verification checks
record integrity, not this link's semantic ancestry; the send adapter checks
that ancestry before extending it. See [Continuing
collaboration](CONTINUING_COLLABORATION.md#linking-follow-up-questions).

### Inspect lifecycle state

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
- **role**: optional, mutable descriptive project responsibility; it is not
  identity or authority;
- **vendor**: `codex` or `claude-code`;
- **full session ID**: the stable product session UUID correlated by the
  operator;
- **session label**: the current human-readable product conversation or
  session name, which may change;
- **associated product executable**: the absolute path associated with this
  participant after operator review; it is runtime metadata, not participant
  identity. A separate active account approval for the same unchanged path and
  fingerprint is still required before product I/O; and
- **route observation**: a fresh, tool-derived transient transport address and
  the evidence used to observe it. Claude observations include the Agent View
  session kind and start time, the optional validated Agent View ID (null when
  absent), and the resolved Git worktree/common-directory context. They contain
  only the selected representation's evidence: a process ID and companion-row
  fields are never persisted. Raw peer sockets are also excluded.

Never use a display name, session label, short reference, working directory,
or Unix-domain socket as stable identity. Never store the Claude peer UDS in
the roster.

A pending enrollment proposal is not a participant-roster entry. It cannot be
used for routing, sending, callbacks, or authority. The identity card shows the
stable session and project evidence needed for human review, but deliberately
omits transient MCP refs, process IDs, and Unix-domain sockets.

A pending proposal is also not a common-name reservation. Multiple unconfirmed
sessions may propose the same name. Confirmation checks uniqueness against the
roster atomically; the first confirmed participant wins, and a conflicting
confirmation appends nothing and leaves that proposal pending. The losing
session must prepare, display, and receive direct confirmation for a fresh card;
the implementation must not silently rename a previously confirmed proposal.

For normal first contact, each product session inspects and proposes itself.
Before doing so, it runs the non-executing `product-discover` command. If the
exact canonical path and fingerprint lack an active account approval, the
session shows the returned candidate card and waits for direct operator
approval before running its guarded `product-approve` command. Only
`product-discover` may consult `PATH`; onboarding receives the approved absolute
path explicitly.

Codex uses trusted `CODEX_THREAD_ID` session metadata when available. Claude
Code uses trusted `CLAUDE_CODE_SESSION_ID` metadata when available and
correlates that full UUID to Agent View. If either full UUID is unavailable to
the running agent, the agent asks the operator for the full current session
UUID and passes it through `--session-id`; it never guesses from a product
name, cwd, short reference, or socket. Claude uses Agent View cwd to prove
project membership, but the exact cwd is not persisted as stable identity.

After enrollment, before every Claude send, the helper independently checks
the live cwd and performs both forms of current discovery:

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
The `name [ref]` value is only the route for that send. The short ref is not
normally visible in Claude `/status`, so it is not a value the operator must
recognize or approve. If the already bound UUID and project context map
uniquely through both discovery surfaces, CAM automatically records the exact
route observation in the journal and may use it. A changed ref alone does not
require another operator confirmation.

If discovery is missing, has multiple process-backed or eligible fallback
representations, is ambiguous, nonlocal, stale, or inconsistent, stop; do not
guess or silently retarget. Request operator help when the ambiguity concerns
the stable mapping, the UUID or project mismatches, the binding generation
changed, or evidence conflicts, including unexpected product session-label or
session-kind drift. Do not substitute a request to approve an unobservable
short ref.

For Codex, the full thread UUID is both the stable session identity and the
`codex queue` address. A Codex session should obtain that UUID from trusted
session metadata or direct operator input in that session, never from a shell
variable expected to expand in another session.

Prepare one self-enrollment card from each actual product session:

```bash
.venv/bin/python tools/cam1_project.py \
  --project-root /absolute/path/to/target/project \
  onboarding prepare \
  --vendor codex \
  --product-bin /account/approved/absolute/path/to/codex

.venv/bin/python tools/cam1_project.py \
  --project-root /absolute/path/to/target/project \
  onboarding prepare \
  --vendor claude-code \
  --product-bin /account/approved/absolute/path/to/claude
```

For an already initialized project, replace `prepare` with `inspect-self` to
perform the same local inspection without appending a proposal. Inspection does
not enroll or authorize the session.

`prepare` initializes the CAM project if needed, inspects the current session
through the explicitly supplied approved product executable, appends a new
`state.participant.enrollment_proposed` event or reuses the identical pending
proposal without another append, and returns one identity card. It does not
send a message or create a routable participant. Optional overrides are
`--common-name`, `--display-name`, `--role`, `--session-id`,
`--session-label`, `--session-kind`, and `--product-bin`. Use `--session-id`
only for the current full UUID when trusted session metadata is unavailable;
the command rejects a value that conflicts with available session metadata.

After reviewing the complete card, the operator returns its exact confirmation
response directly in that same session. The session then supplies the card's
exact values to:

```bash
.venv/bin/python tools/cam1_project.py \
  --project-root /absolute/path/to/target/project \
  onboarding confirm \
  --proposal-id PROPOSAL_UUID_FROM_CARD \
  --confirmation-code CONFIRMATION_CODE_FROM_CARD \
  --operator-reference "Direct confirmation of this exact card in this session"
```

The command rechecks the current session, project, executable, and CAM source
against the proposal before it appends
`state.participant.enrollment_confirmed`. A mismatch fails as stale rather than
silently changing the card. Repeating confirmation of the same exact proposal
is idempotent. The tool cannot authenticate the surrounding conversation; the
same-session direct-confirmation requirement remains an operator and agent
policy boundary.

Inspect proposals and the roster with:

```bash
.venv/bin/python tools/cam1_project.py \
  --project-root /absolute/path/to/target/project \
  onboarding status

.venv/bin/python tools/cam1_project.py \
  --project-root /absolute/path/to/target/project \
  onboarding status --show-identifiers
```

The default output redacts capability-like identifiers. Use
`--show-identifiers` only for an explicit local review. A confirmed Codex
enrollment records its UUID as the operator-correlated `codex_queue` route. A
confirmed Claude enrollment deliberately does not guess a live route. Run
project-aware `claude-preflight --participant COMMON_NAME_FROM_ROSTER`. When
its fresh Agent View and `ListAgents` evidence uniquely correlate the bound
UUID and CAM project to one eligible same-host peer, including a fresh live-cwd
project check, the project-aware path records the tool-derived route
observation automatically. No human confirmation of the MCP short ref is
required.

The following command is retained only for compatibility with older
project-state snapshots and explicit migration or diagnostic procedures. It is
not a normal onboarding step, and its operator reference must cite the stable
identity decision separately from the tool-derived route observation; it must
not claim that the operator recognized an MCP ref that `/status` did not show:

```bash
.venv/bin/python tools/cam1_project.py \
  --project-root /absolute/path/to/target/project \
  participant confirm-route \
  --participant reviewer \
  --expected-address "EXACT FRESH NAME [REF]" \
  --operator-reference "STABLE IDENTITY CONFIRMATION PLUS PREFLIGHT EVIDENCE"
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

Update mutable display, role, or associated-executable metadata only after direct
operator confirmation and with the currently displayed metadata revision:

```bash
.venv/bin/python tools/cam1_project.py \
  --project-root /absolute/path/to/target/project \
  participant update-metadata \
  --participant COMMON_NAME \
  --expected-revision CURRENT_REVISION \
  --display-name "Updated display name" \
  --role "Optional descriptive role" \
  --product-bin /operator/reviewed/absolute/product/path \
  --operator-reference "How the operator confirmed these exact changes"
```

At least one metadata change is required. `--clear-role` removes the role, and
`--clear-product-bin` removes the stored executable; each is mutually exclusive
with its corresponding value option. A successful change appends
`state.participant.metadata_updated`, increments `metadata_revision`, and
preserves participant identity, session binding, and route. A stale
`--expected-revision` fails instead of overwriting concurrent changes.

Historical participants may replay with
`approved_product_executable: null`. Such an entry is retained for audit but is
not live-ready. First obtain or verify the account approval for one absolute
candidate, then record that same path with the `participant update-metadata
--product-bin` command above. Do not re-add or rebind the participant and do not
rewrite prior journal events. Project-aware Claude preflight/send and Codex send
require both the unchanged account approval and an exact match to this recorded
path before product I/O. Clearing the roster path intentionally disables live
transport for that participant without revoking the separate account approval.

Use `participant invalidate` when a binding or route becomes questionable and
`participant retire` when its project role ends. Both append history; neither
deletes prior records or permits reuse of a retired common name. Fresh route
discovery cannot reactivate a stale participant; direct operator review and an
explicit `participant bind` event are required before live transport resumes.

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

For direct binary stdin capture, use `--stdin --capture-to ABSOLUTE_NEW_FILE`.
If that channel cannot close stdin, add `--stdin-byte-count N` using the known
complete UTF-8 payload length. The capture preserves exactly those bytes,
creates an exclusive mode-`0600` file, and then ingests it. A short EOF creates
neither a file nor an observation. This frames the local capture only; it does
not change the CAM envelope or remove transport-visible whitespace.

When the optional `causal.ordering/1` compatibility gate is active, the
project-aware sender derives non-wire `CAM-CAUSAL/1` context from this same
journal. Ingest holds a post-activation request or cancel that omits potentially
dispatched recipient work, records `held_for_clarification`, returns exit status
`4`, and leaves lifecycle state uncommitted. The record shows shared-journal
ordering only; it does not prove that an agent read or understood a message and
does not constrain unrelated work. See [causal ordering](CAUSAL_ORDERING.md).

## Privacy, retention, and moderation

The journal contains message bodies and capability-like routing metadata. Keep
it private to the operating-system account, exclude secrets from messages, and
apply the operator's retention and backup policy. Ordinary filesystem deletion
is not guaranteed secure erasure, and product transcripts or queues may retain
additional copies. The tools do not automatically delete envelope working
files or journal records.

The separate account approval ledger contains local executable paths,
fingerprints, and operator references. Protect and retain it as local policy
state, but do not confuse it with the per-project conversation history.

A future read-only moderator may watch journal appends and alert the operator.
That facility is deliberately deferred. It must not become a daemon required
for delivery, an automatic executor, a source of authority, or a way to widen
CAM/1 beyond the same host and operating-system account.
