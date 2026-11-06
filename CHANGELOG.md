# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
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
- The fake `imsg` follows imsg 0.21.0 as seen on a real Mac: `database.features` in `status`,
  and `destination_caller_id` and `reactions` on every row.
- The recommended `authorized_keys` line adds `from=` (only the Hermes host's address), and the
  SSH tests use that form. Checked on a real Mac over SSH; recorded in `docs/phase0-check.md`.
