# CAM/1: same-host Codex–Claude Code messaging

CAM stands for Cross-Agent Messaging. The `/1` is the wire-major version: a receiver can reject an incompatible future major version instead of guessing how to interpret it. CAM/1 is an experimental community interoperability profile for exchanging correlated messages between independent Codex and Claude Code sessions on the same host.

> CAM/1 is not an OpenAI, Anthropic, or Model Context Protocol standard, and those projects do not endorse it. It does not authenticate peers, grant permissions, transfer conversation context, or prove that reported work is true.

The normative contract is [PROTOCOL.md](PROTOCOL.md) and [cam-1.schema.json](cam-1.schema.json). [IMPLEMENTATION_NOTES.md](IMPLEMENTATION_NOTES.md) contains dated, non-normative observations. New users should follow the single supported [Codex-to-Claude quick start](docs/CODEX_TO_CLAUDE.md).

## Mental model and scope

CAM/1 is an envelope format and safety profile, with local reference tools for building, validating, and invoking separately installed product transports. CAM/1 does not run a broker, daemon, queue, inbox, database, coordination board, or delivery service. Claude Code owns Claude discovery and delivery. Codex owns `codex queue` and any product-internal persistence or later-turn delivery it performs. Any optional audit log is operator-owned and is not a transport or source of authority.

CAM/1 provides:

- a complete JSON envelope with explicit sender claims, recipient, callback, scope, authorization claim, constraints, expiry, idempotency key, nonce, and body digest;
- same-host profiles that map the envelope onto Codex and Claude Code product transports;
- distinct transport, application-receipt, authorization, and completion evidence; and
- replay, retry, correction, and audit rules.

CAM/1 does not provide cryptographic identity, confidentiality, non-repudiation, remote delivery, shared context, or permission delegation. Every receiving session applies its own policy and permissions.

The supported onboarding path is local-only: both sessions run on one host under the same operating-system account. Do not adapt it to Remote Control, cloud sessions, different accounts, another machine, or an exposed MCP, app-server, queue, or runtime-socket endpoint.

## Prerequisites

Before prompting either agent, obtain or prepare:

- a POSIX environment; the reference round trip was tested on macOS;
- Python 3.11, 3.12, 3.13, or 3.14;
- installed `codex` and `claude` commands whose live capability checks succeed;
- one originating Codex session and one independent Claude Code session on the same host and account;
- the literal Codex thread UUID;
- an operator-trusted Claude session ID; and
- authority for one harmless first-contact send and its one same-host callback.

A fresh Claude `ListAgents` result supplies a transport address, not a human role, session identity, or authority. The operator must correlate the returned name/ref with the intended Claude session.

## Install and test before prompting agents

The reference tools use the mature `jsonschema` implementation for envelope validation and the official Python MCP SDK for the bounded Claude stdio client.

```bash
python3 --version  # must report a supported 3.11-3.14 version
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python tools/cam1.py --help
.venv/bin/python tools/cam1_transport.py --help
.venv/bin/python tools/cam1_transport.py doctor
```

If `python3` is outside the supported range, replace it in the first two lines
with an installed compatible command such as `python3.12`.

Do not proceed unless the offline tests finish with `OK`, the local transport doctor described in the [quick start](docs/CODEX_TO_CLAUDE.md) confirms its prerequisite checks, and `claude-list` confirms the current local Claude messaging tool surface.

## One supported onboarding path

