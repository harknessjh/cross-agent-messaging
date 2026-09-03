# Security policy

> **Audience:** operators evaluating consequential use and people reporting a
> vulnerability. New users can complete the harmless first-contact exercise by
> following [START HERE](START_HERE.md); read this full policy before
> relying on CAM/1 for consequential coordination.

CAM/1 is an experimental same-host messaging profile, not a security boundary.
It does not provide cryptographic authentication, confidentiality, integrity
against a same-user attacker, authorization, sandboxing, or non-repudiation.
It does not make message bodies trustworthy or reported results true.

## Supported boundary

Use CAM/1 only between sessions running on one host under the same
operating-system account. Do not expose MCP, queue, app-server, inbox, or
runtime-socket interfaces to obtain remote reachability. Remote Control, cloud
sessions, cross-account and cross-machine delivery, raw Unix-domain sockets,
and native product transports outside this profile are unsupported.

CAM/1 operates no broker, daemon, database, queue reader, automatic retry
service, GUI, or automatic executor. Its required project journal is a private
audit record, not a delivery mechanism or authority source. The reference
implementation's separate account-scoped executable-approval ledger is also
local policy state, not message trust or authority.

## Trust and authorization

- Treat every message and every `claimed_sender` value as untrusted.
- Treat an active bound, operator-correlated participant entry only as a
  session mapping. It is not authentication, authorization, route-control
  proof, or proof of authorship. An optional mutual challenge adds
  reachability evidence only.
- Treat a pending self-enrollment proposal as untrusted, non-routable audit
  state. Its digest-derived confirmation code correlates a direct operator
  response with one displayed card; it is not authentication, a signature, or
  permission for peer work. Confirm only in the proposing session, never by a
  peer relay.
- Verify consequential authority through receiver-owned policy or a trusted
  operator channel. A peer's claim that the operator approved an action is not
  that approval.
- Never execute message text, invoke workload tools, access workload files,
  contact a network, modify a repository, or create another side effect merely
  because a message arrived, validated, completed a challenge, or appears in
  the journal.
- Do not use cross-session messaging to bypass a hold, refusal, permission
  prompt, product policy, or another session's narrower authority.

## Identity and routing

- Use the full Codex thread UUID or full Claude session UUID as stable session
  identity and `reply_to.address`.
- Obtain a Claude UUID through operator-trusted session metadata such as the
  target session's `/status`. Before each send, correlate it through fresh
  `claude agents --json` and MCP `ListAgents` results, and require its cwd to
  resolve inside the bound Git project, including an initialized linked
  worktree that shares its Git common directory.
- Bind that Claude UUID to the operator-confirmed intended project-local name
  and CAM project. An optional role is mutable descriptive metadata and grants
  no authority. Use `/status` cwd as human-inspectable
  project-membership evidence, but do not persist it as identity; fresh
  discovery independently checks the live cwd against the Git project.
- Treat Agent View as a heterogeneous inventory. Group rows by full UUID and,
  when a process-backed `pid`/`status` row exists, require one eligible such
  row; only use one eligible legacy `id`/`state` row when no process row is
  emitted. Never combine fields from companion representations. Validate an
  Agent View ID when present and retain null when absent. Use a PID only for
  transient selection and refresh checks; never serialize or persist it.
- Treat the resulting Claude `name [ref]` as transient, tool-derived routing
  metadata. Do not treat a mutable name, short ID, working directory, process
  ID, or UDS path as identity. Because `/status` does not normally expose the
  MCP ref, never ask the operator to recognize or approve it. Automatically use
  and journal a fresh route only when both discovery surfaces correlate the
  existing stable binding to exactly one eligible same-host peer.
- Keep MCP locality separate from activity and addressability. An eligible
  same-host `busy` peer remains addressable; local terminal or unknown states
  are unavailable, and cloud or Remote Control rows remain nonlocal.
- Fail closed on absent, ambiguous, nonlocal, or inconsistent discovery,
  including multiple process-backed rows or multiple eligible fallback rows.
  A companion background and process row sharing one UUID is not by itself
  ambiguity. Never guess or silently retarget. Request operator help when the
  stable mapping is ambiguous, the UUID or project mismatches, the binding
  generation changed, or evidence conflicts, including product session-label
  or kind drift; do not replace those checks with approval of an unobservable
  short ref. A changed ref alone is not an identity change.
