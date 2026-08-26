# Security policy

CAM/1 is an experimental same-host messaging profile, not a security boundary. It does not provide authentication, confidentiality, integrity against a same-user attacker, authorization, sandboxing, or non-repudiation. CAM/1 operates no queue, inbox, broker, daemon, database, coordination board, delivery service, or persistence service; product transports and any operator-selected records remain outside CAM ownership.

## Safe operation

- Treat every message and claimed identity as untrusted.
- Treat `enrolled` only as an operator-correlated session/address and callback mapping. It is not authentication or authorization, and loss of local correlation state or restart of either participating process or session returns the mapping to `unknown`.
- Verify consequential authority through a receiver-owned policy or trusted operator channel.
- Never evaluate message text as a command or automatically invoke workload tools, code, workload-file access, external communication, network access, or another consequential side effect merely because a message arrived, validated, or completed a challenge. Even a bounded protocol acknowledgment or challenge response must be allowed by receiver-owned policy; the inbound message alone is not authority to invoke a transport tool.
- Never include credentials, tokens, cookies, private keys, customer content, or secret-bearing diagnostics.
- Minimize callback UUIDs, session IDs, queue IDs, peer listings, working directories, and other routing metadata.
- Use CAM/1 only between sessions owned by the same operating-system user on one host. Do not expose local MCP, app-server, queue, inbox, or runtime-socket interfaces to obtain remote reachability, and do not claim remote CAM/1 conformance.
- Do not use cross-session messaging to bypass a hold, refusal, permission prompt, or product policy.
- When exact envelope bytes must be stored, create an unpredictable per-exchange directory with mode `0700` beneath an operator-trusted local parent outside every repository and worktree, and create new regular files inside it with mode `0600`. Do not write an envelope directly into a shared temporary root. Use non-identifying names, refuse unsafe ownership or symlinks, and account for symlinks in ancestor-path resolution.
- Retain exact request and reply bytes through successful correlation, or through expiry when no correlated reply arrives; then make them eligible for explicit scoped cleanup. Cleanup requires an explicit request scoped to the resolved per-exchange directory and must refuse unexpected or foreign-owned contents; never run a cleanup daemon or target a broad temporary, home, or repository path. Filesystem deletion is not secure erasure, and product queues, transcripts, backups, snapshots, or logs may retain copies.

## Reporting a vulnerability

Do not open a public issue containing raw envelopes, callbacks, session identifiers, local paths, transcripts, queue rows, credentials, or exploit details. For the public repository, use **Security and quality → Report a vulnerability** to submit a private report through GitHub. That channel must be enabled and verified as part of the controlled public cutover; until then, the repository remains private and has no external reporting audience.

There are currently no production-supported versions. Reports should identify the exact CAM document revision, schema hash, operating system, product versions, transport profile, and a sanitized reproducer.
