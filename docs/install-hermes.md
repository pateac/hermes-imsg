# Install in Hermes

Tested against Hermes Agent **v2026.9.24**. The Mac side comes first: `imsg` installed, Full Disk
Access granted, and for the SSH transport a key pinned to `imsg rpc`
([phase 0](phase0-check.md), [SECURITY.md](../SECURITY.md)).

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
- **Health**: `imsg status` is checked every minute; a child that stops answering is restarted,
  and an unreadable Messages database is logged.

## Not yet

- **Attachments over SSH.** The pinned key runs only `imsg rpc`, which reports a file's path on the
  Mac, not its bytes. Inbound files are fetched only with the local transport; over SSH the agent
  sees "(attachment)". Outbound files are local-transport only too.
- **Sends over SSH** have not been tried on a real Mac: the first one needs a person at the Mac to
  allow an Automation prompt.
- **Reactions, threaded replies and creating polls** need `imsg`'s helper, which needs SIP off
  (v0.2).
- **Cron delivery from a separate process** (Hermes's `standalone_sender_fn`). Cron jobs run by the
  gateway itself deliver normally.
