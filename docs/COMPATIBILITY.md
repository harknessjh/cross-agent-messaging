# Compatibility upgrades

The CAM compatibility kernel records readiness for every frozen participant
binding before a new project-state feature becomes active. It helps prevent
one updated session from silently writing state that older sessions cannot
safely interpret.

The kernel uses the project-state format `CAM-COMPAT/1`. This is not a new
CAM/1 envelope version, an installer, an authentication mechanism, or authority
to execute work. The fixed contract is defined by
[`cam-compatibility-event-1.schema.json`](../schemas/cam-compatibility-event-1.schema.json).

## How activation works

An upgrade has three journaled stages:

1. **Plan.** `compatibility.upgrade.planned` is an inert, non-state event. It
   freezes the feature ID and version, bounded feature configuration, required
   reader epoch and capabilities, clean validation-profile digest, expiry,
   operator reference, and every non-retired participant's binding generation.
2. **Readiness.** Each frozen participant records a
   `compatibility.participant.ready` event linked to the exact plan record. It
   records the reader epoch, capabilities, binding generation, and the same
   validation-profile digest, together with how the operator confirmed this
   compatibility assertion.
3. **Activation.** `state.compatibility.gate_activated` is one fixed-header
   state event linked to the exact plan and readiness records. Before appending
   it, the reference implementation rechecks the plan, expiry, profile, full
   roster, binding generations, readiness set, and journal links.

The activation is atomic in the canonical journal: either the single gate
record is absent or the complete gate is active. Feature-specific
configuration remains in the referenced plan rather than being copied into
the activation header. `state-current.json.compatibility.active_gates` is a
sorted list of active fixed headers, each carrying its feature ID. Staged plans,
readiness, and configuration remain available through `compatibility status`.
Plan and readiness events do not refresh `state-current.json`; activation does.

Every plan and gate must require both `compatibility.kernel/1` and its own
`FEATURE_ID/FEATURE_VERSION` capability. For example, feature
`causal.ordering` version `1` requires `causal.ordering/1`. A participant cannot
record readiness until its reader advertises every required capability. This
prevents kernel support alone from being mistaken for support for an unknown
feature. See [CAUSAL_ORDERING.md](CAUSAL_ORDERING.md) for that feature's
journal semantics and held-message runbook.

## Commands

Run these commands from the selected CAM checkout. Replace every uppercase
placeholder and use the same absolute project root throughout.

Inspect staged plans, readiness, active gates, and any required reader upgrade:

```bash
.venv/bin/python tools/cam1_project.py \
  --project-root /ABSOLUTE/PATH/TO/PROJECT \
  compatibility status
```

Create one inert plan. The command generates a plan UUID unless `--plan-id` is
supplied:

```bash
.venv/bin/python tools/cam1_project.py \
  --project-root /ABSOLUTE/PATH/TO/PROJECT \
  compatibility plan \
  --feature-id compatibility.kernel \
  --feature-version 1 \
  --expires-at FUTURE_UTC_TIMESTAMP \
  --operator-reference "HOW THE OPERATOR APPROVED THIS PLAN"
```

Use `--feature-config-file /ABSOLUTE/PRIVATE/CONFIG.json` for a bounded JSON
configuration object. Supply a fresh RFC 3339 UTC expiry in
`YYYY-MM-DDTHH:MM:SSZ` form. It must be later than the plan record and no more
than seven days after it. If staging cannot finish in that window, create a new
plan rather than extending or reusing the expired one.
`--required-reader-epoch` replaces the current-epoch default; repeatable
`--required-capability` adds extra capabilities. The kernel and feature-version
capabilities are always included.

Record readiness once for each participant frozen in the plan:

```bash
.venv/bin/python tools/cam1_project.py \
  --project-root /ABSOLUTE/PATH/TO/PROJECT \
  compatibility ready \
  --plan-id PLAN_UUID \
  --participant PARTICIPANT_COMMON_NAME \
  --operator-reference "HOW THE OPERATOR CONFIRMED THIS READINESS" \
  --expected-validation-profile-sha256 PLAN_PROFILE_SHA256
```

After every frozen participant is ready, activate the plan exactly once:

```bash
.venv/bin/python tools/cam1_project.py \
  --project-root /ABSOLUTE/PATH/TO/PROJECT \
  compatibility activate \
  --plan-id PLAN_UUID \
  --operator-reference "HOW THE OPERATOR APPROVED ACTIVATION"
```

A plan retry reuses an existing record only when it supplies the same explicit
plan ID and identical effective content. Equivalent readiness retries require
the same operator reference and other content; only their new timestamp is
ignored. Activation reuse requires the same plan ID and operator reference.
Conflicting reuse, roster or profile drift, an expired plan, missing readiness,
and unsupported capabilities fail closed without an append.

The readiness participant selector accepts a common name or participant UUID.
Use these commands rather than manually appending their reserved event types.

## Clean-profile requirement

`compatibility plan`, `ready`, and `activate` require the complete selected CAM
Git checkout to be clean; these commands provide no dirty-source override. The
checkout must also have a concrete HEAD, with the profiled path set, bytes, and
index flags matching that commit. The plan freezes the resulting profile
digest; every readiness record and the activation header must match it. If the
checkout or profile changes during staging, create a new plan from the intended
clean checkout rather than activating mixed validation rules.

`compatibility status` is deliberately read-only and remains available when
the current checkout is dirty or the active gate requires a newer reader. An
unsupported active gate returns `compatibility.upgrade_required` with the
required epoch and missing capabilities. This expected outcome exits `2` and
writes structured JSON to stderr. Stateful paths that replay canonical state
remain blocked until a compatible checkout is used.

A `compatible` status means only that the narrow compatibility projection did
not find an unsupported active gate. The command verifies the complete journal
hash chain, then semantically replays only participant and compatibility events
through the first unsupported gate. It does not validate unrelated or
feature-specific state semantics and is not proof that ordinary state replay
will succeed. `state status` does not show compatibility details; use
`compatibility status`.

Readiness is an operator-confirmed, caller-recorded assertion associating the
invoking clean tool profile and capabilities with a selected frozen
participant binding. The kernel verifies the binding's participant ID and
generation, and all readiness records must name the plan's profile. The
required `--operator-reference` records how the operator confirmed that
association; the tool cannot authenticate the claim. Because any same-user
caller can select `--participant`, readiness does **not** prove that an
independent agent personally ran the command. Use it as shared tool-profile
compatibility evidence, not as a participant-attestation, trust, or
authorization signature.

## A committed gate with a stale projection

The journal is authoritative and its disposable projection is written
afterward. If activation commits but projection refresh fails, the CLI returns
success with:

```text
status: activated_projection_stale
projection_current: false
warning.code: state.projection_refresh
```

The gate is already active. Do not retry activation. Correct the local
projection-storage problem, then run `state rebuild` to regenerate
`state-current.json` from the journal.

## First-bootstrap limitation

Plans and readiness are non-state events, so readers predating the kernel skip
them safely. The first activation is necessarily different: a pre-kernel
reader sees an unknown `state.compatibility.gate_activated` event and can only
fail with its generic unknown-state error. It cannot produce the newer
actionable compatibility report.

Bootstrap with the `compatibility.kernel` version `1` plan shown above. Before
activating it, the operator must move every actual participant to a
kernel-capable clean checkout; readiness records cannot prove that this agent
migration happened independently. Once the kernel is active, later
fixed-header gates let compatible readers report precise upgrade requirements
through `compatibility status`.

Compatibility activation does not install an update, execute feature
configuration, authorize a message, or expand an agent's permissions. Those
remain separate operator and application responsibilities.
