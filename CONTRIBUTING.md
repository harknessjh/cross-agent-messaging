# Contributing

CAM/1 is an experimental interoperability draft. Contributions should keep the public surface small, auditable, vendor-neutral, and safe by default.

## Before changing the protocol

- Identify whether the change is normative, transport-specific, or non-normative implementation guidance.
- Keep transport reachability, sender identity, operator authorization, and completion evidence distinct.
- Use synthetic identifiers, paths, receipts, and message bodies in every public artifact.
- Update the schema, examples, reference tools, and tests together when the wire contract changes.
- Cite dated, primary vendor documentation or exact source revisions for capability claims.
- Do not add a license or legal terms without the repository owner's explicit decision.

## Validation

From an isolated Python 3.11 or newer environment:

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python tools/cam1.py validate tests/fixtures/valid-hello.json --allow-expired
python tools/cam1.py validate tests/fixtures/valid-ack.json \
  --against tests/fixtures/valid-hello.json \
  --allow-expired
```

Also render the Markdown, validate local links, scan for private identifiers and credentials, and follow the [public release checklist](docs/PUBLIC_RELEASE_CHECKLIST.md) before publication.

## Commit messages

Use short, plain messages focused on the change. Do not include automated attribution, tool attribution, agent/session links, or co-author trailers.

## Security reports

Do not put sensitive routing metadata or exploit details in a public issue. Follow [SECURITY.md](SECURITY.md).
