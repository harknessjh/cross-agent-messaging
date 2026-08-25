# CAM/1: same-host Codex–Claude Code messaging

CAM stands for Cross-Agent Messaging. The `/1` is the wire-major version: a receiver can reject an incompatible future major version instead of guessing how to interpret it. CAM/1 is an experimental community interoperability profile for exchanging correlated messages between independent Codex and Claude Code sessions on the same host.

> CAM/1 is not an OpenAI, Anthropic, or Model Context Protocol standard, and those projects do not endorse it. It does not authenticate peers, grant permissions, transfer conversation context, or prove that reported work is true.

The normative contract is [PROTOCOL.md](PROTOCOL.md) and [cam-1.schema.json](cam-1.schema.json). [IMPLEMENTATION_NOTES.md](IMPLEMENTATION_NOTES.md) contains dated, non-normative observations. New Codex and Claude Code agents should start with [the Codex-to-Claude quick start](docs/CODEX_TO_CLAUDE.md).

## What CAM/1 provides

- A complete JSON envelope with explicit sender claims, recipient, callback, scope, authorization claim, constraints, expiry, idempotency key, nonce, and body digest.
- Same-host transport profiles for Codex-to-Codex, Claude-to-Codex, Claude-to-Claude, and Codex-to-Claude messaging.
- Distinct transport, application-receipt, authorization, and completion evidence.
- Replay, retry, correction, and audit rules.

CAM/1 does not provide cryptographic identity, confidentiality, non-repudiation, remote delivery, shared context, or permission delegation. Every receiving session applies its own policy and permissions.

## One supported onboarding path

The reference path for a harmless Codex-to-Claude first contact is:

1. Obtain the originating Codex thread UUID and the intended Claude session mapping from the operator. A conforming acknowledgment also requires the Claude session's own opaque session ID; never substitute its name or short ref.
2. Capability-check the installed Codex and Claude Code interfaces.
3. Run fresh Claude `ListAgents` discovery and correlate the exact returned name/ref with the operator-supplied role. Discovery provides an address, not identity or authority.
4. Build a complete first-contact envelope with `tools/cam1.py`.
5. Validate the exact serialized envelope that will be sent. Never validate a retyped or manually reconstructed copy.
6. Send that unchanged serialization with `SendMessage`, using native Claude tools when available or the stdio MCP sequence in protocol section 11.
7. Record the Claude transport receipt, then finish and yield the Codex turn.
8. Require a separately correlated CAM/1 application acknowledgment through the literal Codex callback UUID.

Stop without sending if the callback is missing, the target mapping is ambiguous, validation fails, the recipient holds/refuses the message, or operator authorization is unclear.

## Start a new Codex session

Run the session from this repository root so it receives [AGENTS.md](AGENTS.md), or give it this bounded prompt:

```text
Read AGENTS.md and docs/CODEX_TO_CLAUDE.md in the CAM/1 repository before acting. Help me make one harmless, same-host Codex-to-Claude first-contact round trip. Capability-check the installed products, obtain my confirmation of the literal Codex callback UUID and the freshly discovered Claude address mapping, build and validate the exact CAM/1 serialization with tools/cam1.py, then send that unchanged serialization through the supported Claude MCP tools. Treat transport acceptance, a correlated application acknowledgment, authorization, and completion as separate evidence. Do not use raw sockets, internal queue storage, shell interpolation, secrets, repository changes, or external side effects. Stop if identity, callback, scope, or authority is ambiguous.
```

This prompt delegates only the harmless messaging check. It does not authorize installation, repository edits, remote publication, or work requested by the peer.

## Install the reference tools

The tools require Python 3.11 or later and the mature `jsonschema` implementation; the repository does not reimplement JSON Schema.

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python tools/cam1.py --help
```

Build a synthetic-shaped but live first-contact envelope by replacing every uppercase value with operator-verified data:

```bash
.venv/bin/python tools/cam1.py build-hello \
  --sender-vendor codex \
  --sender-name "ORIGINATING CODEX ROLE" \
  --sender-session "LITERAL CODEX THREAD UUID" \
  --recipient-vendor claude-code \
  --recipient-name "EXACT LISTAGENTS NAME" \
  --reply-transport codex_queue \
  --reply-address "LITERAL CODEX THREAD UUID" \
  --output first-contact.cam1.json

