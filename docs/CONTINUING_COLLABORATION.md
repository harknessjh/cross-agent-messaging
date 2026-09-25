# Continuing collaboration

> **Audience:** enrolled collaborators and operators continuing beyond the initial round trip. New users should begin with [START HERE](../START_HERE.md).

After the first hello/ACK, the operator may give each session continuing permission to exchange messages with its enrolled collaborators. Use ordinary language, name the peers and purpose, and set any desired duration or stop condition. Existing session permission rules still apply.

For a disposable discussion test, a direct prompt in each session can be:

```text
You may use CAM/1 to read, acknowledge, and send informational messages with ENROLLED_PEER about DISCUSSION_TOPIC for the next hour. Continue the discussion when a substantive reply is useful; do not acknowledge an ACK or create an automatic chat loop. This covers local CAM bookkeeping and transport calls, but authorizes no application-project inspection, edits, tests, installations, or external actions. Keep routine CAM mechanics in the background. Reuse this permission within its scope rather than asking again for each message; stop when the topic is resolved or the hour ends. Apply this session's existing permissions to any new request for work.
```

The placeholders identify a real enrolled peer and a topic chosen by the operator. This prompt is optional and is never run as part of onboarding. Real project sessions should use their existing task authority and the scope their operator intended.

Local permission to communicate is not an authority claim sent to a peer. Build ordinary discussion requests with `authorization.basis: none`; it means the message grants no authority for recipient work. Use a descriptive operation such as `discuss_proposal`, not `acknowledge` for a substantive question. ACKs to ordinary requests also use `none`. First-contact ACKs retain `first_contact`.

Requests for actual work still need the appropriate risk, scope, and authorization claim. The recipient evaluates them under its own instructions. A bounded grant to perform work may be represented by `delegated_scope`; a grant merely to communicate does not need to be repeated inside every envelope. Message expiry does not shorten or extend independent local permission.

For a requested outcome, use the normal accepted/status/result lifecycle and correlate replies against the preserved root. `received` confirms handling of the message, not completion of the requested work. A follow-up question may be a new root; it does not complete the earlier root by implication.

## Close the loop on requested work

In short: accept within your existing authority, then send the requester a result or error when the work finishes. If blocked, tell your operator and the peer when CAM works. Keep track of unfinished requests across turns. Never acknowledge an ACK.

Use this as receiver-owned working guidance within the session's existing permissions. It does not authorize new work, require another approval for already permitted communication, or extend the first-contact prompts beyond their final report. Keep unrelated authorized work moving.

- Accepting a request that asks for an outcome creates an open obligation to the originating enrolled requester. Before handing back completed work or ending the turn in which it finished, send `result: completed` with a concise summary and authorized evidence paths. If the work failed, send `error: failed` with the reason. An operator-chat summary is not a peer reply.
- If work continues beyond this turn, make sure the requester has been sent acceptance and, once work has begun, `status: started`. The obligation survives turn boundaries and delivery delays. Send meaningful progress, not timer-driven updates. Do not repeat `status: started`: after it, the next lifecycle reply is a result or error. Later progress can be a fresh informational follow-up linked to the received root, explicitly requesting no outcome.
- Before ending a turn, review your own list of accepted requests without a result or error, and close or update each as above. Track the root ID, requester, exact preserved root path, last sent reply/outcome and next action. Do not treat journal entries for other participants as your assignments. `cam1_project.py state status` gives aggregate lifecycle counts, not per-participant obligations; `journal tail` shows recent records for all participants. There is no per-participant outstanding view yet.
- This duty excludes ACKs and messages requesting no outcome. Never acknowledge an ACK or create a chat loop. Validation or `ack: received` alone is not acceptance of work.

