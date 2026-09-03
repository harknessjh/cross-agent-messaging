# Causal ordering

> **Audience:** operators and maintainers enabling CAM's optional causal-ordering
> feature. New users should begin with [START HERE](../START_HERE.md).

CAM's optional causal-ordering gate uses the one shared, canonical Git-bound
project journal under the same operating-system account. It holds a
post-activation request or cancel when that journal shows potentially
dispatched recipient work which the sender's recorded frontier omits. It is an
awareness check, not a trust decision.

The feature uses journal format `CAM-CAUSAL/1` and reader capability
`causal.ordering/1`. It does **not** change the CAM/1 envelope schema or add
fields to messages sent through Codex or Claude Code.

The causal context exists only in that canonical journal. It is not portable
across copied or separate journals, and it is not recorded for sends that
bypass the project-aware adapter. Every participant in an enforced
conversation must therefore use the same project binding and journal.

## Activate it for a project

The compatibility kernel must already be active. From one clean CAM checkout,
create a plan for `causal.ordering` version `1`, record readiness for every
participant frozen by that plan, and activate it:

```bash
.venv/bin/python tools/cam1_project.py \
  --project-root /ABSOLUTE/PATH/TO/PROJECT \
  compatibility plan \
  --feature-id causal.ordering \
  --feature-version 1 \
  --expires-at FUTURE_UTC_TIMESTAMP \
  --operator-reference "HOW THE OPERATOR APPROVED THIS PLAN"
```

Use the returned plan UUID with `compatibility ready` for each participant and
then `compatibility activate`. The complete staged-upgrade procedure and its
clean-profile requirements are in [COMPATIBILITY.md](COMPATIBILITY.md).

Activation affects only conversations whose first outbound intent is later
than the activation record. Conversations begun before activation remain
grandfathered, including later replies, cancels, and renewals in those
conversations.

## What the journal records

Every enforced `message.outbound.intent` records the explicit recipient
participant UUID and a `causal_context` containing:

- `conversation_id`: the root message UUID;
- `depends_on`: exactly the `in_reply_to` UUID for a reply or cancel;
- `supersedes`: exactly the predecessor root UUID for a renewal; and
- `recipient_frontier`: the latest known, recipient-authored messages in that
  same two-party conversation.

New roots use their own message UUID as `conversation_id`. Replies, cancels,
and renewals inherit the existing conversation. Each reference array is
sorted, unique, and limited to 64 canonical UUIDs. The implementation walks
ancestry iteratively and remains bounded by the existing project-journal size
and record-count limits.

The send adapter derives the frontier only from valid inbound records authored
by the intended recipient. It does not accept a caller-supplied frontier and
does not use raw observations, global lifecycle state, or third-party traffic.
An exact transport retry copies the original causal context even if newer
inbound traffic now exists; a retry never silently changes what the original
message claimed to know.

## Receiving a delayed instruction

After ordinary envelope and roster validation and the existing lifecycle
expiry determination, ingest compares each post-activation request or cancel
with the receiver's current same-conversation outbound frontier. It does this
before applying any otherwise-eligible lifecycle transition or interpreting
the requested action. An outbound item is considered potentially dispatched
unless its only recorded outcome is `transport.not_accepted` with
`delivery_state: not_attempted`.

If the instruction omits a required frontier item, ingest:

- appends one `message.inbound.validated` record with assessment
  `held_for_clarification`;
- returns exit status `4` and error code `causal.stale_instruction`;
- reports `action_authorized: false` and `lifecycle_committed: false`; and
- leaves lifecycle and application state unchanged.

An exact redelivery remains held and does not add a second validation record.
It cannot retroactively become current. The sender must construct a fresh
envelope with a new message UUID and send it through the project-aware adapter,
which derives a new frontier. Do not edit or retransmit the held bytes as a
correction.

Malformed, missing, conflicting, or internally inconsistent journal context
also fails closed with a specific `causal.*` diagnostic rather than being
reported as stale. Causal ordering never grants authority, changes permissions,
or overrides lifecycle rules. There is deliberately no global rule that a
terminal lifecycle outcome outranks causality: ordering is assessed first; a
causally current message must still pass the ordinary lifecycle checks.

A `current` or `held_for_clarification` assessment is neither trust nor
authority. It governs only interpretation of that CAM action and does not
constrain unrelated work by either participant.
