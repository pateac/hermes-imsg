# Contributing

Thanks for helping. Issues and pull requests are welcome; the maintainer aims to answer within a
week.

## Run the tests without a Mac

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest
```

Everything runs against `tests/fake_imsg/`, a stand-in for `imsg rpc`:

- `tests/fake_imsg/imsg` is the executable (`imsg rpc [--db PATH]`, or `FAKE_IMSG_DB`).
- `tests/fake_imsg/store.py` is its "chat.db", a SQLite file. Tests seed chats and messages
  (`add_chat`, `add_message`, `mutate`), schedule misbehaviour (`add_fault`: error, delay, drop,
  exit, garbage, stderr), and read back what the fake received (`calls`, `spawns`).
- The fake rejects unknown params and wrong types with `-32602`, as `imsg` does. If you need a
  method it lacks, add it following imsg's `docs/rpc.md`, and say in the PR which imsg version
  you read.

The SSH tests start a throwaway `sshd` on 127.0.0.1. They skip if `sshd` is not installed
(`apt install openssh-server`); CI sets `HERMES_IMSG_REQUIRE_SSH=1` so there they fail instead.

The adapter tests (`tests/test_platform.py`) need Hermes Agent itself, at the tag CI pins. Hermes
ships no wheel, so install it editable from a checkout:

```bash
git clone --depth 1 --branch v2026.9.24 https://github.com/NousResearch/hermes-agent
pip install -e ./hermes-agent
```

Without Hermes they skip; CI's `hermes` job sets `HERMES_IMSG_REQUIRE_HERMES=1` so there they fail
instead. Keep `platform.py` the only module that imports Hermes.

On a Mac, `tests/live/` checks the real `imsg` (opt-in, read-only; see the file's docstring).

## Before you open a PR

```bash
ruff check . && ruff format --check . && mypy && pytest
```

- Tests must be able to fail: include a control that shows the thing under test happened.
- Never log message bodies, at any level. Never log the SSH key path.
- No new runtime dependency for a few lines of code.
- Add a line under "Unreleased" in `CHANGELOG.md`.

## Sign your commits (DCO)

By contributing you certify the [Developer Certificate of Origin](https://developercertificate.org/).
Add a `Signed-off-by` line to each commit with `git commit -s`.

If an AI tool wrote part of your change, say so in the PR; you are still the one vouching for it.
