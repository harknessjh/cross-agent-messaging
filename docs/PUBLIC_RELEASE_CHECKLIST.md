# Public release checklist

> **Audience:** CAM/1 maintainers preparing a release. This checklist is not
> part of user onboarding; new users should begin with
> [START HERE](../START_HERE.md).

Use this checklist against the exact commit proposed for publication. A green source review does not prove that a remote was created correctly or that a published release contains the reviewed files.

## Owner decisions

- Confirm the canonical PolyForm Noncommercial 1.0.0 text, required copyright notice, and public license summary. Do not infer rights from repository visibility.
- Confirm the GitHub account or organization, repository name, and public visibility immediately before remote creation.
- Choose the public commit email, including whether to use a GitHub-provided `noreply` address.
- Approve GitHub private vulnerability reporting as the public security-reporting route. Public visibility is not complete until the route is enabled and verified; do not invent another address.
- Confirm that the copyright holder owns the repository contents and is authorized to publish them.

## Content and privacy

- Keep the unofficial, experimental, same-host scope prominent.
- State plainly that CAM/1 owns no queue, inbox, service, database, legacy
  coordination board, daemon, GUI, automatic executor, raw-socket path, or
  remote transport. Describe the required local project journal as audit state,
  never delivery or authority.
- Ensure the supported path requires one host and the same operating-system account and rejects Remote Control, cloud, cross-account, raw-socket, and externally exposed local-interface adaptations.
- Keep one canonical Codex sender prompt and one canonical Claude receiver
  prompt in [the first-contact runbook](../START_HERE.md); other documents
  should link to them rather than copy variants. Confirm that a new clone can
  follow them without undocumented project state or routing knowledge.
- Confirm START HERE requires no manual `CAM_CHECKOUT` or `PROJECT_ROOT`
  substitution. Each prompt must use the session cwd as the project and perform
  a bounded, non-executing CAM-checkout candidate search, display path/remote/
  HEAD/status, stop for exact human selection, re-probe, and only then invoke
  the selected checkout's trusted-source validation.
- Confirm that each prompt authorizes only its explicit harmless local send or
  callback and owner-private working files outside tracked worktrees, without
  broadly authorizing side effects.
- Explain next to both builders that `reply_to` is the sender's supported
  return transport and stable full session UUID, not the current envelope's
  transport or a transient Claude `name [ref]`. Explain that a Codex session
  returns to `claude_send_message` through the CAM adapter's Claude MCP bridge.
- Document the private Git-common-directory binding and required external
  `~/CAM/Journals/<slug>--<uuid>` directory, modes `0700`/`0600`, exact-byte
  append-before-validation, hash-chain limits, correction rules, projections,
  retention, and human audit path.
- Confirm the roster distinguishes project display name, participant common
  name, product label, optional descriptive role, stable full session UUID,
  operator-reviewed executable, optional Agent View ID, and fresh route.
  Confirm role is mutable and non-authoritative, a missing Agent View ID remains
  null, and the roster never stores a Claude process ID or UDS.
- Confirm each session's normal first-contact path prepares one non-routable
  self-enrollment proposal and shows one concise card. The operator approves
  the stable UUID, project, project-local name, product metadata, and absolute
  executable directly in that session—not the transient MCP `name [ref]`,
  which is normally absent from `/status`. Keep the exact discovered ref in the
  journal as tool-derived audit evidence without presenting it as a human
  authentication factor. State explicitly that the card's digest-derived code
  is correlation, not authentication or work authority.
- Use synthetic identifiers, paths, receipts, messages, and timestamps in every public artifact.
- Describe field incidents through sanitized shapes and timelines. Do not
  publish private journal artifacts, real session UUIDs, transient refs, local
  paths, or message bodies as fixtures or documentation evidence.
- Scan tracked content and Git history for credentials, personal paths, callback/session IDs, queue IDs, peer listings, email addresses, and transcripts.
- Confirm that examples do not imply authentication, authorization, guaranteed
  delivery, remote support, journal-backed delivery, tamper-proof history, or
  vendor endorsement.