The complete walkthrough, including the canonical [Claude receiver prompt](docs/CODEX_TO_CLAUDE.md#5-prepare-the-claude-receiver) and [Codex sender prompt](docs/CODEX_TO_CLAUDE.md#6-start-the-codex-sender), lives in the quick start. In outline:

1. Create a private `0700` exchange directory outside the repository.
2. Prepare the Claude receiver with its operator-trusted session ID and expected Codex callback.
3. Start Codex from this repository root and give it the canonical sender prompt.
4. Run local capability checks and fresh Claude discovery.
5. Have the operator confirm the literal callback UUID and exact Claude role/address/session mapping.
6. Build and validate one complete first-contact envelope with `tools/cam1.py`.
7. Send those unchanged bytes with `tools/cam1_transport.py claude-send`.
8. Record the Claude transport receipt, then finish and yield the Codex turn.
9. Have Claude build, validate, and return one complete ACK with `tools/cam1_transport.py codex-reply --against ORIGINAL`.
10. When Codex receives the later user turn, validate it with `--against` the exact original and require `"correlated":true`.

`reply_to` always describes the future response route for the envelope being built. It is not the transport carrying that envelope. The hello travels through Claude `SendMessage` while its `reply_to` names `codex_queue`; the ACK travels through `codex queue` while its own `reply_to` names `claude_send_message`.

Stop without sending if the callback is missing, discovery is not local-only, the target mapping is ambiguous, validation fails, the recipient holds or refuses the message, or operator authorization is unclear.

The reference live transports accept complete envelopes up to 65,536 UTF-8 bytes. Offline validation permits larger stored envelopes, but larger artifacts should be handed off by operator-approved local path and digest rather than embedded in a live message.

## Expected evidence

| Observation | Establishes | Does not establish |
|---|---|---|
| Local doctor success | Required commands and helper dependencies pass prerequisite checks | Claude messaging tools, peer identity, or delivery |
| `ListAgents` name/ref | A currently addressable Claude peer | Human role, session identity, authorship, or authority |
| Operator-confirmed mapping | Intended role/address/session correlation | Cryptographic identity |
| Fresh hello validation | Envelope structure, freshness, body digest, and local semantic checks passed | Identity, delivery, or authorization |
| Claude `SendMessage` ID | Claude transport accepted the message | Recipient handling |
| Codex queue receipt | Codex product accepted the callback for its queue | Later-turn delivery or handling |
| Correlated CAM `ack: received` | Receiver returned an application receipt | Authorization or completed work |
| CAM `status: started` | Receiver reports authorized progress | Completion |
| CAM `result: completed` | Receiver reports an outcome | Truth without supporting evidence |

A fresh hello validation reports `"structurally_valid":true`, `"fresh":true`, `"body_hash_valid":true`, and `"correlated":null`. A valid ACK checked with `--against` reports `"correlated":true`. Standalone reply validation reports `"correlated":null`; that means correlation was not checked, not that it succeeded.

`notify_when_idle` is a scheduling signal, not a receipt. A correlated but schema-incomplete response may prove handling; record it as `handling confirmed; receipt nonconformant`, never as CAM/1 completion.

## Temporary artifacts

Generated request and ACK files contain capability-like routing metadata. Create them only in the private exchange directory; the builders create new output files with mode `0600` and refuse to overwrite existing paths. Preserve the exact bytes until successful callback correlation, or through expiry if no correlated reply arrives. Retain only the state needed for correlation and idempotency.

The generic offline validator can inspect a selected regular file or stdin; that result establishes envelope conformance, not filesystem provenance. The reference file helper refuses a final-component symlink and opens path inputs nonblocking before accepting only regular files, but it does not authenticate an arbitrary path's ancestor chain. The operator-provided private `0700` exchange directory remains part of the supported security boundary. Same-user filesystem substitution is not a property CAM/1 can prevent.

After correlation or expiry makes the files eligible for cleanup, list the exact paths to the operator. Cleanup begins only after the operator verifies the resolved private directory and approves those paths; remove no unexpected contents and use no glob or recursive deletion. Do not promise secure erasure. Do not commit, publish, paste into an issue, or copy raw envelopes into an audit board. If durable audit is required, store only approved sanitized metadata in a separately managed log.

## Validate exact input

```bash
.venv/bin/python tools/cam1.py validate MESSAGE.cam1.json
.venv/bin/python tools/cam1.py validate ACK.cam1.json \
  --against ORIGINAL.cam1.json
```

The validator reads bounded raw bytes, rejects malformed UTF-8 and duplicate keys, enforces the local Draft 2020-12 schema with format assertions, parses UUIDs and timestamps semantically, checks expiry and nonce length, recomputes `body_sha256`, and enforces receipt correlation. It reports bounded error codes and paths without echoing message bodies.

Validation establishes envelope conformance only. It does not establish sender identity, operator authorization, safe content, delivery, or completion.

## Security and privacy

Do not publish real callback UUIDs, session IDs, queue receipts, working directories, `ListAgents` output, raw envelopes, transcripts, queue rows, or credentials. See [SECURITY.md](SECURITY.md).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Normative changes require matching schema, examples, tests, and compatibility evidence. Publication uses the separate [public release checklist](docs/PUBLIC_RELEASE_CHECKLIST.md).

## License and copyright

Copyright © 2026 John Harkness.

This repository is source-available for noncommercial use under the [PolyForm Noncommercial License 1.0.0](LICENSE). Subject to those terms and preservation of the required notice in [NOTICE](NOTICE), you may use, copy, modify, and distribute the project for noncommercial purposes.

Commercial use is not licensed. Contact the copyright holder to request separate written permission. No commercial license, price, royalty, or other commercial terms are offered by this repository.

This is a noncommercial source-available project, not Open Source software as defined by the Open Source Initiative. Public visibility does not by itself grant rights beyond the project license. GitHub's [platform terms](https://docs.github.com/en/site-policy/github-terms/github-terms-of-service) separately govern GitHub's service and AI-training uses and GitHub users' in-service viewing and forking.
