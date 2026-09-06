# After a Codex or Claude Code update

> **Audience:** existing CAM users whose product executable changed. This is
> not first-time enrollment. New users should [START HERE](../START_HERE.md).

A product update can change the executable's canonical path, fingerprint, or
both. Refresh those records without re-enrolling an unchanged session. Updating
CAM itself is a separate [reader-upgrade procedure](COMPATIBILITY.md).

## 1. Inspect the candidate and the participant together

From the confirmed, clean CAM checkout, select the existing project and
participant. Use `--vendor codex` for Codex or `--vendor claude-code` for Claude:

```bash
.venv/bin/python tools/cam1_transport.py \
  --project-root /absolute/path/to/target/project \
  product-discover --vendor claude-code --participant COMMON_NAME
```

Discovery resolves the current `PATH` candidate without executing it, even for
`--version`. It does not install, approve, revoke, update the roster, or send a
message. It does not establish that the candidate is the newest release or the
binary running an existing session. To select a managed installation explicitly,
add `--product-bin /absolute/path/to/launcher-or-executable`. A launcher symlink
is resolved to its canonical target; approval never covers all future targets.
Supply any existing `--state-root` or `--git-bin` overrides consistently.

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
| `approval_candidate` | Obtain direct operator approval and run the returned `approval_command` with a truthful operator reference. |
| `replacement_approval_required` | Follow the returned guarded revocation, rediscovery, and fresh-approval sequence for this same canonical path. |

Approval is account-scoped: the same unchanged executable can serve several
participants and projects. A **different canonical path** has its own approval;
approving it does not revoke the old path. Retire an old approval separately,
with operator confirmation, once other users no longer need it. An in-place
replacement requires explicit replacement approval even if its filename or
reported version is unchanged. Fingerprint metadata drift also needs review;
a matching content hash alone is insufficient.

After approval, directly confirm any proposed roster change and run
`participant_update.command_text`, replacing `DIRECT_OPERATOR_REFERENCE` with
the actual confirmation reference. This uses the existing
`participant update-metadata` operation; it does not change the participant's
session UUID, binding generation, role, or display name. Repeat the read-only
discovery for each affected participant/project, reusing the account approval.
Do not edit `state-current.json` or replay one project's command in another.

If the candidate changes or the metadata revision conflicts, rediscover and
review the new proposal. Do not discard the guards. After the refresh, use the
approved canonical paths for `doctor` and subsequent transport calls. Product
permission prompts remain under the host product's control. A successful
approval does not prove interface compatibility, delivery, or action authority.

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
trust an installer directory, or restart a user's agents. Missing old binaries
and expected-versus-supplied roster mismatches point back to discovery. If an
update also replaces the **session UUID**, follow the separate
[session-replacement procedure](CODEX_TO_CLAUDE.md#replacing-an-enrolled-session);
an executable metadata update cannot repair that identity change. Do not
automatically retry old messages after any update.

Copyright © 2026 John Harkness. See [LICENSE](../LICENSE) and [NOTICE](../NOTICE).
