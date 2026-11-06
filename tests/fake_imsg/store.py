"""The fake imsg's "chat.db": one SQLite file shared by the fake and the tests.

Tests seed chats and messages, schedule faults, and read back what the fake
received. Because it is a file, it survives the fake being killed and
respawned, the same way chat.db survives `imsg rpc` restarts. Stdlib only:
the fake also runs under the system python3 behind sshd.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from typing import Any

ME = "+15550001000"  # the Mac's own account handle

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (id INTEGER PRIMARY KEY AUTOINCREMENT, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS mutations (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, message_id INTEGER NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS faults (
    id INTEGER PRIMARY KEY AUTOINCREMENT, method TEXT NOT NULL, action TEXT NOT NULL,
    arg TEXT NOT NULL, remaining INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT, pid INTEGER, method TEXT, params TEXT);
CREATE TABLE IF NOT EXISTS spawns (
    id INTEGER PRIMARY KEY AUTOINCREMENT, pid INTEGER, at REAL, argv TEXT, ssh_command TEXT);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class FakeDB:
    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = os.fspath(path)
        self.conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def reset(self) -> None:
        tables = ("chats", "messages", "mutations", "faults", "calls", "spawns", "settings")
        for table in tables:
            self.conn.execute(f"DELETE FROM {table}")

    # --- seeding (tests) ---------------------------------------------------

    def add_chat(
        self,
        identifier: str,
        *,
        participants: list[str] | None = None,
        is_group: bool = False,
        name: str = "",
        service: str = "iMessage",
    ) -> dict[str, Any]:
        body = {
            "name": name or identifier,
            "display_name": name if is_group else "",
            "identifier": identifier,
            "guid": f"any;{'+' if is_group else '-'};{identifier}",
            "service": service,
            "last_message_at": now(),
            "is_group": is_group,
            "participants": participants if participants is not None else [identifier],
        }
        cur = self.conn.execute("INSERT INTO chats (body) VALUES (?)", (json.dumps(body),))
        return {"id": cur.lastrowid, **body}

    def add_message(
        self,
        chat_id: int,
        text: str = "",
        *,
        sender: str | None = None,
        is_from_me: bool = False,
        **extra: Any,
    ) -> int:
        """Insert a row as Messages would; extra fields (poll, is_reaction, ...) pass through."""
        chat = self.chat(chat_id)
        if chat is None:
            raise KeyError(f"no chat {chat_id}")
        body = {
            "chat_identifier": chat["identifier"],
            "chat_guid": chat["guid"],
            "chat_name": chat["name"],
            "participants": chat["participants"],
            "is_group": chat["is_group"],
            "guid": str(uuid.uuid4()).upper(),
            "sender": ME if is_from_me else (sender or chat["participants"][0]),
            "is_from_me": is_from_me,
            "text": text,
            "created_at": now(),
            "attachments": [],
            "reactions": [],
            "destination_caller_id": ME,  # present on every row of the Nov 5 Mac
            **extra,
        }
        cur = self.conn.execute(
            "INSERT INTO messages (chat_id, body) VALUES (?, ?)", (chat_id, json.dumps(body))
        )
        assert cur.lastrowid is not None
        return cur.lastrowid

    def mutate(self, message_id: int, kind: str, **changes: Any) -> None:
        """An edit or unsend of an existing row (`kind` is "edited" or "unsent")."""
        row = self.conn.execute("SELECT body FROM messages WHERE id = ?", (message_id,)).fetchone()
        body = {**json.loads(row[0]), **changes}
        if kind == "edited":
            body["edited_at"] = now()
        elif kind == "unsent":
            body.update(text="", attachments=[], unsent_at=now())
        else:
            raise ValueError(kind)
        self.conn.execute(
            "UPDATE messages SET body = ? WHERE id = ?", (json.dumps(body), message_id)
        )
        update = json.dumps({**body, "type": kind})
        self.conn.execute(
            "INSERT INTO mutations (message_id, body) VALUES (?, ?)", (message_id, update)
        )

    def add_fault(self, method: str, action: str, *, times: int = 1, **arg: Any) -> None:
        """Make the fake misbehave on `method` for `times` requests (-1: forever).

        Actions: error (code, message, data), delay (ms), drop (never reply),
        exit (code; die after receiving, before replying), garbage (text: a
        non-JSON stdout line before the reply), stderr (text). Method
        "$startup" fires once when the fake starts (exit, stderr, garbage).
        """
        self.conn.execute(
            "INSERT INTO faults (method, action, arg, remaining) VALUES (?, ?, ?, ?)",
            (method, action, json.dumps(arg), times),
        )

    def set(self, key: str, value: str) -> None:
        """Settings: bridge_ready ("1"/"0", default "0"), db_ready (default "1")."""
        self.conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    # --- reading back (tests) ----------------------------------------------

    def calls(self, method: str | None = None) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT pid, method, params FROM calls ORDER BY id").fetchall()
        out = [{"pid": p, "method": m, "params": json.loads(a)} for p, m, a in rows]
        return [c for c in out if method is None or c["method"] == method]

    def spawns(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT pid, at, argv, ssh_command FROM spawns ORDER BY id")
        return [{"pid": p, "at": t, "argv": json.loads(a), "ssh_command": s} for p, t, a, s in rows]

    # --- used by the fake server -------------------------------------------

    def get(self, key: str, default: str) -> str:
        row = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def record_spawn(self, argv: list[str]) -> None:
        self.conn.execute(
            "INSERT INTO spawns (pid, at, argv, ssh_command) VALUES (?, ?, ?, ?)",
            (os.getpid(), time.time(), json.dumps(argv), os.environ.get("SSH_ORIGINAL_COMMAND")),
        )

    def record_call(self, method: str, params: Any) -> None:
        self.conn.execute(
            "INSERT INTO calls (pid, method, params) VALUES (?, ?, ?)",
            (os.getpid(), method, json.dumps(params)),
        )

    def take_fault(self, method: str) -> tuple[str, dict[str, Any]] | None:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute(
                "SELECT id, action, arg, remaining FROM faults "
                "WHERE method = ? AND remaining != 0 ORDER BY id LIMIT 1",
                (method,),
            ).fetchone()
            if row and row[3] > 0:
                self.conn.execute(
                    "UPDATE faults SET remaining = remaining - 1 WHERE id = ?", (row[0],)
                )
        finally:
            self.conn.execute("COMMIT")
        return (row[1], json.loads(row[2])) if row else None

    def chat(self, chat_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT body FROM chats WHERE id = ?", (chat_id,)).fetchone()
        return {"id": chat_id, **json.loads(row[0])} if row else None

    def chats(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT id, body FROM chats ORDER BY id DESC").fetchall()
        return [{"id": i, **json.loads(b)} for i, b in rows]

    def find_chat(self, field: str, value: str) -> dict[str, Any] | None:
        return next((c for c in self.chats() if c[field] == value), None)

    def messages_after(self, rowid: int, chat_id: int | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT id, chat_id, body FROM messages WHERE id > ?", [rowid]
        if chat_id is not None:
            sql, args = sql + " AND chat_id = ?", [*args, chat_id]
        rows = self.conn.execute(sql + " ORDER BY id", args).fetchall()
        return [{"id": i, "chat_id": c, **json.loads(b)} for i, c, b in rows]

    def max_rowid(self) -> int:
        return int(self.conn.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()[0])

    def mutations_after(self, seq: int) -> list[tuple[int, dict[str, Any]]]:
        rows = self.conn.execute(
            "SELECT m.seq, m.message_id, msg.chat_id, m.body FROM mutations m "
            "JOIN messages msg ON msg.id = m.message_id WHERE m.seq > ? ORDER BY m.seq",
            (seq,),
        ).fetchall()
        return [(s, {"id": i, "chat_id": c, **json.loads(b)}) for s, i, c, b in rows]

    def max_mutation_seq(self) -> int:
        return int(self.conn.execute("SELECT COALESCE(MAX(seq), 0) FROM mutations").fetchone()[0])
