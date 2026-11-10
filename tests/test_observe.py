"""observe.py: the standalone observer (the plugin's reading code, no Hermes) and the audit."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from conftest import FAKE_IMSG, eventually, run
from fake_imsg.store import FakeDB

from hermes_imsg import observe
from hermes_imsg.__main__ import main
from hermes_imsg.transport import LocalTransport

COLE = "+15551230001"


def _lines(state: Path) -> list[dict[str, Any]]:
    path = state / observe.RECORD
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def test_observer_records_each_row_once_across_a_killed_child(db: FakeDB, tmp_path: Path) -> None:
    chat = db.add_chat(COLE)
    state = tmp_path / "state"

    async def scenario() -> None:
        task = asyncio.create_task(observe.run(LocalTransport(FAKE_IMSG), state))
        await eventually(lambda: bool(db.calls("watch.subscribe")))
        db.add_message(chat["id"], "SECRET one")
        db.add_message(chat["id"], "my own reply", is_from_me=True)  # never recorded
        db.add_message(chat["id"], "", is_reaction=True, reaction_type="like", reacted_to_guid="X")
        await eventually(lambda: len(_lines(state)) == 2)
        os.kill(db.spawns()[-1]["pid"], signal.SIGKILL)
        db.add_message(chat["id"], "SECRET two")  # written while the child is gone
        await eventually(lambda: len(db.spawns()) == 2 and len(_lines(state)) == 3)
        await asyncio.sleep(0.3)  # a double would show now
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    run(scenario())
    lines = _lines(state)
    expected = [r["guid"] for r in db.messages_after(0) if not r["is_from_me"]]
    assert [x["guid"] for x in lines] == expected
    assert [x["kind"] for x in lines] == ["inbound", "reaction", "inbound"]
    assert "SECRET" not in (state / observe.RECORD).read_text()  # no message text, ever


def test_observer_resumes_after_a_restart_and_skips_history_at_first(
    db: FakeDB, tmp_path: Path
) -> None:
    chat = db.add_chat(COLE)
    db.add_message(chat["id"], "history before the observer ever ran")
    state = tmp_path / "state"

    async def observe_until(n: int) -> None:
        task = asyncio.create_task(observe.run(LocalTransport(FAKE_IMSG), state))
        await eventually(lambda: len(_lines(state)) == n)
        await asyncio.sleep(0.3)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def first() -> None:
        task = asyncio.create_task(observe.run(LocalTransport(FAKE_IMSG), state))
        await eventually(lambda: bool(db.calls("watch.subscribe")))
        db.add_message(chat["id"], "live")
        await eventually(lambda: len(_lines(state)) == 1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    run(first())
    db.add_message(chat["id"], "while stopped 1")
    db.add_message(chat["id"], "while stopped 2")
    run(observe_until(3))
    rows = db.messages_after(0)
    assert [x["guid"] for x in _lines(state)] == [r["guid"] for r in rows[1:]]  # not the history


# --- the audit ----------------------------------------------------------------------------

T0 = 1_794_000_000.0  # a fixed instant in Nov 2026


def _at(offset: float) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(T0 + offset, UTC).isoformat(timespec="seconds")


def _record(path: Path, rows: list[tuple[float, str, str]]) -> Path:
    lines = [
        {
            "at": _at(t),
            "kind": kind,
            "rowid": i + 1,
            "guid": guid,
            "chat_guid": f"any;-;{COLE}",
            "sender": COLE,
        }
        for i, (t, kind, guid) in enumerate(rows)
    ]
    path.write_text("".join(json.dumps(x) + "\n" for x in lines))
    return path


def _hermes_db(path: Path, messages: list[tuple[str, str, str | None, float]]) -> Path:
    """The two tables and the columns the audit reads (the real schema: test_platform.py)."""
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT NOT NULL, started_at REAL);"
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT NOT NULL,"
        " role TEXT NOT NULL, content TEXT, timestamp REAL NOT NULL, platform_message_id TEXT);"
        "INSERT INTO sessions VALUES ('bb', 'bluebubbles', 0), ('tg', 'telegram', 0);"
    )
    for source, role, guid, t in messages:
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp, platform_message_id)"
            " VALUES (?, ?, 'x', ?, ?)",
            ("bb" if source == "bluebubbles" else "tg", role, T0 + t, guid),
        )
    conn.commit()
    conn.close()
    return path


def test_audit_passes_when_every_delivered_message_was_recorded_once(tmp_path: Path) -> None:
    record = _record(
        tmp_path / "observe.jsonl",
        [(0, "inbound", "A"), (10, "reaction", "R"), (20, "inbound", "B")],
    )
    dbs = [
        _hermes_db(tmp_path / "cole.db", [("bluebubbles", "user", "A", 1)]),
        _hermes_db(
            tmp_path / "megan.db",
            [
                ("bluebubbles", "user", "B", 21),
                ("bluebubbles", "assistant", "REPLY", 22),
                ("telegram", "user", "TG", 5),
            ],
        ),
    ]
    before = [d.read_bytes() for d in dbs]
    result = observe.audit(record, dbs)
    # B reached Hermes a second after the record's last line: outside the window, inside GRACE
    assert result.passed and result.delivered == 1 and result.recorded == 2
    assert result.missed == [] and result.doubled == [] and result.extra == []
    assert [d.read_bytes() for d in dbs] == before  # read-only
    argv = ["audit", "--record", str(record)] + [x for d in dbs for x in ("--hermes-db", str(d))]
    assert main(argv) == 0


def test_audit_finds_missed_doubled_extra_and_unidentified(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    record = _record(
        tmp_path / "observe.jsonl",
        [
            (0, "inbound", "A"),
            (5, "inbound", "A"),  # doubled
            (10, "inbound", "EXTRA"),  # e.g. an unregistered sender the relay's side ignored
            (100, "inbound", "C"),
        ],
    )
    db = _hermes_db(
        tmp_path / "cole.db",
        [
            ("bluebubbles", "user", "A", 1),
            ("bluebubbles", "user", "MISSED", 50),
            ("bluebubbles", "user", None, 60),  # no id: cannot be checked, only counted
            ("bluebubbles", "user", "C", 100),
            ("bluebubbles", "user", "OUTSIDE", 500),  # after the record's last line: not counted
        ],
    )
    result = observe.audit(record, [db])
    assert [g for g, _ in result.missed] == ["MISSED"]
    assert result.doubled == ["A"]
    assert [r["guid"] for r in result.extra] == ["EXTRA"]
    assert result.unidentified == 1 and not result.passed
    assert main(["audit", "--record", str(record), "--hermes-db", str(db)]) == 1
    out = capsys.readouterr().out
    assert "missed (handed to Hermes, not recorded): 1" in out and out.rstrip().endswith("FAIL")
    assert COLE not in out  # senders are masked in the report


def test_audit_window_can_be_narrowed(tmp_path: Path) -> None:
    record = _record(tmp_path / "observe.jsonl", [(0, "inbound", "A"), (100, "inbound", "B")])
    db = _hermes_db(
        tmp_path / "cole.db",
        [("bluebubbles", "user", "EARLY", 1), ("bluebubbles", "user", "B", 100)],
    )
    assert not observe.audit(record, [db]).passed  # control: EARLY counts as missed
    narrowed = observe.audit(record, [db], since=T0 + 50)
    assert narrowed.passed and narrowed.delivered == 1 and narrowed.recorded == 1


def test_observe_needs_exactly_one_place_to_reach_imsg(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        main(["observe", "--state-dir", str(tmp_path)])
    with pytest.raises(SystemExit):
        main(["observe", "--local", "--host", "mac", "--state-dir", str(tmp_path)])
