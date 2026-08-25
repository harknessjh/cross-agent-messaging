# Public release checklist

Use this checklist against the exact commit proposed for publication. A green source review does not prove that a remote was created correctly or that a published release contains the reviewed files.

## Owner decisions

- Select and add an explicit license. Do not infer one from repository visibility.
- Confirm the GitHub account or organization, repository name, and public visibility immediately before remote creation.
- Choose the public commit email, including whether to use a GitHub-provided `noreply` address.
- Establish a private security-reporting route.

## Content and privacy

- Keep the unofficial, experimental, same-host scope prominent.
- Use synthetic identifiers, paths, receipts, messages, and timestamps in every public artifact.
- Scan tracked content and Git history for credentials, personal paths, callback/session IDs, queue IDs, peer listings, email addresses, and transcripts.
- Confirm that examples do not imply authentication, authorization, guaranteed delivery, remote support, or vendor endorsement.
- Render the Markdown and validate every local and external link.

## Technical validation

- Install the declared dependencies in a clean supported Python environment.
- Run the full offline test suite.
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

- Create the remote only after the owner confirms account, name, visibility, and license.
- Use authenticated Git tooling; never place a token in a remote URL.
- Push the reviewed commit, then verify the remote default branch and commit hash independently.
- Configure least-privilege Actions permissions before enabling workflows.
- Review secret scanning, push protection, dependency alerts, branch protection, and private vulnerability reporting.
- Inspect the rendered public repository as a logged-out visitor.

Record source validation, Git commit, remote creation, push, and public visibility as separate evidence. None substitutes for the next.
