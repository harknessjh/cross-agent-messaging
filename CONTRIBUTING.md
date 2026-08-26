# Contributing

CAM/1 is an experimental interoperability draft. Contributions should keep the public surface small, auditable, vendor-neutral, and safe by default.

## Before changing the protocol

- Identify whether the change is normative, transport-specific, or non-normative implementation guidance.
- Keep transport reachability, sender identity, operator authorization, and completion evidence distinct.
- Use synthetic identifiers, paths, receipts, and message bodies in every public artifact.
- Update the schema, examples, reference tools, and tests together when the wire contract changes.
- Keep the canonical Codex sender and Claude receiver prompts in [the quick start](docs/CODEX_TO_CLAUDE.md); link to them instead of copying variants into other documents.
- Keep `tools/cam1_transport.py` narrowly local and send-only. Do not add receive, polling, retry, daemon, database, board, raw-socket, or remote behavior. Reply envelopes must be checked against the exact preserved original before transport.
- Cite dated, primary vendor documentation or exact source revisions for capability claims.
- Do not add a license or legal terms without the repository owner's explicit decision.

## Validation

Reproduce CI from an isolated Python 3.11, 3.12, 3.13, or 3.14 environment:

```bash
python -m pip install --requirement requirements-dev.txt
python -m ruff format --check tools tests
python -m ruff check --select E4,E7,E9,F,I,B,UP --target-version py311 tools tests
python -m unittest discover --start-directory tests --verbose
python tools/cam1.py --help
python tools/cam1_transport.py --help
python tools/cam1.py validate tests/fixtures/valid-hello.json --allow-expired
python tools/cam1.py validate tests/fixtures/valid-ack.json \
  --against tests/fixtures/valid-hello.json \
  --allow-expired
```

Also render the Markdown, validate local links, scan for private identifiers and credentials, confirm that onboarding uses a private exchange directory outside the repository, and follow the [public release checklist](docs/PUBLIC_RELEASE_CHECKLIST.md) before publication. Do not run a live transport test unless the operator separately authorizes its exact same-host recipient, callback, and harmless scope.

## Commit messages

Use short, plain messages focused on the change. Do not include automated attribution, tool attribution, agent/session links, or co-author trailers.

## Security reports

Do not put sensitive routing metadata or exploit details in a public issue. Follow [SECURITY.md](SECURITY.md). Public release remains blocked until the owner replaces any private-reporting placeholder with a tested private route; contributors must not invent an address.
