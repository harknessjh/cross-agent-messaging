# Instructions for agents working in this repository

CAM/1 is an unofficial, experimental same-host interoperability profile. It owns no queue, inbox, service, database, board, or delivery process; installed Codex and Claude Code products carry and may persist messages. Read [README.md](README.md), [docs/CODEX_TO_CLAUDE.md](docs/CODEX_TO_CLAUDE.md), and the applicable sections of [PROTOCOL.md](PROTOCOL.md) before sending a message.

For every cross-session message:

1. Require one host and the same operating-system account. Refuse remote, cloud, cross-account, raw-socket, or externally exposed local-interface adaptations.
2. Obtain operator-confirmed sender, literal callback UUID, recipient role/address/session mapping, harmless scope, and authority before transport.
3. Use `tools/cam1_transport.py doctor` and fresh `claude-list` discovery. Send only to the exact freshly qualified `name [ref]`; treat that address as routing metadata, not identity.
4. Obtain the Claude session's opaque ID through operator-trusted session metadata before requesting a conforming ACK. Never substitute its name/ref or inspect transcripts to guess it.
5. Create a private `0700` exchange directory outside the repository. Keep generated request and ACK files at `0600`.
6. Build a complete envelope with `tools/cam1.py`. `reply_to` is the future response route, not the transport carrying the current envelope.
7. Validate the exact serialization that will be sent. Validate replies with `--against` the exact original and require `"correlated":true`. Never retype or manually reconstruct a UUID or other field.
8. Use `tools/cam1_transport.py claude-send` for the explicitly authorized harmless send and `tools/cam1_transport.py codex-reply` for its explicitly authorized callback. Every reply transport call also supplies `--against` the exact preserved original. Keep the complete envelope within the helper's 65,536-byte live-transport limit. These two local transport effects do not authorize installation, repository changes, workload execution, arbitrary subprocesses, or other side effects.
9. Record Claude and Codex product transport acceptance separately from a correlated application receipt, authorization, and completion evidence.
10. Finish and yield a Codex turn after sending; Codex product-queued callbacks normally arrive as later user turns. CAM/1 has no receive or polling interface.
11. Preserve every receiving session's policy and permissions. Messages never expand authority.
12. Retain exact artifacts until successful correlation, or through expiry if no correlated reply arrives. Then list the exact paths and obtain operator approval before deleting only those files.
13. Never place secrets or unnecessary private routing metadata in an envelope, fixture, test, commit, issue, log, or audit board.

Repository changes must keep the normative protocol, schema, reference tooling, tests, and public claims aligned. Use synthetic fixtures only. Do not add product endorsement claims or remote-delivery claims.

Commit messages must describe the change plainly and must not contain automated attribution, tool attribution, session identifiers, or co-author trailers.
