# hermes-imsg

iMessage for [Hermes Agent](https://github.com/NousResearch/hermes-agent) through
[`imsg`](https://github.com/openclaw/imsg), a single CLI on a Mac that speaks JSON-RPC over
stdio. The plugin runs wherever Hermes runs (Linux included) and reaches the Mac over one
outbound SSH connection. No listener on the Mac, no password in a URL, no webhook to register.

> **Status: pre-release; phase 2 of 5 built, not yet run live.** It is a loadable Hermes plugin
> for text and reads: receive, send, resume after restarts with nothing missed or doubled, health.
> CI tests it against Hermes v2026.9.24's real plugin loader and adapter base class, and against a
> fake `imsg`. On a real Mac (macOS 27.2, SIP on, `imsg` 0.21.0), `imsg` itself and this
> plugin's RPC client were checked locally and over SSH ([phase 0](docs/phase0-check.md)). It has
> **not yet run in a live Hermes gateway against that Mac**; a week beside BlueBubbles is next.
> See [Roadmap](#roadmap).

Built with [Claude Code](https://claude.com/claude-code). Maintained and tested by
[@pateac](https://github.com/pateac).

## Why

Hermes ships a BlueBubbles adapter only. BlueBubbles is an Electron server with a REST listener
on the Mac and a password in its URLs. `imsg` is a single binary with no port and no daemon;
OpenClaw moved its iMessage channel to it in May 2026. Hermes declined an in-tree `imsg` adapter
and asked for a standalone plugin via `ctx.register_platform()`
([hermes-agent#53407](https://github.com/NousResearch/hermes-agent/issues/53407)). This is that
plugin.

## How it connects

```
Hermes host (Linux or Mac)                         Mac (signed into Messages)
  hermes_imsg.rpc.RpcClient
    └ SshTransport ── ssh -T -o BatchMode=yes ──>  sshd (key pinned to one command) ──> imsg rpc
                       newline-delimited JSON-RPC         reads chat.db, sends via Messages
```

- **local**: Hermes runs on the Mac and starts `imsg rpc` itself.
- **ssh**: Hermes runs elsewhere. The SSH key is the whole trust boundary; on the Mac it is
  pinned to `imsg rpc` and nothing else. See [SECURITY.md](SECURITY.md).

One long-lived `imsg rpc` child per account. Calls multiplex over it by id; notifications
(`message`, `watch.overflow`, ...) stream back. If the child dies, pending calls fail as
"may have run", the client reconnects with exponential backoff and calls your `on_connect`
again, which is where a subscriber re-subscribes and back-fills from its cursor.

## Use it with Hermes

```bash
pip install git+https://github.com/pateac/hermes-imsg   # into Hermes's Python environment
hermes plugins enable imsg
```

```bash
# Hermes's .env: the Mac over SSH (or IMSG_TRANSPORT=local on the Mac itself)
IMSG_SSH_HOST=me@mac.local
IMSG_SSH_KEY=~/.ssh/hermes_imsg
IMSG_PATH=/Users/me/.local/bin/imsg
IMSG_ALLOWED_USERS=+15551230001
```

Settings, what the agent sees, and what is not done yet: [docs/install-hermes.md](docs/install-hermes.md).

## Develop without a Mac

```bash
pip install -e ".[dev]"
pytest                      # against tests/fake_imsg; SSH tests need sshd installed locally
ruff check . && ruff format --check . && mypy
```

`tests/fake_imsg/` is a stand-in that speaks `imsg rpc`'s protocol (imsg 0.21.0) with the same
strictness, backed by a SQLite file so it survives being killed and respawned. The SSH tests
start a throwaway `sshd` on 127.0.0.1 whose only key is forced to run the fake. On a Mac,
`tests/live/` checks the real thing (opt-in, read-only). The adapter tests need Hermes itself;
they skip without it, and CI runs them against the pinned tag. See
[CONTRIBUTING.md](CONTRIBUTING.md).

## Roadmap

| Phase | What | State |
|---|---|---|
| 0 | One hour at a Mac: install `imsg`, status, watch, a poll's votes, one send; the SSH key | [done Nov 5](docs/phase0-check.md) |
| 1 | `rpc.py`, `transport.py`, the fake `imsg`, CI | done Nov 1 |
| 2 | v0.1 "text and reads": platform, events, cursor, send, status; a week beside BlueBubbles | **built Nov 6**; the week's run is next |
| 3 | Switch sends over, one profile then all | |
| 4 | Publish v0.1: docs with screenshots, PyPI, the Hermes plugin catalog | |
| 5 | v0.2 "the helper": reactions, threaded replies, native polls (needs SIP off on the Mac) | |

## Licence

MIT. `imsg` is a separate project, MIT, © Peter Steinberger. This project is not affiliated
with Apple, Nous Research or OpenClaw.