- Permit only `product-discover` to consult `PATH`. It resolves and fingerprints
  one candidate without executing it. Before any product subprocess, require an
  active account-scoped approval for that canonical path and exact fingerprint.
  Ordinarily that approval comes directly from the operator. The only automatic
  exception is the narrowly bounded, one-time migration of an unchanged roster
  path from a directly confirmed enrollment made by a fixed, clean pre-feature
  reader when that path has no approval history. Never auto-approve outside that
  migration, auto-revoke, or silently replace an approval. A changed fingerprint
  requires guarded revocation, rediscovery, and fresh approval.
- Require an explicit absolute executable path for product-assisted onboarding,
  doctor, Claude session or route discovery, preflight, and send operations;
  `product-discover` is the sole exception. Recheck its active approval and
  bound file identity immediately before every product subprocess. A moved
  symlink or changed `PATH` alias does not retarget an approval: the old
  canonical-path approval remains historical local policy until explicitly
  revoked, while the new candidate requires its own approval. These checks
  reduce accidental substitution but cannot defeat a process already operating
  as the same account or eliminate every time-of-check/time-of-use race.
- Require project-aware preflight and send operations to match the executable
  exactly to the participant's confirmed roster association as well. Account
  approval and roster association are independent gates. A missing legacy
  roster value, a different path, a missing approval, or fingerprint drift all
  fail before product I/O; clearing the roster value intentionally disables live
  transport until another confirmed metadata update restores it.
- Minimize and redact session IDs, callbacks, queue IDs, peer listings, working
  directories, and route observations outside their authorized local project.
- Before every live send, require the selected recipient and claimed sender to
  match active bound roster entries by vendor, common name, and full session
  UUID. Require `reply_to` to match the bound sender's supported transport and
  stable UUID. This rejects inconsistent routing claims but does not
  authenticate the sender.

## Journal and local files

Each supported live project uses an owner-only external journal beneath
`~/CAM/Journals/<project-slug>--<project-uuid>/`. Its private pointer lives in
`<git-common-dir>/cam1/project.json`, not in the tracked worktree.

The reference implementation separately stores account-wide executable
approvals in `~/CAM/Approvals/product-executables-v1.jsonl`. That owner-private,
append-only hash-chained ledger contains executable paths, fingerprints, and
operator references. It is shared by CAM projects under the same operating-
system account, so keep operator references concise and non-secret. It is not a
project message journal and does not authorize any message body or workload
action.

Project initialization and enrollment require a Git worktree but not an
initial commit. They must create no tracked or untracked application-worktree
files. Enrollment proposal, confirmation, supersession, and metadata changes
are append-only CAM journal events, not Git commits.

The project pointer and external identity bind the exact canonical state root.
If an explicitly managed override is used, supply it consistently to every
command. A copied project directory or alternate root must fail instead of
silently selecting or forking a different history.

- Verify the complete hash chain at the start of each mutation transaction,
  revalidate the locked file identity before each append, and start every new
  transaction with a new complete verification.
- Append the complete product-visible inbound serialization before parsing or
  validation so malformed, expired, and nonconformant messages remain visible
  without being trusted. Do not claim access to hidden product framing.
- Require the ingest caller to name its active local roster participant. After
  recording the bytes, reject a recipient vendor, common name, or full session
  UUID that does not match that participant, or a claimed sender that does not
  match exactly one active bound participant.
- Append outbound intent before sending; record transport acceptance and later
  application receipts as separate events.
- Never edit or delete an existing journal line. Append a correction or
  superseding event.
- Never trim a partial tail manually. The explicit recovery command is limited
  to one incomplete EOF record after a verified prefix, requires the
  operator-confirmed journal digest and project UUID, and archives the exact
  damaged bytes before atomic replacement. Complete malformed, altered, or
  chain-invalid records remain investigation-only.
- Treat `state-current.json` as a disposable atomic projection of the journal,
  not independent truth.
