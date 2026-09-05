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

## Linking follow-up questions

Build a fresh discussion request normally. When it follows an earlier message from this peer, add `--continues-message FULL_MESSAGE_UUID` to the project-aware `claude-send` or `codex-send` command. Use the ID of the message you actually received and ingested, not the latest message guessed from the journal. Keep `in_reply_to: null` on this new root; ordinary ACK/status/result replies still use their preserved `--against` root.

CAM derives the conversation's starting message from the verified project journal. The link is recorded as `attributes.conversation_link` on `message.outbound.intent`, where `journal tail --show-content` can display it. That command reveals private message content, so use it only in the intended local session. The link is not added to the JSON sent to the peer. A lifecycle reply belongs to its own root; a later linked request can continue that root or one of its replies. An exact eligible transport retry retains its original link without repeating the flag.

The parent must have exact, validated, non-held inbound evidence for this sender and the same two session endpoints. Unknown messages, other projects or peers, self-references, and contradictory ancestry are rejected before dispatch. Fresh route observations may still have been journaled. Do not combine this flag with a retry or renewal; those already have their own meaning.

The link groups discussion for review only. It neither completes the parent nor authorizes work, renews an expired instruction, changes delivery, or activates causal ordering. Historical discussion may be referenced after expiry, but any new work still needs fresh intent and current authority. The existing [causal-ordering feature](CAUSAL_ORDERING.md) continues to treat separate request roots separately.

For maintainer live tests, include the intended one hello and one ACK in the direct test authorization. Do not add a test-harness instruction to ask again at every send and then attribute the resulting pauses to CAM. Product permission prompts remain subject to the product's policy.
