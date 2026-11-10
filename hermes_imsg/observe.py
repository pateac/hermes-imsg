"""Watching without answering, and checking what was seen against another relay.

Shared with the platform: `subscribe` (resume from the cursor) and `write_record` (one line
per row, never message text). The standalone observer, `python -m hermes_imsg observe`, runs
the same subscribe, replay check, classification and record as the plugin's observe-only mode,
in its own process: a week beside another relay then touches nothing in the Hermes that is
serving. `audit` compares the record with the message ids that relay handed Hermes, read
from Hermes's own state databases.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import events, status
from .cursor import Cursor
from .rpc import RpcClient
from .transport import Transport

log = logging.getLogger(__name__)

RECORD = "observe.jsonl"
GRACE = 300.0  # s: the record's clock and Hermes's differ by webhook and queue delay


async def subscribe(client: RpcClient, cursor: Cursor) -> None:
    """Resume from the cursor: imsg replays every row after it, then streams live."""
    database = (client.status or {}).get("database") or {}
    cursor.bind(str(database.get("path") or ""))
    params: dict[str, Any] = {"include_reactions": True, "attachments": True}
    if cursor.rowid:
        params["since_rowid"] = cursor.rowid
    await client.call("watch.subscribe", params)


def write_record(path: Path, event: events.Inbound | events.Secondary) -> None:
    """One JSON line per row: time, kind, row id, message GUID, chat GUID, sender. No text."""
    kind = "inbound" if isinstance(event, events.Inbound) else event.kind
    line = {
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "kind": kind,
        "rowid": event.rowid,
        "guid": event.guid,
        "chat_guid": event.chat_guid,
        "sender": event.sender,
    }
    with open(path, "a") as f:
        f.write(json.dumps(line) + "\n")


async def run(transport: Transport, state_dir: Path) -> None:
    """Record every row after the cursor until cancelled; reconnects by itself."""
    state_dir.mkdir(parents=True, exist_ok=True)
    cursor = Cursor(state_dir / "cursor.json")
    record = state_dir / RECORD

    async def on_connect(client: RpcClient) -> None:
        await subscribe(client, cursor)

    async with RpcClient(transport, on_connect=on_connect) as client:
        watcher = asyncio.create_task(status.watch(client))
        try:
            while True:
                note = await client.notifications.get()
                if note.method == "watch.overflow":
                    log.warning("watch buffer overflowed; resubscribing from the cursor")
                    await subscribe(client, cursor)
                    continue
                row = note.params.get("message")
                if note.method != "message" or not isinstance(row, dict):
                    continue
                rowid = events.new_rowid(row)
                if rowid is not None and rowid <= cursor.rowid:
                    continue  # a replay after a reconnect or restart
                event = events.classify(row)
                if event is not None:
                    write_record(record, event)
                if rowid is not None:
                    cursor.advance(rowid)  # after the line is written: at-least-once
        finally:
            watcher.cancel()


# --- the audit ----------------------------------------------------------------------------

DELIVERED_SQL = (
    "SELECT m.platform_message_id, m.timestamp FROM messages m JOIN sessions s "
    "ON s.id = m.session_id WHERE s.source = ? AND m.role = 'user' AND m.timestamp BETWEEN ? AND ?"
)


@dataclass
class Audit:
    since: float
    until: float
    delivered: int = 0  # the relay's messages in the window, with an id
    unidentified: int = 0  # the relay's messages in the window with no id (cannot be checked)
    recorded: int = 0  # inbound rows recorded in the window
    missed: list[tuple[str, float]] = field(default_factory=list)
    doubled: list[str] = field(default_factory=list)
    extra: list[dict[str, Any]] = field(default_factory=list)
    unreadable_lines: int = 0

    @property
    def passed(self) -> bool:
        return not self.missed and not self.doubled


def _epoch(at: str) -> float:
    return datetime.fromisoformat(at).timestamp()


def audit(
    record: Path,
    hermes_dbs: list[Path],
    *,
    source: str = "bluebubbles",
    since: float | None = None,
    until: float | None = None,
) -> Audit:
    """Missed: the relay handed Hermes a message, inside the window, that the record lacks.
    Doubled: an inbound row recorded twice. Extra: recorded inside the window but not handed
    over by the relay within GRACE of it (each needs a reason). Missed is judged strictly
    inside the window and extra against a window widened by GRACE, so clock skew at the
    edges never reads as a failure."""
    rows: list[dict[str, Any]] = []
    unreadable = 0
    for line in record.read_text().splitlines():
        try:
            row = json.loads(line)
            row["ts"] = _epoch(row["at"])
            rows.append(row)
        except (ValueError, KeyError, TypeError):
            unreadable += 1
    if not rows:
        raise ValueError(f"{record} has no readable lines")
    since = min(r["ts"] for r in rows) if since is None else since
    until = max(r["ts"] for r in rows) if until is None else until
    result = Audit(since=since, until=until, unreadable_lines=unreadable)
    inbound = [r for r in rows if r.get("kind") == "inbound"]
    result.doubled = sorted(g for g, n in Counter(r["guid"] for r in inbound).items() if n > 1)
    recorded = {r["guid"] for r in inbound}

    delivered: dict[str, float] = {}  # widened by GRACE; `inside` is the strict window
    for db in hermes_dbs:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)  # read-only: never writes
        try:
            for message_id, ts in conn.execute(
                DELIVERED_SQL, (source, since - GRACE, until + GRACE)
            ):
                if message_id:
                    delivered[str(message_id)] = float(ts)
                elif since <= ts <= until:
                    result.unidentified += 1
        finally:
            conn.close()
    inside = {g: t for g, t in delivered.items() if since <= t <= until}
    result.delivered = len(inside)
    result.missed = sorted(
        ((g, t) for g, t in inside.items() if g not in recorded), key=lambda m: m[1]
    )
    in_window = [r for r in inbound if since <= r["ts"] <= until]
    result.recorded = len(in_window)
    result.extra = [r for r in in_window if r["guid"] not in delivered]
    return result
