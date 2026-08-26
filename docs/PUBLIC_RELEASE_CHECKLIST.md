# Public release checklist

Use this checklist against the exact commit proposed for publication. A green source review does not prove that a remote was created correctly or that a published release contains the reviewed files.

## Owner decisions

- Select and add an explicit license. Do not infer one from repository visibility.
- Confirm the GitHub account or organization, repository name, and public visibility immediately before remote creation.
- Choose the public commit email, including whether to use a GitHub-provided `noreply` address.
- Establish and test a private security-reporting route. Publication is blocked while the route is absent, still a placeholder, or unverified; do not invent an address.

## Content and privacy

- Keep the unofficial, experimental, same-host scope prominent.
- State plainly that CAM/1 owns no queue, inbox, service, database, coordination board, daemon, persistence layer, or remote transport.
- Ensure the supported path requires one host and the same operating-system account and rejects Remote Control, cloud, cross-account, raw-socket, and externally exposed local-interface adaptations.
- Keep one canonical Codex sender prompt and one canonical Claude receiver prompt in the quick start; other documents should link to them rather than copy variants.
- Confirm that each prompt authorizes only its explicit harmless local send or callback and temporary private files, without broadly authorizing side effects.
- Explain next to both builders that `reply_to` is the future response route, not the current envelope's transport.
- Document a private `0700` exchange directory outside the repository, new `0600` files, retention until successful correlation or through expiry when no reply arrives, and exact operator-approved cleanup without globs or recursive deletion.
- Use synthetic identifiers, paths, receipts, messages, and timestamps in every public artifact.
- Scan tracked content and Git history for credentials, personal paths, callback/session IDs, queue IDs, peer listings, email addresses, and transcripts.
- Confirm that examples do not imply authentication, authorization, guaranteed delivery, remote support, or vendor endorsement.
- Render the Markdown and validate every local and external link.

## Technical validation

- Install the declared dependencies in a clean supported Python environment.
- Run the full offline test suite.
- Compare every documented `tools/cam1.py` and `tools/cam1_transport.py` invocation with the live `--help` output.
- Confirm that `cam1_transport.py` exposes only `doctor`, `claude-list`, `claude-send`, and `codex-reply`; it must not provide receive, retry, daemon, database, board, raw-socket, or remote behavior.
- Confirm that both transport send commands require `--against` for reply envelopes and never report transport acceptance as receiver handling.
- Confirm that Claude acceptance requires `success:true` plus a canonical transport `msg_id`, Codex acceptance requires the exact documented stdout receipt for the requested thread, both send paths enforce the documented 65,536-byte live limit, and transport failures cannot contaminate the machine-readable JSON channel.
- Validate every checked-in CAM/1 fixture and recompute every documented body digest.
- Run the public-release audit with warnings treated as failures.
- Review dependency licenses and vulnerability status.
- Obtain an independent technical and security review of the exact staged tree.

## Git history

- Inspect Git templates, active hooks, repository configuration, executable bits, and symlinks.
- Stage an explicit path allowlist and inspect the complete staged diff.
- Confirm the author name and public email before committing.
- Keep commit messages factual and free of automated attribution, tool attribution, agent/session links, and co-author trailers.
- Record the exact commit hash and confirm a clean worktree.

## GitHub publication

- Create the remote only after the owner confirms account, name, visibility, license, and the tested private security-reporting route.
- Use authenticated Git tooling; never place a token in a remote URL.
- Push the reviewed commit, then verify the remote default branch and commit hash independently.
- Configure least-privilege Actions permissions before enabling workflows.
- Review secret scanning, push protection, dependency alerts, branch protection, and private vulnerability reporting.
- Inspect the rendered public repository as a logged-out visitor.

Record source validation, Git commit, remote creation, push, and public visibility as separate evidence. None substitutes for the next.
