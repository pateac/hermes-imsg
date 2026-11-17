# Phase 2: a week beside BlueBubbles

The standalone observer (`hermes-imsg observe`) ran on a Linux Hermes host from Nov 10 to Nov 17,
2026, reading a Mac's Messages through `imsg` 0.21.0 over SSH (key pinned to the gate), while
Hermes `v2026.9.24` kept answering through its BlueBubbles adapter. Nothing in the serving Hermes
was changed or restarted. The audit (`hermes-imsg audit`, `scripts/audit-detail.py`) compared the
record with the message ids BlueBubbles handed Hermes, read-only from Hermes's `state.db` files.

**Result: nothing missed, nothing doubled, and `imsg` never slower than BlueBubbles.**

| Measure (Nov 11 00:00 to Nov 17 00:00, six whole days) | Count |
|---|---|
| Messages BlueBubbles handed Hermes, with an id | 141 (14 more without an id, not checkable) |
| Inbound messages the observer recorded | 170 (and 5 tapbacks, 1 edit) |
| Seen by both | 136 |
| Missed (handed to Hermes, not recorded) | **0** |
| Handed to Hermes as a message, recorded by imsg as a tapback | 5 |
| Doubled | **0** |
| Recorded, not handed to Hermes | 34 |

**Timing, for the 136 seen by both:** `imsg` recorded each message a median 0.6 s before Hermes
stored BlueBubbles' copy; never later; at most 11.5 s earlier. The record keeps whole seconds, so
up to a second of that lead is rounding.

**The 5 tapbacks.** Hermes's BlueBubbles adapter at this tag drops only the six classic tapbacks
(`associatedMessageType` 2000 to 2005 and 3000 to 3005), so other reactions, most likely the
custom emoji tapbacks, reached Hermes as messages and could start a turn. `imsg` reports them as
reactions, which this plugin never turns into a turn. The first audit counted them as missed; it
now reports them on their own line.

**The 34 recorded but not handed over**, by likely reason: 14 folded into a nearby turn in the
same chat (Hermes keeps one id per turn when messages arrive in a burst), 7 unaddressed group
messages (the host's mention gate), 2 from a sender the host does not route, and 11 not yet
explained (8 with no BlueBubbles traffic within 15 minutes, 3 others): either commands the host
answers without storing a message, or messages BlueBubbles did not deliver.

**Restarts in the week:** one, the observer's connection to the Mac, for 62 s on a night with no
traffic (five reconnects, each an SSH connect timeout), resumed from the cursor with nothing lost.
No reboot of the Hermes host.

Not covered: sends through `imsg` (none were made; the observer cannot send), attachments over SSH
(the observer does not fetch them), and restarts on the Mac side, which the Hermes host cannot see.
