# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- `hermes_imsg.rpc`: JSON-RPC 2.0 client for one long-lived `imsg rpc` child. Calls multiplexed
  by id, notifications on a queue, reconnect with exponential backoff and an `on_connect` hook,
  typed errors for imsg's codes (-32000 to -32004, -32601, -32602), each with `retry_safe`.
- `hermes_imsg.transport`: `LocalTransport` and `SshTransport` (`ssh -T -o BatchMode=yes`,
  key path redacted from logs), and a one-shot `health_check()` that returns `imsg status`.
- `tests/fake_imsg/`: a stand-in for `imsg rpc` (imsg 0.20.0) backed by SQLite, with fault
  injection; SSH tests against a throwaway `sshd` with a forced-command key.
- CI: ruff, mypy (strict) and pytest on Python 3.11 to 3.13.
