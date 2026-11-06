"""The resume point: the last imsg row handed to Hermes, kept on disk.

On connect the platform subscribes with `since_rowid` set to this cursor, so
imsg replays every row after it and then streams live: nothing is missed across
a restart or a reconnect. A row at or below the cursor is a replay and is
dropped, so nothing is delivered twice. The cursor is written after Hermes has
accepted a row (at-least-once): only a crash between the two replays a row.

Row ids belong to one Messages database (imsg `docs/rpc.md`). When imsg reports
a different database path from the saved one, the cursor is discarded and the
stream starts at the newest row.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)


class Cursor:
    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self.rowid = 0  # 0: no history yet; start at the newest row
        self.database = ""
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            log.error("cursor %s unreadable (%s); starting at the newest row", self.path, exc)
            return
        if isinstance(data, dict):
            if type(data.get("rowid")) is int and data["rowid"] > 0:
                self.rowid = data["rowid"]
            if isinstance(data.get("database"), str):
                self.database = data["database"]

    def bind(self, database: str) -> None:
        """Keep the saved position only if it came from this database."""
        if database == self.database:
            return
        if self.database and self.rowid:
            log.warning("Messages database changed; discarding the saved position")
            self.rowid = 0
        self.database = database
        self._save()

    def advance(self, rowid: int) -> None:
        if rowid > self.rowid:
            self.rowid = rowid
            self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        with open(tmp, "w") as f:
            json.dump({"rowid": self.rowid, "database": self.database}, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)  # atomic: a crash leaves the old cursor or the new one