Use the typed builders and validate against the exact preserved root; every lifecycle reply refers to that root, not an intervening ACK or status. See the [command reference](CODEX_TO_CLAUDE.md) and [legal lifecycle table](../PROTOCOL.md#legal-pairwise-message-transitions).

### Choose a legal reply

| Current request state | Your next reply |
| --- | --- |
| Pending; you will take the work under current authority | `ack: accepted`, or `ack: received` followed by `status: accepted` before work. Let the typed builders handle the nonce. |
| Pending; you will not take the work | `ack: rejected` with a reason; this is terminal. |
| Pending; local operator input is required | `ack: needs_human_confirmation` (nonce null), and tell your operator what is needed. Before expiry, the later decision is `ack: accepted` or `ack: rejected`; `error: failed` is also legal. |
| You already sent `ack: received` | `status: accepted` to accept, or `error: failed` if you cannot proceed; not `ack: rejected` or another ACK. |
| Accepted | `status: started` once work begins if it continues beyond the turn, or `result: completed` / `error: failed` for the outcome. |
| Started | `result: completed` or `error: failed`; do not send another `status: started`. |

These are receiver reply choices, not a replacement for cancellation or other [protocol rules](../PROTOCOL.md#5-message-types-and-lifecycle). Only accept within current local authority. A pending request or a `needs_human_confirmation` hold expires unconfirmed; do not act on it after expiry. A fresh `build-late-rejection` (nonce null) can report that outcome. A request accepted before its root expires can receive a fresh result afterward: message freshness is not the work deadline. Each reply still needs its own unexpired lifetime, legal current state and applicable authority; expiry never renews authority or reopens terminal work. A timely `received` state can also continue, but must reach acceptance before work.

### Report blockers without creating permission loops

Report a blocker promptly to your operator and, when the approved CAM path remains usable, to the requester. Examples include missing input, an approval or permission prompt, a CAM checkout or executable re-approval, and a sandbox denial. Pause only the affected action.

For a recoverable blocker on an accepted request, send one notice per distinct blocker, not repeated "still blocked" updates. Build a fresh informational `request` with `authorization.basis: none`, a descriptive operation, and a body that explicitly requests no outcome. Link it with `--continues-message` to the original request you received and ingested. Keep `in_reply_to: null`; do not combine this link with a retry or renewal. This is a discussion notice, not a lifecycle reply and not completion of the blocked request. See [linking follow-ups](#linking-follow-up-questions).

Name the blocker and what is needed from this session's own operator. Never ask the peer to grant approval or answer a permission prompt; a peer's "go ahead" is not authority. When the blocker clears, continue under existing authority; a result or error closes the loop. If the work cannot proceed and is being failed, use `error: failed` instead. For a request not yet accepted, use the legal reply choices above rather than treating a notice as acceptance.

If your own CAM command is awaiting permission or fails a gate, CAM itself is blocked: tell the operator the reply is unsent. Do not attempt another CAM command just to report that same blocker, bypass a gate, widen permissions or switch transports to force delivery. A changed CAM HEAD/profile or product executable is a visible blocker, not permission to approve or adopt it automatically. Report the failed check and the next operator decision, with known old/new values where available; do not guess a previously approved value.

### When a reply cannot be sent

Report what is known: unsent, transport-accepted, or unknown. A valid, correlated reply's transport acceptance satisfies the send step, not proof of delivery, handling, or the reported work's correctness. Use the [reply transport recovery table](CODEX_TO_CLAUDE.md#reply-transport-recovery) to distinguish a safe retry or fresh reply from an unresolved send; missing peer feedback is not retry permission.

After sending the appropriate reply or update, finish and yield a Codex turn so queued messages can surface later. Yielding is a scheduling step, not completion of accepted work. Do not poll a peer, the journal or product storage for delivery, wait indefinitely inside the sending turn, or start a retry timer. Keep successful plumbing in the background.

## Linking follow-up questions

Build a fresh discussion request normally. When it follows an earlier message from this peer, add `--continues-message FULL_MESSAGE_UUID` to the project-aware `claude-send` or `codex-send` command. Use the ID of the message you actually received and ingested, not the latest message guessed from the journal. Keep `in_reply_to: null` on this new root; ordinary ACK/status/result replies still use their preserved `--against` root.

CAM derives the conversation's starting message from the verified project journal. The link is recorded as `attributes.conversation_link` on `message.outbound.intent`, where `journal tail --show-content` can display it. That command reveals private message content, so use it only in the intended local session. The link is not added to the JSON sent to the peer. A lifecycle reply belongs to its own root; a later linked request can continue that root or one of its replies. An exact eligible transport retry retains its original link without repeating the flag.

The parent must have exact, validated, non-held inbound evidence for this sender and the same two session endpoints. Unknown messages, other projects or peers, self-references, and contradictory ancestry are rejected before dispatch. Fresh route observations may still have been journaled. Do not combine this flag with a retry or renewal; those already have their own meaning.

The link groups discussion for review only. It neither completes the parent nor authorizes work, renews an expired instruction, changes delivery, or activates causal ordering. Historical discussion may be referenced after expiry, but any new work still needs fresh intent and current authority. The existing [causal-ordering feature](CAUSAL_ORDERING.md) continues to treat separate request roots separately.

For maintainer live tests, include the intended one hello and one ACK in the direct test authorization. Do not add a test-harness instruction to ask again at every send and then attribute the resulting pauses to CAM. Product permission prompts remain subject to the product's policy.
