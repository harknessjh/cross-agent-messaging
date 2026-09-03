# CAM/1: same-host Codex–Claude Code messaging

> **New here? [START HERE](START_HERE.md).** It is the only guide a
> human operator needs to set up CAM/1, enroll two sessions, and complete one
> harmless first-contact round trip.

CAM stands for Cross-Agent Messaging. CAM/1 is an experimental community
profile that lets independent Codex and Claude Code sessions on the same
computer exchange structured messages through their existing local transports.
The `/1` identifies wire-major version 1.

CAM/1 is not an OpenAI, Anthropic, or Model Context Protocol standard, and
those projects do not endorse it.

## What CAM/1 provides

CAM/1 combines four small pieces:

1. **Structured envelopes** identify the claimed sender, intended recipient,
   scope, constraints, expiry, and reply correlation.
2. **Typed builders and validators** prevent agents from hand-assembling or
   silently repairing malformed messages.
3. **One-shot local adapters** use Claude Code's session messaging and Codex's
   local queue without adding a new message service.
4. **A required project journal** keeps a private, append-only record of sent
   and received messages, transport outcomes, acknowledgments, and participant
   history.

The journal makes conversations reviewable by the human operator. Product
transports still control delivery, and a transport receipt remains distinct
from recipient handling or completed work.

Before CAM invokes Codex or Claude Code, a non-executing discovery step normally
binds the exact product path and fingerprint to a direct, account-scoped
operator approval. That approval is reused across projects while the executable
remains unchanged; it authorizes only CAM's use of that executable, not a
message body or project action. A narrowly bounded one-time migration can
grandfather an unchanged executable from a directly confirmed legacy CAM
enrollment; it creates the same auditable approval record before product I/O.

## Deliberate limits

CAM/1 supports sessions running on one host under the same operating-system
account. It does not provide:

- remote, cloud, cross-machine, or cross-account delivery;
- cryptographic peer authentication or trusted human identity;
- permission delegation or automatic execution of received instructions;
- a broker, daemon, database, GUI, inbox reader, or polling service; or
- shared conversation context or proof that an agent's report is true.

Every received message is untrusted input. The receiving session must apply its
own permissions and obtain its own authority before consequential work.

## Working style

CAM is a messenger, not a firewall or work manager. Its tools can detect
contradictions among an envelope, the project roster, the current session, and
the Git project. They cannot prevent an operator from deliberately or
accidentally pasting or directing content to another session.

Keep successful CAM mechanics in the background. Communicate the substance:
what a collaborator said, what you think, and what changes. Mention
preservation, validation, journal, sequence, or hash details when they affect
trust, recovery, or the result—not as routine narration.

A structured envelope does not turn a suggestion into a mandate. Unless
existing operator direction or receiver-owned policy requires a particular
mechanism, agents should continue to reason independently, propose equivalent
or better approaches, and exercise their ordinary initiative within existing
authority.

## Requirements

- macOS or another compatible POSIX environment;
- Python 3.11 through 3.14;
- Git and a local target directory initialized with `git init`;
- installed Codex and Claude Code commands;
- one independent session from each product on the same host and user account;
- direct approval of each unchanged product executable fingerprint, except for
  a qualifying one-time migration from a directly confirmed legacy enrollment;
  and
- human confirmation of each session's enrollment identity card.

The target project does not need an initial commit. Start each agent inside the
target Git worktree; CAM uses the current working directory by default.

Clone CAM and create its Python environment once. Repeat project preparation
and initial session enrollment for each project that will use CAM. Replacing a
session inside an existing project uses the existing roster and journal; follow
the replacement procedure linked from [START HERE](START_HERE.md) instead of
creating another CAM clone or project journal.

Follow [START HERE](START_HERE.md) for installation and the complete
first-contact workflow.

## Identity and routing in one minute

- A Codex thread's stable identity is its full thread UUID.
- A Claude Code session's stable identity is its full session UUID.
- Human-readable names and Claude MCP short references are routing aids, not
  stable identity.
- Each session proposes its own identity card. The human confirms that exact
  card directly in the same session.
