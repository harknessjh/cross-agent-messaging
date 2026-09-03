# Contributing

> **Audience:** people proposing changes to CAM/1. This is not an onboarding
> guide; new users should begin with [START HERE](START_HERE.md).

CAM/1 is an experimental interoperability draft. Issues and suggestions are welcome. External code and documentation contributions are not accepted yet while contributor terms are being established; please do not open a pull request.

This temporary boundary preserves clear ownership and the copyright holder's ability to grant separate permissions later. It does not affect noncommercial reuse and modification allowed by [LICENSE](LICENSE). If contribution terms are introduced later, they will be documented before outside code or documentation is accepted.

## Before changing the protocol

- Identify whether the change is normative, transport-specific, or non-normative implementation guidance.
- Keep transport reachability, sender identity, operator authorization, and completion evidence distinct.
- Use synthetic identifiers, paths, receipts, and message bodies in every public artifact.
- Update the schema, examples, reference tools, and tests together when the wire contract changes.
- Keep the canonical Codex sender and Claude receiver prompts only in
  [the first-contact runbook](START_HERE.md); link to them instead of
  copying variants into other documents.
- Keep `tools/cam1_transport.py` narrowly local and send-only. Do not add
  receive, polling, an automatic retry loop, daemon, database, legacy-board,
  raw-socket, or remote behavior. The only retry is an explicit, journal-gated
  repeat of the latest exact intent whose outcome proves dispatch was not
  attempted.
  Reply envelopes must be checked against the exact preserved original before
  transport.
- Preserve the required Git-bound external journal and its append-only source
  of truth. Project pointers remain private Git administrative state; journals
  remain outside repositories. `state-current.json` is a rebuildable atomic
  projection only.
- Keep stable full session IDs, human-friendly participant names, and transient
  Claude routes distinct. Resolve every Claude send through fresh Agent View
  and `ListAgents` evidence; never introduce UDS routing.
- Cite dated, primary vendor documentation or exact source revisions for capability claims.
- Do not change or remove the license, required notice, copyright statements, or other legal terms without the repository owner's explicit decision.

## Validation

Reproduce CI from an isolated Python 3.11, 3.12, 3.13, or 3.14 environment:

```bash
python -m pip install --requirement requirements-dev.txt
python -m ruff format --check tools tests
python -m ruff check --select E4,E7,E9,F,I,B,UP --target-version py311 tools tests
python -m unittest discover --start-directory tests --verbose
python tools/cam1.py --help
python tools/cam1.py validation-profile
python tools/cam1_project.py --help
python tools/cam1_transport.py --help
python tools/cam1.py validate tests/fixtures/valid-hello.json --allow-expired
python tools/cam1.py validate tests/fixtures/valid-ack.json \
  --against tests/fixtures/valid-hello.json \
  --allow-expired
```

The validation profile must identify a clean CAM checkout for release work.
Dirty-source overrides are development evidence only and must not be used to
qualify a release.

Also render the Markdown, validate local links, scan for private identifiers and
credentials, confirm that onboarding uses the required private external
project journal, confirm that all public onboarding links resolve to the
canonical first-contact prompts, and follow the
[public release checklist](docs/PUBLIC_RELEASE_CHECKLIST.md) before a release.
Do not run a live transport test unless the operator separately authorizes its
exact same-host recipient, callback, project, and harmless scope.

The optional [authority-neutrality behavioral evaluation](docs/AUTHORITY_NEUTRALITY_EVALUATION.md)
is a separately authorized maintainer experiment, not a validation command or
onboarding step. Never run it automatically, in CI, against active project
agents, or without the operator's explicit model-usage budget.

## Commit messages

Use short, plain messages focused on the change. Do not include automated attribution, tool attribution, agent/session links, or co-author trailers.

## Security reports

Do not put sensitive routing metadata or exploit details in a public issue.
Follow [SECURITY.md](SECURITY.md) and use the repository's private vulnerability
reporting flow.