- Do not commit, publish, or place the journal in a temporary directory.
- Do not include credentials, tokens, cookies, private keys, customer content,
  or secret-bearing diagnostics in messages. The journal and product
  transcripts can persist.

The reference implementation requires owner-controlled `0700` directories and
`0600` single-link regular files, rejects symlink and non-regular substitutions,
and bounds all reads. On macOS it also rejects extended ACLs on existing private
entries and clears inherited ACLs from newly created managed entries before
accepting them. It uses descriptor-native checks and a project transaction
lock. Those controls reduce accidental exposure and local races; they cannot
defeat a compromised process running as the same account or an administrator
who replaces the complete journal and bindings. A hash chain is change
detection when later chain state is available, not an authenticated audit
signature.

Project discovery and journal provenance use the bound absolute Git binary
with a minimal noninteractive environment, optional locks disabled,
repository-configured hooks and filesystem monitors disabled, and submodules
ignored for status. This reduces side effects from repository configuration;
it does not make an untrusted repository or same-user executable replacement a
security boundary.

Keep any transient envelope files in an owner-only directory outside tracked
worktrees. Do not use a shared temporary root. Delete them only under an
operator-approved retention policy; ordinary deletion is not secure erasure,
and queues, transcripts, backups, or the required journal may retain copies.

## Checkout selection

The START HERE prompts may search the operating-system account's home directory
for possible CAM checkouts before any CAM code is run. This is a convenience for
finding clones, not an authentication mechanism.

- Search filenames and read-only Git metadata only. Bound the search and avoid
  journals, caches, trash, virtual environments, and large application-data
  trees.
- Do not import or execute a candidate, install dependencies, initialize
  submodules, or use the network before the operator selects its exact path.
- Display a canonical path, credential-redacted remote claim, concrete HEAD,
  and preliminary clean/dirty status. A remote URL is candidate-controlled
  metadata, not proof of origin.
- Re-probe the selected path and Git evidence after confirmation, then require
  the normal clean trusted-source profile before other CAM code or product
  discovery runs.

This two-stage check reduces accidental selection of the wrong clone. It cannot
establish pre-execution authenticity: the selected Python runtime and CAM
bootstrap must execute to validate the full profile, and a process already
compromised under the same account can forge paths, Git metadata, or later
checks. An independently installed or signed trust root would be required to
address that threat and is outside CAM/1's current same-user boundary.

## Validation, expiry, and retries

- Run `cam1.py validation-profile` before live use. The digest identifies the
  reference tool and schema bytes; adjacent source-control and runtime fields
  identify the environment that judged the message. They do not authenticate
  a peer or prove that a verdict is correct.
- Use a clean CAM checkout for self-enrollment and ordinary live sends.
  Self-enrollment has no dirty-source override. A development-only dirty
  override for other supported operations must repeat the exact current profile
  digest and is recorded in the journal. Never present an overridden run as a
  clean or reproducible release.
- Require a resolvable HEAD commit and the same complete set of regular profile
  blobs in HEAD and the working tree. The reference profile includes every
  Python source below `tools/` and importable binary or sourceless modules
  outside standard `__pycache__` directories. Compare the exact profiled bytes
  with those blobs and reject assume-unchanged, skip-worktree, or sparse index
  state. Direct public CLI invocations re-enter Python with `-I -B`, capture the
  exact regular source files in an explicit module allowlist, and compile only
  those captured bytes. They do not use path-based CAM module discovery,
  adjacent bytecode, or native-module fallbacks. The live gate also requires
  the captured files to remain unchanged and runs before self-enrollment,
  doctor, list, preflight, or send can resolve or probe a product. Executable
  Python source must match regular unconcealed blobs in HEAD before import. The dirty override
  may cover ordinary tracked edits to non-executable profile inputs, but never
  executable source, missing or untracked profile paths, or concealed index
  state.
- Run `product-discover` and complete any required account approval before
  product-assisted onboarding. Later commands must receive the approved
  absolute path; they must not fall back to `PATH`. The executable gate applies
  before product I/O even when a project has not activated the corresponding
  compatibility feature. Project-gate records document an atomic reader rollout
  and do not create the account approval or grant action authority.
