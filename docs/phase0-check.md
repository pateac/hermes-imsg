# Phase 0: the check on a real Mac

Run on Nov 5, 2026 on an M4 Mac with macOS 27.2, SIP on, as a standard (non-admin) user signed
into Messages. BlueBubbles was serving the same account throughout. Nothing changed SIP, and no
helper was installed. The SSH and forced-command key setup is still to do.

| Step | Result | Notes |
|---|---|---|
| 0. Starting state | Pass | Full Disk Access granted to Terminal; Messages database readable |
| 1. Install `imsg` | Pass | 0.21.0 signed release, checksum matches, helper dylib not installed |
| 2. Status | Pass | Database ready, bridge not ready, protocol version 1 |
| 3. Chats and history | Pass | Direct chat GUID form is `any;-;<phone>` |
| 4. Watch and tapback | Pass | A second reader did not disturb BlueBubbles replies |
| 5. Poll | Pass | `imessage.poll.created` and `imessage.poll.voted` rows captured |
| 6. One send | Pass | Delivered; no agent reply within 45 seconds |
| 7. Live tests and fake comparison | Pass | `tests/live` 2 of 2 on Python 3.13 |

## `imsg` build

- Version 0.21.0, signed `Developer ID Application: Peter Steinberger (Y5PE65HELJ)`, notarized
- Binary sha256: `ba9d1c5e5b7f8329b3de10a113ea3b7abc12e36fe3a960a3323b0a0f4d3af601`
- Release zip sha256: `0da64a1c2c125eba033c4092ea17a6997f1ccc51dbd8be68ce77aa43df6d8885`
- `database.features`, all true: `routing_metadata`, `balloon_payloads`, `reply_context`,
  `scheduled_messages`, `reactions`, `unread_state`
- Live methods with the bridge down (15): `initialize`, `status`, `chats.list`, `chats.get`,
  `messages.stats`, `messages.history`, `messages.search`, `messages.after`,
  `messages.scheduled`, `watch.subscribe`, `watch.unsubscribe`, `message.send_status`, `send`,
  `typing`, `read`
- `supported_methods`: 45

## Findings for the plugin

1. **Poll rows have junk `text`.** It is U+FFFD on `imessage.poll.created` rows and a single
   space on `imessage.poll.voted` rows. Read `poll` on these rows and ignore `text`.
2. **One poll is several rows.** The question also arrives as its own plain-text row, and each
   vote is another row. Without handling, one poll could start several agent runs.
3. **RPC replies can arrive out of order.** Match responses by id (`rpc.py` does).
4. **The first send needs a person at the Mac.** macOS shows an Automation prompt (Terminal
   wants to control Messages) that must be allowed. Setup docs should cover this, including for
   sends over SSH, which were not tested.
5. **Error `data` wording differs from the fake.** Match on codes, not on the text.

## Poll row shapes

Field names and types as captured from `imsg watch --reactions --json`. These rows also have
`balloon_bundle_id` ending in `com.apple.messages.Polls`.

```text
imessage.poll.created
  creator, event, kind, poll_guid, question: str
  participants: [str]
  options: [{id, text}]
  metadata: {associated_message_type: int, bundle_id: str, payload_bytes: int,
             query_keys: [str], summary_bytes: int, url_scheme: str}

imessage.poll.voted
  event, kind, original_guid, poll_guid: str
  participants: [str]
  vote: {event_type, option_id, option_text, participant}
  votes: [{event_type, option_id, option_text, participant}]
  metadata: {associated_message_type: int, bundle_id: str, payload_bytes: int,
             summary_bytes: int, url_scheme: str}
```

## Real `imsg` compared with the fake

Matches: error codes and messages on all probes (-32602 and -32601), `data` being a string,
`watch.subscribe` and `watch.unsubscribe` results, and `ok: true` when unsubscribing an unknown
subscription.

| Area | Difference | Now |
|---|---|---|
| Version | Fake reported 0.20.0, real is 0.21.0 | Fake reports 0.21.0 |
| `status` | Fake lacked `database.features` | Added, all true |
| Rows, always on real | Fake lacked `destination_caller_id`, `reactions` | Added |
| `methods` | Fake lacks `messages.stats`, `messages.search`, `messages.scheduled`, `typing`, `read` | Fake lists only what it implements |
| `supported_methods` | Fake lists 23, real lists 45 | Same reason |
| Rows, sometimes on real | Fake lacks `balloon_bundle_id`, `poll`, `is_reaction`, `is_reaction_add`, `reacted_to_guid`, `reaction_type`, `reaction_emoji`, `is_read`, `reply_to_guid`, `reply_to_sender`, `reply_to_text` | Tests pass these per row; poll and tapback rows come with phase 2's event tests |
| Error `data` text | Wording differs for a wrong type, unknown parameter, missing parameter and unknown method | Match on codes |

## Not tested

Deferred to phase 5, because `poll.send` needs the bridge and so SIP off:

- `poll.send`
- The `is_from_me` `imessage.poll.created` row for a poll created from the agent's account
- Reading another person's vote on such a poll

Also not tested: sends over SSH (the Automation prompt may land on `sshd-keygen-wrapper`).
