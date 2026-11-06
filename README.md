# hermes-imsg

iMessage for [Hermes Agent](https://github.com/NousResearch/hermes-agent) through
[`imsg`](https://github.com/openclaw/imsg), a single CLI on a Mac that speaks JSON-RPC over
stdio. The plugin runs wherever Hermes runs (Linux included) and reaches the Mac over one
outbound SSH connection. No listener on the Mac, no password in a URL, no webhook to register.

> **Status: pre-release, phase 1 of 5.** What exists today is the plumbing under the platform:
> the JSON-RPC client, the local and SSH transports, and a fake `imsg` so CI needs no Mac.
> It is **not yet a loadable Hermes plugin** (the platform, events, cursor and send modules land
> in v0.1). On a real Mac (macOS 27.2, SIP on, `imsg` 0.21.0), reads, watch, tapbacks, poll votes
> and a send all worked, and the RPC client passed its live tests: see
> [the phase 0 check](docs/phase0-check.md). Over SSH from a Linux host, with the key pinned to
> `imsg rpc`, `status` works the same; sends over SSH are not tested yet.
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

## Try the pieces that exist

```bash
pip install git+https://github.com/pateac/hermes-imsg
```

```python
import asyncio
from hermes_imsg.rpc import RpcClient
from hermes_imsg.transport import SshTransport

mac = SshTransport(
    "mac.local", identity_file="~/.ssh/hermes_imsg", imsg_path="/opt/homebrew/bin/imsg"
)


async def main():
    print(await mac.health_check())  # imsg's status: database, bridge, methods

    async def subscribe(client):
        await client.call("watch.subscribe", {})

    async with RpcClient(mac, on_connect=subscribe) as client:
        await client.wait_ready(20)
        while True:
            note = await client.notifications.get()
            if note.method == "message":
                print(note.params["message"]["chat_guid"])


asyncio.run(main())
```

Every error says whether re-sending is safe (`err.retry_safe`). The client never retries a call
by itself: a send that may have run must not be sent twice.

## Develop without a Mac

```bash
pip install -e ".[dev]"
pytest                      # against tests/fake_imsg; SSH tests need sshd installed locally
ruff check . && ruff format --check . && mypy
```

`tests/fake_imsg/` is a stand-in that speaks `imsg rpc`'s protocol (imsg 0.20.0) with the same
strictness, backed by a SQLite file so it survives being killed and respawned. The SSH tests
start a throwaway `sshd` on 127.0.0.1 whose only key is forced to run the fake. On a Mac,
`tests/live/` checks the real thing (opt-in, read-only). See [CONTRIBUTING.md](CONTRIBUTING.md).

## Roadmap

| Phase | What | State |
|---|---|---|
| 0 | One hour at a Mac: install `imsg`, status, watch, a poll's votes, one send; the SSH key | [done Nov 5](docs/phase0-check.md) |
| 1 | `rpc.py`, `transport.py`, the fake `imsg`, CI | **this release** |
| 2 | v0.1 "text and reads": platform, events, cursor, send, status; a week beside BlueBubbles | next |
| 3 | Switch sends over, one profile then all | |
| 4 | Publish v0.1: docs with screenshots, PyPI, the Hermes plugin catalog | |
| 5 | v0.2 "the helper": reactions, threaded replies, native polls (needs SIP off on the Mac) | |

## Licence

MIT. `imsg` is a separate project, MIT, © Peter Steinberger. This project is not affiliated
with Apple, Nous Research or OpenClaw.
