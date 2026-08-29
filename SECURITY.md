# Security policy

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
audit record, not a delivery mechanism or authority source.

## Trust and authorization

- Treat every message and every `claimed_sender` value as untrusted.
- Treat an active bound, operator-correlated participant entry only as a
  session mapping. It is not authentication, authorization, route-control
  proof, or proof of authorship. An optional mutual challenge adds
  reachability evidence only.
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
- Treat the resulting Claude `name [ref]` as transient routing metadata. Do not
  treat a mutable name, short ID, working directory, process ID, or UDS path as
  identity.
- Fail closed on absent, ambiguous, duplicated, nonlocal, or inconsistent
  discovery. Never guess or silently retarget.
- Treat paths reported by `doctor` as candidates. The operator must approve an
  absolute Claude or Codex executable path, and every live operation must use
  it explicitly. This avoids ordinary `PATH` substitution but cannot eliminate
  replacement or time-of-check/time-of-use attacks by the same account.
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

## Validation, expiry, and retries

- Run `cam1.py validation-profile` before live use. The digest identifies the
  reference tool and schema bytes; adjacent source-control and runtime fields
  identify the environment that judged the message. They do not authenticate
  a peer or prove that a verdict is correct.
- Use a clean CAM checkout for ordinary live sends. A development-only dirty
  override must repeat the exact current profile digest and is recorded in the
  journal. Never present an overridden run as a clean or reproducible release.
- Require a resolvable HEAD commit and the same complete set of regular profile
  blobs in HEAD and the working tree. The reference profile includes every
  Python source below `tools/` and importable binary or sourceless modules
  outside standard `__pycache__` directories. Compare the exact profiled bytes
  with those blobs and reject assume-unchanged, skip-worktree, or sparse index
  state. The public facades redirect bytecode lookup while importing audited
  modules so ignored adjacent caches cannot replace tracked source. The dirty
  override may cover ordinary tracked edits, but never missing or untracked
  profile paths or concealed index state.
- Do not use an unpacked or otherwise unversioned source tree for live sends.
  Offline validation remains available, but live use requires verifiable Git
  revision and clean/dirty state in addition to the content profile.
- Do not pipe standalone validation into a native transport command. A later
  process can mask the validator's nonzero status. Use the project-aware
  adapter, which validates the exact bytes again before dispatch.
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