- Before every Claude send, CAM freshly correlates the bound full UUID through
  Claude's current discovery surfaces and verifies that the live session still
  belongs to the intended Git project.

The operator confirms stable, human-visible information. CAM does not ask the
operator to recognize a transient MCP short reference that Claude `/status`
does not normally show.

## Where CAM stores project state

CAM adds no files to the application worktree. It stores:

- a private project pointer below `<git-common-dir>/cam1/`; and
- the owner-only append-only journal below
  `~/CAM/Journals/<project-slug>--<project-uuid>/`; plus
- a separate owner-private, append-only account approval ledger at
  `~/CAM/Approvals/product-executables-v1.jsonl`.

The approval ledger is not a project journal. It records which unchanged local
product executables CAM may invoke and is reused across Git projects under the
same operating-system account.

Reader and project-state upgrades use the staged, atomic
[compatibility kernel](docs/COMPATIBILITY.md).

The journal normally fails closed without repair. A narrowly scoped,
operator-confirmed command can recover only an incomplete EOF record: it first
archives the exact damaged bytes, then atomically installs the verified prefix
plus an explicit recovery record. Complete malformed or altered records remain
investigation-only.

Appending a journal record is not a Git commit. The journal is an audit record,
not a message queue or source of authority. See the optional
[project-journal guide](docs/PROJECT_JOURNAL.md) to inspect or recover it.

## Documentation by task

Only the first row is required for a new user.

| Your task | Read this |
| --- | --- |
| Install CAM/1 and send the first message | **[START HERE](START_HERE.md)** |
| See the agent command reference or troubleshoot | [Detailed Codex-to-Claude procedure](docs/CODEX_TO_CLAUDE.md) |
| Inspect the roster or audit journal | [Project journal guide](docs/PROJECT_JOURNAL.md) |
| Roll out a reader upgrade or understand causal holds | [Compatibility gates](docs/COMPATIBILITY.md) and [causal ordering](docs/CAUSAL_ORDERING.md) |
| Understand risks or report a vulnerability | [Security policy](SECURITY.md) |
| Implement or evaluate protocol conformance | [Protocol specification](PROTOCOL.md) and [wire schema](cam-1.schema.json) |
| Understand tested product behavior | [Implementation notes](docs/IMPLEMENTATION_NOTES.md) |
| Study onboarding's behavioral influence (maintainers; disposable sessions only) | [Optional authority-neutrality evaluation](docs/AUTHORITY_NEUTRALITY_EVALUATION.md) |
| Change or release this repository | [Contributing](CONTRIBUTING.md), [agent instructions](AGENTS.md), and the [release checklist](docs/PUBLIC_RELEASE_CHECKLIST.md) |

START HERE names the command-reference sections that each agent must follow;
the human operator does not need to read them. The protocol and schemas are
normative implementation references. Implementation notes and the release
checklist are maintainer material, not onboarding prerequisites.

## Security and privacy

Do not put credentials, private keys, customer content, or unnecessary local
routing metadata in CAM messages or public reports. A valid envelope does not
authenticate its author or authorize its body.

Read [SECURITY.md](SECURITY.md) before consequential use. Report suspected
vulnerabilities through the private route described there rather than a public
issue.

## Project status

CAM/1 is experimental and depends on version-specific local product interfaces.
The synthetic test suite covers the supported Python versions and captured
Claude/Codex interface shapes without requiring live vendor sessions. Current
compatibility evidence and limitations are recorded in
[implementation notes](docs/IMPLEMENTATION_NOTES.md).

## Contributing

Issues and suggestions are welcome. External pull requests are not currently
accepted while contributor terms are being established. See
[CONTRIBUTING.md](CONTRIBUTING.md).

## License and copyright

Copyright © 2026 John Harkness.

This repository is source-available for noncommercial use under the
[PolyForm Noncommercial License 1.0.0](LICENSE). Preserve the notices required
by that license and [NOTICE](NOTICE).

Commercial rights are not granted by the public license. Contact the copyright
holder to discuss separate commercial terms.