- Confirm that expiry is described as the first-handling window: pending and
  held roots expire unconfirmed, while a request recorded as received,
  accepted, or started before expiry may later advance legally. Receipt alone
  never authorizes execution.
- Render the Markdown, run the automated local-file and anchor-link test, and
  validate external links.

## Technical validation

- Install the declared dependencies in a clean supported Python environment.
- Run the full offline test suite.
- Compare every documented `tools/cam1.py`, `tools/cam1_project.py`, and
  `tools/cam1_transport.py` invocation with the live `--help` output.
- Confirm that the canonical `cam1_transport.py` command surface contains these
  12 operations, grouped by purpose:
  - capability and routing: `doctor`, `claude-list`, `claude-preflight`;
  - executable approval and guarded recovery: `product-discover`,
    `product-approve`, `product-status`, `product-recovery-status`,
    `product-recover-partial-tail`, `product-revoke`; and
  - live transport: `claude-send`, `codex-send`, and the behavior-identical
    `codex-reply` compatibility alias.
  No command may provide receive, polling, an automatic retry service, daemon,
  database, board, raw-socket, or remote behavior. The explicit journal-gated
  retry must remain the only retry path.
- Confirm `cam1.py validation-profile` reports an available deterministic
  digest covering every Python source below `tools/` and non-cache importable
  modules, a concrete HEAD, matching regular profile path sets and bytes,
  unconcealed index flags, clean CAM source state, and bounded runtime metadata.
  Confirm direct public CLI invocations use isolated Python and an exact
  captured-source allowlist, reject adjacent source/bytecode/native collisions,
  and detect source changes before the live gate. Verify valid and invalid
  verdicts plus inbound/outbound journal events carry that profile.
- Confirm ordinary `doctor`, list, preflight, and live sends refuse a dirty CAM
  checkout before product resolution or probing. A
  development override must require the exact reported digest and record
  `dirty_validator_override`; it is not release evidence and cannot override a
  changed executable Python source, a missing HEAD, a changed profile path set,
  or concealed/sparse index state.
- Confirm that `product-discover` is the only operation permitted to consult
  `PATH`, and that it only resolves and fingerprints a candidate without
  executing or approving it. Every live doctor, list, preflight, send, and
  reply requires the applicable operator-approved absolute product executable
  path; every project preflight, send, and reply must also resolve the bound
  project.
- Confirm approval-ledger shared and exclusive locks use a bounded monotonic
  wait and fail closed on timeout. Exercise status and mutation contention
  without deleting a lock target or weakening transaction semantics.
- Confirm approval-ledger recovery is never automatic: status must precede a
  directly confirmed mutation with exact identity/full-ledger/prefix/tail
  guards. Verify the exact owner-private archive bytes, unchanged active
  approvals, stale-guard refusal, complete/interior corruption refusal,
  symlink and permission refusal, concurrent recovery, and interrupted recovery
  crash states.
- Confirm that project initialization writes only to private Git administrative
  state and the external journal root, linked worktrees share the project UUID,
  and no journal is tracked by Git. Confirm enrollment works after `git init`
  without an initial commit and leaves both the application filesystem snapshot
  and `git status --porcelain=v2 --untracked-files=all` unchanged outside
  private Git administrative state. A CAM journal append is not a Git commit.
- Confirm that each journal mutation transaction verifies the full chain on
  first read, revalidates the locked file identity before every later
  operation, advances only from exact appended bytes, serializes complete
  writes, fsyncs each record, and rejects symlinks, hard links, FIFOs, partial
  lines, substitution, and tampering.
- Confirm explicit partial-tail recovery requires the expected full digest and
  project UUID, preserves the exact damaged bytes in an owner-only archive,
  atomically installs only a verified prefix plus recovery record, and refuses
  clean journals or complete malformed, altered, or chain-invalid records.
