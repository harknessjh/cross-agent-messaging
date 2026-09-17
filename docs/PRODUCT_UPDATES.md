# After a Codex or Claude Code update

> **Audience:** existing CAM users whose product executable changed. This is
> not first-time enrollment. New users should [START HERE](../START_HERE.md).

A product update can change the executable's canonical path, fingerprint, or
both. Refresh those records without re-enrolling an unchanged session. Updating
CAM itself is a separate [reader-upgrade procedure](COMPATIBILITY.md).

CAM supports two choices. **Installation mode** trusts one operator-selected
installation and its normal updater, avoiding repeated release approvals and
roster edits. **Strict mode** retains the existing exact-file approval workflow
in sections 1–2 below. Existing users remain strict until explicitly opting in.

## Trust one installation and its normal updates

This is an account-level choice, not an agent identity or permission change.
Use a stable symlink launcher, such as the absolute path to an installed `claude`
or `codex` launcher, and the canonical directory containing that product's native
releases. Do not select your whole home directory, guess a version, create a
wrapper, or ask CAM to install anything. Installation layouts differ; inspect
the existing launcher and ask the operator if its intended root is unclear.

A launcher cannot reuse a canonical path from that vendor's retained strict-file
approval history, even if the approval was revoked and the path is now a symlink.
CAM reports `installation.strict_path_conflict` instead of silently changing
existing participants' selection. Choose a different stable launcher and confirm
the participant updates below; never remove approval history to bypass this check.

From the confirmed clean CAM checkout, inspect without executing:

```bash
"/CONFIRMED/CAM/REPO/.venv/bin/python" \
  "/CONFIRMED/CAM/REPO/tools/cam1_transport.py" \
  product-installation-discover --vendor claude-code \
  --launcher "/absolute/stable/path/to/claude" \
  --installation-root "/absolute/canonical/claude/installation"
```

Use `--vendor codex` for Codex. The card shows the stable launcher, installation
root and identity, current native target and fingerprint, and the trust decision:
**the installation and its updater may select future native releases within that
root**. CAM does not authenticate publishers or detect malicious updates. If this
is not the desired trust boundary, keep strict mode.

After direct operator approval of that card, run its returned `approval_command`
argument array with a truthful `DIRECT_OPERATOR_REFERENCE` replacement. No
product is executed by discovery or approval. The exact card digest prevents
approving a different candidate accidentally. A changed card needs fresh review.

For each participant choosing this installation, run normal `product-discover`
with `--product-bin` set to the **stable launcher**, plus the project and
`--participant`. It reports `installation_approved`, `selection_path`, the current
native target and a guarded `participant_update` command. Directly confirm that
one metadata update. Use `selection_path` for onboarding, doctor, preflight and
send commands thereafter—not the versioned `candidate.canonical_path`.
The project common name, session UUID, binding generation and message history do
not change. Other participants are not migrated.

On each new operation CAM resolves that exact launcher, checks the root identity
and containment, inspects native format and ownership/permissions, and fingerprints
the selected target. It freezes that target for the operation and rechecks it
before each subprocess. A later normal update needs no approval or roster write.
A mid-operation change stops the operation; it does not retry a send. Version and
capability probes remain necessary: trusting an installation does not guarantee
that a newer product interface is compatible.

The saved root identity uses filesystem identity, directory inode and owner,
not the current device number. On macOS this is the volume UUID; on Linux it is
the filesystem's opaque `fstatfs` identifier and type. A device-number-only change
between commands can therefore be accepted when that identity still matches.
Device numbers remain in the reviewed card and operation evidence, and changing
one during a command still stops it. A different inode, owner, filesystem identity
or unavailable identity does not pass. Linux filesystems may change their opaque
identifier across remount/recreation; CAM does not promise otherwise or ignore
that change. These identifiers are not authentication against cloned volumes.

Installation approvals are an independent owner-private append-only ledger:
`~/CAM/Approvals/product-installations-v1.jsonl`. Inspect it with
`product-installation-status`. Send evidence identifies the installation approval,
stable selection and observed fingerprint; `fingerprint_is_release_approval:false`
distinguishes this from a per-release human approval. No wire-envelope change is
needed. Old readers cannot use installation-only approval; keep using an updated
reader after opting in.