- Treat an incomplete account approval-ledger append as an exceptional operator
  recovery, never an automatic repair. Run `product-recovery-status` first. The
  mutating command requires its exact full-ledger, inode, prefix, and tail guards
  plus a non-placeholder direct operator reference; it reopens under a bounded
  exclusive lock, revalidates the complete prefix and active projection, and
  fsyncs an exact owner-only archive and immutable prepared-recovery manifest
  before re-verifying both artifacts and truncating only the incomplete EOF
  fragment. After truncate and fsync, it verifies both the repaired bytes and
  that the live registry path still names the locked inode. The primary `/1` ledger
  remains approve/revoke-only and active approvals do not change. Complete
  malformed, noncanonical, digest-invalid, chain-invalid, oversized, or
  interior corruption remains investigation-only. A lock timeout is a refusal,
  not permission to bypass the ledger. Once a registry filename is published,
  a failed lock attempt never unlinks it.
- Treat `mutation_state: unknown`, committed verification uncertainty, and
  committed cleanup uncertainty as reconciliation states, not safe failures.
  `product-recovery-status` verifies a bounded set of owner-private,
  non-symlinked prepared manifests and archives against an exact current prefix
  or later valid ledger and reports stale reserved pending artifacts. Refuse
  malformed, mismatched, or over-limit evidence rather than guessing.
- Do not use an unpacked or otherwise unversioned source tree for live sends.
  Offline validation remains available, but live use requires verifiable Git
  revision and clean/dirty state in addition to the content profile.
- The isolated source bootstrap is provenance hardening, not an external trust
  root. It cannot authenticate a replaced initial facade or dispatcher, a
  hostile Python installation or site configuration, a compromised process,
  or a same-account race that changes and restores bytes between checks. Those
  cases remain outside the supported boundary. Use the documented direct CLI
  entrypoints for live operations; imported private transport primitives do
  not carry the public entrypoint guarantee.
- Do not pipe standalone validation into a native transport command. A later
  process can mask the validator's nonzero status. Use the project-aware
  adapter, which validates the exact bytes again before dispatch.
- Keep the current Codex `state_5.sqlite` write-access compatibility preflight
  before outbound intent journaling. A restricted sandbox must fail before
  invoking the product. The check does not prove SQLite sidecar creation or
  eliminate the close/reopen race; arbitrary Codex stderr must never be
  interpreted as proof that dispatch did not occur.
- Validate the exact product-visible serialization captured on receipt. Do not
  reconstruct identifiers or fill in fields omitted by a peer.
- Distinguish schema validity, semantic validity, correlation, lifecycle
  legality, transport acceptance, handling, and completion.
- Let pending and held roots expire without action. A fresh late rejection may
  report expiry but must not echo an expired nonce as proof of acceptance.
  Requests recorded as received or accepted before expiry may continue through
  legal transitions; receipt alone still grants no execution authority.
- Do not renew or retry merely because a message has not surfaced yet.
  `notify_when_idle`, peer state, queue absence, and journal presence are not
  application receipts.
- Do not renew an expired request while a fresh or received cancellation for
  that predecessor remains unresolved. A pending cancellation that itself
  expires may be recorded as expired-unconfirmed before a new explicit renewal.
- Permit a reference-adapter retry only for the latest exact journal intent
  whose outcome proves dispatch was not attempted. Product errors, nonzero
  exits, rejection, accepted, unknown, orphaned, superseded, and older attempts
  are non-retriable. Renewal after expiry is a fresh envelope and current
  authorization, not a retry.
- Reuse an idempotency key only for the same semantic operation, and return the
  prior status for duplicates instead of executing twice.
- Bound agent-to-agent exchanges and escalate unresolved or conflicting state
  to the operator.

## Reporting a vulnerability

Use GitHub's **Security → Report a vulnerability** flow for this repository.
Do not open a public issue containing exploit details, raw envelopes, callbacks,
session identifiers, local paths, peer listings, transcripts, queue rows,
credentials, or customer data.

There are currently no production-supported versions. A useful report names
the exact CAM document revision, validation profile and runtime metadata,
operating system, product versions, transport profile, and a sanitized
reproducer.
