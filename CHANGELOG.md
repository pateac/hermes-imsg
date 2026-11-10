# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- The Hermes platform plugin `imsg` (`hermes_imsg.platform`), for text and reads, tested against
  Hermes v2026.9.24: one turn per message from someone else; the agent's own sends, tapbacks,
  edits, unsends and poll rows start none (tapbacks, edits and polls go to
  `secondary_listeners`); chat and user ids as Hermes's BlueBubbles adapter; group mention
  gating; plain-text sends split at 4,000 characters and rate limited; local attachments copied
  into Hermes's media cache. Loads from pip (`hermes_agent.plugins` entry point) or as a plugin
  folder (`plugin.yaml`); enable with `hermes plugins enable imsg`.
- `hermes_imsg.events`: every imsg row is one inbound message, one secondary event, or nothing.
- `hermes_imsg.cursor`: the resume point; replays after a reconnect or restart are dropped.
- `hermes_imsg.send`: GUID or handle targets, a token-bucket rate limit, and failures that say
  whether a retry could duplicate.
- `hermes_imsg.status`: `imsg status` as a health object, checked every minute; a child that stops
  answering is restarted.
- Files both ways over SSH: `scripts/hermes-imsg-gate`, the forced command for the key, runs
  `imsg rpc`, serves files only from `~/Library/Messages/Attachments` (`fetch`), and stages
  outgoing files in an outbox (`put`, 100 MB cap); arguments are base64, never shell-parsed.
  `Transport.fetch` / `Transport.put` on both transports; the adapter sends images (file or URL),
  documents, audio and video. Without the gate, files fail with an error naming it.
- `hermes-imsg observe` (also `python -m hermes_imsg observe`): the plugin's own reading code in a
  standalone process, writing the same record, for a week beside another relay without touching
  the Hermes that is serving. `hermes-imsg audit`: compares the record with the message ids that
  relay handed Hermes (from Hermes's `state.db`, read-only): missed, doubled, extra.
- `observe_only` (`IMSG_OBSERVE_ONLY`): record each row to `observe.jsonl` (ids, never text),
  start no turns and send nothing, for a run beside another relay.
- `docs/install-hermes.md`.
- CI: a `hermes` job runs the adapter tests against Hermes at the pinned tag.
- `docs/phase0-check.md`: the check on a real Mac (macOS 27.2, SIP on, `imsg` 0.21.0), with
  findings for the plugin and the real poll row shapes.
- `hermes_imsg.rpc`: JSON-RPC 2.0 client for one long-lived `imsg rpc` child. Calls multiplexed
  by id, notifications on a queue, reconnect with exponential backoff and an `on_connect` hook,
  typed errors for imsg's codes (-32000 to -32004, -32601, -32602), each with `retry_safe`.
- `hermes_imsg.transport`: `LocalTransport` and `SshTransport` (`ssh -T -o BatchMode=yes`,
  key path redacted from logs), and a one-shot `health_check()` that returns `imsg status`.
- `tests/fake_imsg/`: a stand-in for `imsg rpc` (imsg 0.20.0) backed by SQLite, with fault
  injection; SSH tests against a throwaway `sshd` with a forced-command key.
- CI: ruff, mypy (strict) and pytest on Python 3.11 to 3.13.

### Changed
- `RpcClient` gains `ready` and `restart()`.
- The fake `imsg` follows imsg 0.21.0 as seen on a real Mac: `database.features` in `status`,
  and `destination_caller_id` and `reactions` on every row.
- The recommended `authorized_keys` line adds `from=` (only the Hermes host's address), and the
  SSH tests use that form. Checked on a real Mac over SSH; recorded in `docs/phase0-check.md`.