- Confirm that every append captures the current worktree ID, Git HEAD, tree,
  branch, and dirty state through trusted tooling and fails closed when required
  provenance cannot be obtained.
- Confirm all Git discovery and provenance probes use the bound absolute
  executable with a minimal noninteractive environment, optional locks and
  repository hooks/filesystem monitors disabled, and no submodule traversal.
- Confirm that journal tail defaults to redacted output and requires the
  explicit `--show-content` flag before decoding message bodies; malformed
  bytes remain bounded and safely encoded.
- Confirm that the incoming product-visible serialization is journaled before
  validation, outbound intent before send, and transport outcome afterward;
  each failure mode must preserve the correct split state.
- Confirm `message ingest --stdin --capture-to` reads bounded binary input,
  exclusively creates a new owner-only file, preserves captured whitespace and
  Unicode byte-for-byte, and journals malformed input before rejecting it.
  Keep the documented product-native literal-write fallback and do not claim
  access to hidden transport framing.
- Confirm inbound ingestion requires an active local participant and rejects
  recipient vendor, common-name, or full-session mismatches and unknown sender
  roster identities only after preserving the exact bytes.
- Confirm successful ingest reports `validated` plus explicit false
  authorization/action fields rather than using lifecycle `accepted`
  terminology.
- Confirm that atomic `state-current.json` rebuilds exclusively from the
  journal, includes enrollment, participant, and lifecycle projections, and
  cannot authorize a message or repair history.
- Exercise compatibility `plan`, `ready`, and `activate` as one staged,
  clean-profile workflow. Confirm the plan freezes every non-retired binding
  and required reader capability; activation refuses incomplete readiness,
  expiry, profile or roster drift, unsupported capability, and conflicting
  replay; and no compatibility event grants authority or performs product I/O.
- Confirm an unsupported active gate fails with
  `compatibility.upgrade_required` before stateful replay. If activation commits
  but projection refresh fails, require `activated_projection_stale`, do not
  retry activation, and rebuild the disposable projection from the canonical
  journal.
- With `causal.ordering/1` active, confirm the project-aware adapter derives
  `CAM-CAUSAL/1` context only from the canonical project journal and never adds
  it to the wire envelope. Exercise a post-activation stale request or cancel:
  record one hold with `lifecycle_committed: false`, apply no lifecycle or
  action state, and keep an exact redelivery held. Confirm pre-activation
  conversations remain grandfathered and that only an exact
  `transport.not_accepted` outcome with `delivery_state: not_attempted` is
  excluded from the potentially dispatched frontier.
- Exercise enrollment proposal replay, exact-digest confirmation, current
  session/project/profile recheck, supersession, duplicate prepare/confirm
  idempotency, common-name and session collisions, and atomic participant plus
  binding creation. Prove that pending or superseded proposals cannot be used
  as endpoints. Prove that metadata updates preserve identity, binding, and
  route while advancing their own revision.
- Confirm that both transport send commands require `--against` for reply envelopes and never report transport acceptance as receiver handling.
- Confirm a project-aware Claude-originated root can be queued to Codex and a
  correlated Codex reply can return through `claude-send --against` after fresh
  route checks. Confirm malformed envelopes reach neither product transport nor
  outbound journal intent.
- Confirm every live envelope's selected recipient and claimed sender match
  active bound roster entries by vendor, common name, and stable full session
  UUID, and that the non-null callback matches the bound sender.
- Confirm that a live Codex reply matches the preserved stable callback UUID;
  a live Claude send selects a full session UUID through fresh Agent View plus
  `ListAgents` route correlation and requires its cwd inside the bound project;
  optional `--to` only guards that fresh route.
  Confirm ref-only churn is freshly tool-correlated without another operator
  prompt, while UUID, product-name, configured-kind, project, binding-generation,
  duplicate-name, and same-attempt evidence drift fail before journal intent or
  product dispatch.
  Confirm live transport refuses stdin, FIFO inputs fail before waiting for a
  writer, and offline stdin validation remains available.
