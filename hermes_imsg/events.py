"""What each imsg `message` notification means for the agent.

Every row is exactly one of:

- `Inbound`: a new message from someone else. It becomes one Hermes turn.
- `Secondary`: a tapback, an edit or unsend, or a native poll row (created or
  voted). Reported to listeners, never a new turn: a poll arrives as several
  rows and poll rows carry placeholder text (U+FFFD or a space), so treating
  them as messages would start several turns for one poll (phase 0 findings).
  The poll's visible caption is a separate ordinary row, and that is the one
  turn the agent sees.
- `None`: our own outbound row (`is_from_me`), or a row with nothing to say.

Pure functions over the row dicts imsg emits (imsg `docs/json.md`); no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any


@dataclass(frozen=True)
class Attachment:
    filename: str
    mime_type: str
    total_bytes: int
    path: str | None  # on the Mac; None when imsg has no local file
    missing: bool


@dataclass(frozen=True)
class Inbound:
    rowid: int
    guid: str
    chat_guid: str
    chat_identifier: str
    chat_name: str
    is_group: bool
    sender: str
    text: str
    created_at: str
    reply_to_guid: str | None = None
    attachments: tuple[Attachment, ...] = ()


@dataclass(frozen=True)
class Secondary:
    kind: str  # "reaction", "edited", "unsent" or "poll"
    rowid: int
    guid: str
    chat_guid: str
    sender: str
    is_from_me: bool
    target_guid: str | None  # the message reacted to, edited, or the poll
    detail: dict[str, Any] = field(default_factory=dict)
    advances_cursor: bool = True  # edits and unsends reuse an old row id


def _str(row: dict[str, Any], key: str) -> str:
    value = row.get(key)
    return value if isinstance(value, str) else ""


def _attachments(row: dict[str, Any]) -> tuple[Attachment, ...]:
    out = []
    for a in row.get("attachments") or []:
        if not isinstance(a, dict):
            continue
        size = a.get("total_bytes")
        out.append(
            Attachment(
                filename=_str(a, "transfer_name") or _str(a, "filename"),
                mime_type=_str(a, "mime_type"),
                total_bytes=size if type(size) is int else 0,
                path=_str(a, "original_path") or None,
                missing=a.get("missing") is True,
            )
        )
    return tuple(out)


def classify(row: dict[str, Any]) -> Inbound | Secondary | None:
    rowid, guid, chat_guid = row.get("id"), _str(row, "guid"), _str(row, "chat_guid")
    if type(rowid) is not int or not guid or not chat_guid:
        return None  # not a row we can route or resume from
    sender, from_me = _str(row, "sender"), row.get("is_from_me") is True

    def secondary(kind: str, target: str | None, detail: dict[str, Any]) -> Secondary:
        return Secondary(kind, rowid, guid, chat_guid, sender, from_me, target, detail)

    mutation = row.get("type")
    if mutation in ("edited", "unsent"):
        edit = secondary(mutation, guid, {"text": _str(row, "text")})
        return replace(edit, advances_cursor=False)
    if row.get("is_reaction") is True:
        keys = ("reaction_type", "reaction_emoji", "is_reaction_add")
        detail = {k: row[k] for k in keys if k in row}
        return secondary("reaction", _str(row, "reacted_to_guid") or None, detail)
    poll = row.get("poll")
    if isinstance(poll, dict):
        target = _str(poll, "poll_guid") or _str(poll, "original_guid") or None
        return secondary("poll", target, poll)
    if from_me:
        return None
    text, attachments = _str(row, "text"), _attachments(row)
    if not sender or not (text.strip() or attachments):
        return None  # e.g. a group rename, which imsg surfaces as an empty row
    return Inbound(
        rowid=rowid,
        guid=guid,
        chat_guid=chat_guid,
        chat_identifier=_str(row, "chat_identifier"),
        chat_name=_str(row, "chat_name"),
        is_group=row.get("is_group") is True or ";+;" in chat_guid,
        sender=sender,
        text=text,
        created_at=_str(row, "created_at"),
        reply_to_guid=_str(row, "reply_to_guid") or _str(row, "thread_originator_guid") or None,
        attachments=attachments,
    )
