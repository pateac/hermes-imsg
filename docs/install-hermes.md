# Install in Hermes

Tested against Hermes Agent **v2026.9.24**. The Mac side comes first: `imsg` installed, Full Disk
Access granted ([phase 0](phase0-check.md)), and for the SSH transport the gate script installed
and the key pinned to it ([SECURITY.md](../SECURITY.md)):

```bash
# on the Mac, as the user signed into Messages, from a checkout of this repository
install -m 755 scripts/hermes-imsg-gate ~/.local/bin/hermes-imsg-gate
```

```
# ~/.ssh/authorized_keys on the Mac: the Hermes host's address, then the gate
from="192.0.2.10",restrict,command="/Users/me/.local/bin/hermes-imsg-gate" ssh-ed25519 AAAA... hermes-imsg
```

## 1. Make the plugin loadable

Either route works; both were checked with Hermes's own plugin loader.

- **pip**, into the Python environment Hermes runs in:
  ```bash
  pip install git+https://github.com/pateac/hermes-imsg
  ```
- **A plugin folder**: copy the `hermes_imsg` package directory to `~/.hermes/plugins/imsg/`
  (it carries its own `plugin.yaml`). In Docker, mount it there.

Then enable it:

```bash
hermes plugins enable imsg
```

## 2. Configure

In Hermes's `.env`, for Hermes on another machine reaching the Mac over SSH:

```bash
IMSG_SSH_HOST=me@mac.local
IMSG_SSH_KEY=~/.ssh/hermes_imsg
IMSG_PATH=/Users/me/.local/bin/imsg
IMSG_ALLOWED_USERS=+15551230001,someone@example.com
IMSG_HOME_CHANNEL=any;-;+15551230001
```

For Hermes running on the Mac itself, replace the three SSH lines with `IMSG_TRANSPORT=local`.

Or in `config.yaml` (environment variables win over these):

```yaml
platforms:
  imsg:
    enabled: true
    extra:
      host: me@mac.local
      identity_file: ~/.ssh/hermes_imsg
      imsg_path: /Users/me/.local/bin/imsg
      require_mention: true   # in group chats, answer only when addressed ("hermes, ...")
```

| Key | Env | Default | Meaning |
|---|---|---|---|
| `host` | `IMSG_SSH_HOST` | | SSH destination; setting it selects the SSH transport |
| `identity_file` | `IMSG_SSH_KEY` | | Private key for that host |
| `ssh_config` | `IMSG_SSH_CONFIG` | | An ssh config file (`-F`) |
| `transport` | `IMSG_TRANSPORT` | | `local` when Hermes runs on the Mac |
| `imsg_path` | `IMSG_PATH` | `imsg` | `imsg` on the Mac; over SSH give the full path |
| `state_dir` | `IMSG_STATE_DIR` | `<hermes home>/imsg` | Where the resume cursor is kept |
| `sends_per_minute` | `IMSG_SENDS_PER_MINUTE` | 30 | Outbound rate limit (bursts of 5) |
| `max_attachment_mb` | `IMSG_MAX_ATTACHMENT_MB` | 25 | Inbound attachment size cap |
| `require_mention`, `mention_patterns` | `IMSG_REQUIRE_MENTION`, `IMSG_MENTION_PATTERNS` | off | Group gating, as in Hermes's BlueBubbles adapter |

Who may talk to the agent is Hermes's own check: `IMSG_ALLOWED_USERS` (handles) or
`IMSG_ALLOW_ALL_USERS`.

## A run beside another relay (observe only)

To check the plugin against a relay that is already answering (BlueBubbles, say) without the agent
answering twice, set `IMSG_OBSERVE_ONLY=1` (or `observe_only: true`). The plugin subscribes and
resumes exactly as it would live, but starts no turns and sends nothing: a send returns an error.
Each row it would have handed to Hermes is appended to `<state_dir>/observe.jsonl`: the time, the
kind (`inbound`, `reaction`, `edited`, `unsent`, `poll`), the row id, the message GUID, the chat
GUID and the sender. Never the message text.

Compare the `inbound` GUIDs with the message ids the other relay gave Hermes; Hermes's BlueBubbles
adapter uses the same message GUID. Nothing should be missing, and nothing should appear twice.
To go live, unset it. The cursor carries over, so the switch does not replay the observed days.

## 3. What the agent sees

- **One turn per message from someone else.** The agent's own sends come back from Messages and
  are dropped.
- **Tapbacks, edits, unsends and poll rows never start a turn.** A poll's visible caption is an
  ordinary message and starts one turn. Hosts that want tapbacks or votes (approvals, say) add a
  listener to `adapter.secondary_listeners`.
- **Identity matches Hermes's BlueBubbles adapter**: the chat id is the chat GUID
  (`any;-;+15551230001`, `any;+;chat…` for groups), the user id is the sender's phone or email,
  the message id is the message GUID. An attachment-only message reads "(attachment)".
- **Nothing missed, nothing doubled.** A cursor (`<state_dir>/cursor.json`) records the last row
  handed to Hermes. On start and after any reconnect, `imsg` replays every row after it; replays
  at or below it are dropped. The very first start begins at the newest message, with no history.
- **Sends** are plain text (markdown stripped), split at 4,000 characters, and rate limited. A
  send that may have reached the recipient is never retried.
- **Files both ways.** Inbound photos, audio and documents are copied into Hermes's media cache
  (over SSH through the gate's `fetch`; the Mac's copies are never moved). Outbound images,
  documents, audio and video are placed on the Mac (the gate's `put`) and sent with `imsg send`.
  Audio goes as a file; a native voice bubble needs the helper (v0.2).
- **Health**: `imsg status` is checked every minute; a child that stops answering is restarted,
  and an unreadable Messages database is logged.

## Not yet

- **Sends over SSH** have not been tried on a real Mac: the first one needs a person at the Mac to
  allow an Automation prompt.
- **Reactions, threaded replies and creating polls** need `imsg`'s helper, which needs SIP off
  (v0.2).
- **Cron delivery from a separate process** (Hermes's `standalone_sender_fn`). Cron jobs run by the
  gateway itself deliver normally.