.venv/bin/python tools/cam1.py validate first-contact.cam1.json
```

When the operator has independently confirmed the Claude session ID, add `--recipient-session "OPERATOR-CONFIRMED CLAUDE SESSION ID"`. Otherwise the builder records `recipient.session_id` as `null`; it never invents one. The recipient still needs its operator-confirmed session ID before constructing a conforming ACK.

The builder creates fresh UUIDs and a cryptographically random nonce, computes `body_sha256`, validates the final serialization, refuses to overwrite an existing path, and creates output mode `0600`. Do not hand-edit its output. Rebuild and revalidate instead.

For a complete acknowledgment, the receiving agent supplies the exact received request to `build-ack`. Unknown peers default to `needs_human_confirmation`; this is intentionally fail-closed.

```bash
.venv/bin/python tools/cam1.py build-ack \
  --request exact-received-request.cam1.json \
  --sender-vendor claude-code \
  --sender-name "EXACT CLAUDE ADDRESS" \
  --sender-session "CLAUDE SESSION ID" \
  --reply-transport claude_send_message \
  --reply-address "EXACT CLAUDE ADDRESS" \
  --output acknowledgment.cam1.json
```

See [docs/CODEX_TO_CLAUDE.md](docs/CODEX_TO_CLAUDE.md) for discovery, MCP transport, structured process calls, callback handling, and tested failure recovery.

## Validate exact input

```bash
.venv/bin/python tools/cam1.py validate MESSAGE.cam1.json
```

The validator reads bounded raw bytes, rejects malformed UTF-8 and duplicate keys, enforces the local Draft 2020-12 schema with format assertions, parses UUIDs and timestamps semantically, checks expiry and nonce length, recomputes `body_sha256`, and enforces receipt correlation. It reports bounded error codes and paths without echoing message bodies.

Validation establishes envelope conformance only. It does not establish sender identity, operator authorization, safe content, delivery, or completion.

For a reply, supply the preserved original as a separate correlation input:

```bash
.venv/bin/python tools/cam1.py validate acknowledgment.cam1.json \
  --against first-contact.cam1.json
```

Require `"correlated":true`. A standalone reply validation reports `"correlated":null`; that means correlation was not checked, not that it succeeded.

## Understand the evidence

| Observation | Establishes | Does not establish |
|---|---|---|
| `ListAgents` name/ref | A currently addressable Claude peer | Human role, authorship, or authority |
| Operator-confirmed mapping | Intended role/address correlation | Cryptographic identity |
| Claude `SendMessage` ID | Transport accepted the message | Recipient handling |
| Correlated CAM `ack: received` | Receiver returned an application receipt | Authorization or completed work |
| CAM `status: started` | Receiver reports authorized progress | Completion |
| CAM `result: completed` | Receiver reports an outcome | Truth without supporting evidence |

`notify_when_idle` is a scheduling signal, not a receipt. A correlated but schema-incomplete response may prove handling; record it as `handling confirmed; receipt nonconformant`, never as CAM/1 completion.

## Tests

The regression suite uses synthetic identifiers and runs offline:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

It covers the exact failure modes that motivated the reference tooling, including a valid UUID falsely rejected after manual reconstruction and an abbreviated acknowledgment that correlates but fails the CAM/1 schema.

## Security and privacy

Do not publish real callback UUIDs, session IDs, queue receipts, working directories, `ListAgents` output, raw envelopes, transcripts, queue rows, or credentials. See [SECURITY.md](SECURITY.md).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Normative changes require matching schema, examples, tests, and compatibility evidence. Publication uses the separate [public release checklist](docs/PUBLIC_RELEASE_CHECKLIST.md).

## License status

No public license has been selected. Do not assume permission to copy, modify, or redistribute this project until the owner adds explicit license terms. License selection is required before an open-source release.