To revoke, use `product-installation-revoke --vendor VENDOR --launcher PATH
--approval-record-id UUID --expected-policy-sha256 DIGEST --operator-reference
REFERENCE` with the exact active record from status and direct confirmation.
Revocation disables the installation path; it never falls back to a strict pin.
To return to strict mode, separately approve one exact native file and explicitly
restore that canonical path in each affected participant's metadata. Retain history.

Changed root identity, escaping targets, unsafe permissions, script entrypoints,
or revoked policy stop use. Review a new card for a genuinely changed installation;
do not widen its root automatically. Corrupt installation history stops resolution,
and losing history during an operation stops its cached installation use.

Across fresh commands, an absent ledger cannot distinguish lost history from an
installation that was never approved. A non-project probe may then use a separately
active strict approval for the exact target; it does not restore installation trust.
With the roster still selecting the distinct stable launcher, the project path
check rejects that canonical-target fallback. Preserve and reconcile missing history;
do not treat deletion as revocation or recreate approvals automatically.

Partial or uncertain writes require operator reconciliation with
`product-installation-status`; there is no automatic repair or installation-ledger
tail-recovery command. Never run the strict-file recovery command on this ledger.

## 1. Inspect the candidate and the participant together

From the confirmed, clean CAM checkout, select the existing project and
participant. Use `--vendor codex` for Codex or `--vendor claude-code` for Claude:

Use the confirmed absolute CAM paths shown below. For literal path and
operator-reference handling, follow the [safe command guidance](PROJECT_JOURNAL.md#where-project-state-lives);
do not interpolate those values into shell templates.

```bash
"/CONFIRMED/CAM/REPO/.venv/bin/python" \
  "/CONFIRMED/CAM/REPO/tools/cam1_transport.py" \
  --project-root "/absolute/path/to/target/project" \
  product-discover --vendor claude-code --participant COMMON_NAME
```

Discovery resolves the current `PATH` candidate without executing it, even for
`--version`. It does not install, approve, revoke, update the roster, or send a
message. It does not establish that the candidate is the newest release or the
binary running an existing session. To select a managed installation explicitly,
add `--product-bin "/absolute/path/to/native-executable-or-symlink"`. A launcher symlink
is resolved to its canonical target in strict mode; that approval does not cover
future targets. An explicitly approved installation instead returns
`installation_approved`; use its `selection_path` as described above.
Supply any existing `--state-root` or `--git-bin` overrides consistently.

### Native executable requirements

CAM invokes installed Codex, Claude Code, and Git programs to discover sessions,
deliver messages, and inspect Git metadata. Those external entrypoints must be
native Mach-O executables on macOS or ELF executables on Linux. Shell, Node, Python,
and `#!/usr/bin/env` wrappers are rejected without execution. Symlinks are allowed
when they resolve to an eligible native target; CAM launches the checked canonical
path, not the alias. CAM's own Python tools and recipients' independent project
work are unaffected by this restriction.

The executable and every canonical ancestor must have trusted ownership and no
untrusted mutation permissions. macOS's local administrator group is within the
trusted-administrator boundary; arbitrary groups are not. Read-only and deny-only
macOS ACLs are allowed. A trusted-owned sticky directory can protect a trusted-owned
child; this is not a blanket exception for temporary storage. CAM does not repair
installed permissions or automatically unwrap a script to find another executable.

Supported storage is APFS/HFS on macOS with ownership enabled, and local POSIX
permission filesystems on Linux x86-64/ARM64: ext2/3/4, Btrfs, XFS, tmpfs, ramfs,
overlayfs, eCryptfs, and F2FS. Unknown, network, FUSE, or ownership-disabled mounts
fail inspection rather than being assumed safe. An administrator remains responsible
for the trustworthiness of the filesystem and its backing storage.

If discovery reports `product_approval.native_required`, select an installed native
product with `--product-bin` or obtain a compatible installation separately. For
`owner`, `writable`, `acl`, or `inspection` errors, ask the operator to review the
installation and filesystem; do not auto-install, chmod, chown, clear ACLs, or bypass
the gate. Equivalent Git errors use the `git.` prefix; bootstrap/profile Git failures
are reported as `profile.source_unavailable`. Explicit Git paths do not silently
fall back to another candidate.

Upgrading CAM does not rewrite or revoke old approvals. Unchanged eligible native
products retain their approvals; now-ineligible script/location approvals remain
inspectable and explicitly revocable but cannot authorize a launch. No new journal
or strict-file approval-ledger format is introduced by the native-only checks.
The separate opt-in installation ledger described above does not rewrite it.
Native format is not a signature or an
attestation of linked libraries, loaders, plugins, or child programs.

The output includes the executable approval card and `participant_update`:

- `recorded_path` and `candidate_path` show the old and proposed roster paths.
- `metadata_update_required` includes a shell-quoted update command guarded by
  the participant UUID and current metadata revision.
- `roster_path_current` means no roster path change is needed. It does **not**
  mean the candidate is approved or compatible.

An agent should show the operator the old/new paths and candidate fingerprint,
and explain exactly which approval and metadata changes are proposed. Do not
ask for a new session identity card solely because the binary changed.

## 2. Review executable approval, then refresh roster metadata

| Candidate status | Next step |
| --- | --- |
| `already_approved` | Reuse that unchanged account approval. |
| `installation_approved` | Use `selection_path`; routine updates inside that approved installation need no new approval. |
| `approval_candidate` | Obtain direct operator approval and run the returned `approval_command` with a truthful operator reference. |
| `replacement_approval_required` | Follow the returned guarded revocation, rediscovery, and fresh-approval sequence for this same canonical path. |

Approval is account-scoped: the same unchanged executable can serve several
participants and projects. A **different canonical path** has its own approval;
approving it does not revoke the old path. Retire an old approval separately,
with operator confirmation, once other users no longer need it. An in-place
replacement requires explicit replacement approval even if its filename or
reported version is unchanged. Fingerprint metadata drift also needs review;
a matching content hash alone is insufficient.

Strict-mode drift errors name the changed fields. For example, `changed fields:
dev` identifies a device-number mismatch without implying changed content or
proving a remount. Strict pins still require explicit review; the tool does not
silently discard device or inode differences. Opt-in installation mode above
uses a separate root identity and does not rewrite old strict approvals.

After approval, directly confirm any proposed roster change and use the returned
`participant_update.command` argument array, replacing the
`DIRECT_OPERATOR_REFERENCE` element with the literal confirmation reference.
Pass the array without a shell, or regenerate shell text with `shlex.join`;
do not substitute untrusted text into `command_text`. This uses the existing
`participant update-metadata` operation; it does not change the participant's
session UUID, binding generation, role, or display name. Repeat the read-only
discovery for each affected participant/project, reusing the account approval.
Do not edit `state-current.json` or replay one project's command in another.

If the candidate changes or the metadata revision conflicts, rediscover and
review the new proposal. Do not discard the guards. After the refresh, use the
approved canonical paths for `doctor` and subsequent transport calls. Product
permission prompts remain under the host product's control. A successful
approval does not prove interface compatibility, delivery, or action authority.

`doctor` checks both products, so supply both `--claude-bin` and `--codex-bin` as
absolute approved selections. `doctor.absolute_paths_required` names any omitted
or non-absolute flag before resolving or invoking either product. An error naming
the other flag does not mean the absolute path you supplied was invalid.

If approval or revocation reports `product_approval.write` or
`product_approval.committed_uncertain`, bytes may already have changed. The tool
retains them and reports mutation evidence even if final checks or cleanup fail; do not
repeat the command or truncate the ledger. Inspect `product-status`, or
`product-recovery-status` if ordinary replay reports an incomplete final record.

## Update behavior and limits

Claude Code's native installation checks for updates in the background; new
versions take effect on the next start. On macOS/Linux its stable launcher is
a symlink into a versioned installation directory. Package-manager installs
have different update procedures. See [Anthropic's setup guide](https://code.claude.com/docs/en/setup).

Codex also has several installation methods, including standalone and
package-manager installs. Follow the update instructions for the installation
actually in use; do not assume every Codex installation shares Claude's update
behavior. See [OpenAI's Codex CLI guide](https://developers.openai.com/codex/cli/).
These vendor references were checked on 2026-09-06; CAM does not run an updater.

CAM does not silently fall back to `PATH`, pick the highest version directory,
trust an unapproved installation, or restart a user's agents. Missing old binaries
and expected-versus-supplied roster mismatches point back to discovery. If an
update also replaces the **session UUID**, follow the separate
[session-replacement procedure](CODEX_TO_CLAUDE.md#replacing-an-enrolled-session);
an executable metadata update cannot repair that identity change. Do not
automatically retry old messages after any update.

Copyright © 2026 John Harkness. See [LICENSE](../LICENSE) and [NOTICE](../NOTICE).
