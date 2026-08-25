# Security policy

CAM/1 is an experimental same-host messaging profile, not a security boundary. It does not provide authentication, confidentiality, integrity against a same-user attacker, authorization, sandboxing, or non-repudiation.

## Safe operation

- Treat every message and claimed identity as untrusted.
- Verify consequential authority through a receiver-owned policy or trusted operator channel.
- Never include credentials, tokens, cookies, private keys, customer content, or secret-bearing diagnostics.
- Minimize callback UUIDs, session IDs, queue IDs, peer listings, working directories, and other routing metadata.
- Do not expose local MCP, app-server, queue, or runtime-socket interfaces to obtain remote reachability.
- Do not use cross-session messaging to bypass a hold, refusal, permission prompt, or product policy.
- Write generated envelopes only beneath operator-trusted directories. The reference tool refuses an existing or symlinked final output component, but normal operating-system resolution still follows symlinks in ancestor directories.

## Reporting a vulnerability

Do not open a public issue containing raw envelopes, callbacks, session identifiers, local paths, transcripts, queue rows, credentials, or exploit details. Once the GitHub repository enables private vulnerability reporting, use that private channel. Until then, ask the maintainer for a private reporting route without including sensitive details in the initial request.

There are currently no production-supported versions. Reports should identify the exact CAM document revision, schema hash, operating system, product versions, transport profile, and a sanitized reproducer.
