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

## A week beside another relay (observe only)

To check what the plugin sees against a relay that is already answering (BlueBubbles, say) without
anyone getting two answers, observe first. Two ways, with the same reading code and the same record:

- **Standalone (when that Hermes is in service):** `hermes-imsg observe` on the Hermes host, in its
  own process. Hermes itself is not touched or restarted.

  ```bash
  python3 -m venv ~/hermes-imsg-venv
  ~/hermes-imsg-venv/bin/pip install git+https://github.com/pateac/hermes-imsg
  ~/hermes-imsg-venv/bin/hermes-imsg observe --host me@mac.local \
      --identity-file ~/.ssh/hermes_imsg --state-dir /an/encrypted/disk/imsg-observe
  ```

  It has no dependencies, so a checkout works as well, with nothing installed: `git clone
  https://github.com/pateac/hermes-imsg && cd hermes-imsg && python3 -m hermes_imsg observe ...`.
  Keep it running (in `tmux`, say). It reconnects by itself, and after a stop or a reboot it
  resumes from its cursor, so nothing in between is lost. The very first start begins at the
  newest message.
- **Inside Hermes:** `IMSG_OBSERVE_ONLY=1` (or `observe_only: true`). The plugin subscribes and
  resumes as it would live, but starts no turns and sends nothing: a send returns an error. To go
  live, unset it; the cursor carries over, so the switch does not replay the observed days.

Either way, each row is appended to `<state_dir>/observe.jsonl`: the time, the kind (`inbound`,
`reaction`, `edited`, `unsent`, `poll`), the row id, the message GUID, the chat GUID and the
sender. Never the message text. The senders are phone numbers and emails, so keep the folder on an
encrypted disk and delete it after the audit.

### The audit

Hermes keeps each inbound message's platform id in its `state.db` (one per profile), and Hermes's
BlueBubbles adapter uses the same message GUID as imsg. So:

```bash
hermes-imsg audit --record /an/encrypted/disk/imsg-observe/observe.jsonl \
    --hermes-db <hermes home>/state.db --hermes-db <hermes home>/profiles/<name>/state.db
```

It opens the databases read-only and reports, for the window (the record's first to last line, or
`--since` / `--until`):

- **missed**: BlueBubbles handed Hermes a message the record lacks. Must be 0.
- **doubled**: an inbound row recorded twice. Must be 0.
- **extra**: recorded but not handed to Hermes. Each needs a reason: a sender Hermes does not
  route, a group it ignores, a time BlueBubbles or Hermes was down.
- messages with **no id**, counted but not checkable.

It exits 0 when missed and doubled are both 0. Hermes's and the record's clocks differ by a few
seconds, so extras are checked against a window five minutes wider; audit the whole days in the
middle of the week to keep the edges out of it.

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
