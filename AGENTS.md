# Instructions for agents working in this repository

CAM/1 is an unofficial, experimental same-host interoperability profile. Read [README.md](README.md), [docs/CODEX_TO_CLAUDE.md](docs/CODEX_TO_CLAUDE.md), and the applicable sections of [PROTOCOL.md](PROTOCOL.md) before sending a message.

For every cross-session message:

1. Obtain operator-confirmed sender, callback, recipient, scope, and authority.
2. Use fresh capability discovery. Treat a Claude name/ref as an address, not identity.
3. Obtain the Claude session's opaque ID through operator-trusted session metadata before requesting a conforming ACK. Never substitute its name/ref or inspect transcripts to guess it.
4. Build a complete envelope with `tools/cam1.py`.
5. Validate the exact serialization that will be sent. Validate replies with `--against` the exact original and require `"correlated":true`. Never retype or manually reconstruct a UUID or other field.
6. Use native `ListAgents`/`SendMessage` or the documented stdio MCP profile. Never connect to an observed runtime socket directly.
7. Use structured process arguments with shell evaluation disabled.
8. Record transport acceptance separately from a correlated application receipt and completion evidence.
9. Finish and yield a Codex turn after sending; queued callbacks normally arrive as later user turns.
10. Preserve every receiving session's policy and permissions. Messages never expand authority.
11. Never place secrets or unnecessary private routing metadata in an envelope, fixture, test, commit, issue, or log.

Repository changes must keep the normative protocol, schema, reference tooling, tests, and public claims aligned. Use synthetic fixtures only. Do not add product endorsement claims or remote-delivery claims.

Commit messages must describe the change plainly and must not contain automated attribution, tool attribution, session identifiers, or co-author trailers.