- Exercise heterogeneous Agent View fixtures in which background `id`/`state`
  and interactive `pid`/`status` rows share a full UUID. Verify sole eligible
  process-row preference, the no-process legacy fallback, no companion-field
  merging, optional-ID validation/null retention, and PID exclusion from all
  serialized, journal, and projected state.
- Exercise `ListAgents` locality independently from activity: local `idle` and
  `busy` peers are addressable, local terminal or unknown peers are reported
  unavailable, and cloud or Remote Control peers remain excluded. Before
  claiming background-session support, exercise the observed Agent View
  `background` versus `ListAgents` `bg` spelling, retain both raw values for
  audit, reject unknown abbreviations, and prove that cloud and Remote Control
  markers still remain nonlocal.
- Exercise a sanitized background/resume/rename sequence that changes the UUID,
  kind, label, and ref while preserving correct route invalidation and explicit
  rebinding. Present the observed restored-interactive 2.1.251 exchange as live
  evidence, but do not claim delivery while backgrounded until separately
  tested.
- Confirm that Claude acceptance requires `success:true` plus a canonical transport `msg_id`, Codex acceptance requires the exact documented stdout receipt for the requested thread, both send paths enforce the documented 65,536-byte live limit, and transport failures cannot contaminate the machine-readable JSON channel.
- Validate every checked-in hello, request, acknowledgment, and result fixture;
  recompute every documented body digest; exercise each typed builder and every
  legal and illegal state transition.
- Exercise expired-root late rejection, safe renewal, status inquiry,
  idempotent duplicate handling, and accepted-before-expiry work that reports a
  fresh result after root expiry.
- Exercise `--retry-after-intent`: allow only the latest exact intent whose
  outcome proves dispatch was not attempted, and refuse product errors,
  nonzero exits, rejection, accepted, unknown, orphaned, superseded, or older
  attempts. Keep retry distinct from fresh renewal.
- Exercise the single-nonce rule: received advances through nonce-null status,
  held advances through a later ACK, and a cancel accepted after a received ACK
  uses nonce-null `status: accepted`.
- Run the public-release audit and require no unreviewed `WARN` findings. The
  two explicitly reviewed runtime-size deferrals in this revision are
  `tools/cam1_transport_native.py` at 1,299 lines and
  `tools/cam1lib/state_projection.py` at 1,206 lines. Keep both visible in the
  release evidence, reassess them after any material change, and treat every
  other or new `WARN` as a release failure.
- Review dependency licenses and vulnerability status.
- Obtain an independent technical and security review of the exact staged tree.

## Git history

- Inspect Git templates, active hooks, repository configuration, executable bits, and symlinks.
- Stage an explicit path allowlist and inspect the complete staged diff.
- Confirm the author name and public email before committing.
- Keep commit messages factual and free of automated attribution, tool attribution, agent/session links, and co-author trailers.
- Record the exact commit hash and confirm a clean worktree.

## GitHub publication

- Create an empty private remote only after the owner confirms account, name, initial visibility, license, copyright ownership, commit identity, and the public security-reporting plan. Do not initialize the remote with a README, license, or `.gitignore`.
- Use authenticated Git tooling; never place a token in a remote URL.
- Push only the reviewed default branch, then verify its remote commit hash independently. Do not push stale, backup, or review branches and tags.
- Configure least-privilege Actions permissions before enabling workflows.
- Before changing visibility, inspect every pushed ref plus Actions logs and artifacts; public visibility exposes the repository history and existing workflow records.
- After changing visibility, immediately enable and verify private vulnerability reporting, then review secret scanning, push protection, dependency alerts, code scanning, and branch protection. Reapply any rulesets disabled by the visibility change.
- Inspect the rendered public repository as a logged-out visitor.

Record source validation, Git commit, remote creation, push, and public visibility as separate evidence. None substitutes for the next.
