"""cursor.Cursor: the resume point survives restarts and never moves backwards."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from hermes_imsg.cursor import Cursor


def test_new_cursor_starts_at_zero_and_persists(tmp_path: Path) -> None:
    path = tmp_path / "state" / "cursor.json"
    c = Cursor(path)
    assert c.rowid == 0 and not path.exists()
    c.advance(41)
    c.advance(40)  # never backwards
    assert Cursor(path).rowid == 41
    assert json.loads(path.read_text())["rowid"] == 41
    assert [p.name for p in path.parent.iterdir()] == ["cursor.json"]  # no temp file left


def test_bind_keeps_the_position_for_the_same_database(tmp_path: Path) -> None:
    c = Cursor(tmp_path / "c.json")
    c.bind("/Users/howard/Library/Messages/chat.db")
    c.advance(100)
    again = Cursor(tmp_path / "c.json")
    again.bind("/Users/howard/Library/Messages/chat.db")
    assert again.rowid == 100


def test_bind_discards_the_position_for_another_database(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    c = Cursor(tmp_path / "c.json")
    c.bind("/old/chat.db")
    c.advance(100)
    other = Cursor(tmp_path / "c.json")
    with caplog.at_level(logging.WARNING):
        other.bind("/new/chat.db")
    assert other.rowid == 0 and Cursor(tmp_path / "c.json").database == "/new/chat.db"
    assert "database changed" in caplog.text


def test_unreadable_cursor_starts_at_the_newest_row_and_says_so(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "c.json"
    path.write_text("{not json")
    with caplog.at_level(logging.ERROR):
        assert Cursor(path).rowid == 0
    assert "unreadable" in caplog.text
    path.write_text(json.dumps({"rowid": "12"}))  # wrong type is ignored, not trusted
    assert Cursor(path).rowid == 0
