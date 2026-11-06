"""events.classify: every imsg row shape maps to exactly one outcome."""

from __future__ import annotations

from typing import Any

from hermes_imsg.events import Inbound, Secondary, classify

COLE = "+15551230001"
DM = f"any;-;{COLE}"


def row(rowid: int, text: str = "hi", **extra: Any) -> dict[str, Any]:
    base = {
        "id": rowid,
        "chat_id": 1,
        "chat_identifier": COLE,
        "chat_guid": DM,
        "chat_name": COLE,
        "participants": [COLE],
        "is_group": False,
        "guid": f"GUID-{rowid}",
        "sender": COLE,
        "is_from_me": False,
        "text": text,
        "created_at": "2026-11-05T20:00:00.000Z",
        "attachments": [],
        "reactions": [],
        "destination_caller_id": "+15550001000",
    }
    return {**base, **extra}


def test_plain_message_is_inbound() -> None:
    event = classify(row(10, "dinner?"))
    assert isinstance(event, Inbound)
    assert (event.rowid, event.guid, event.chat_guid, event.sender) == (10, "GUID-10", DM, COLE)
    assert event.text == "dinner?" and event.is_group is False and event.attachments == ()


def test_group_is_detected_from_the_guid() -> None:
    event = classify(row(11, chat_guid="any;+;chat123", is_group=False))
    assert isinstance(event, Inbound) and event.is_group is True


def test_own_messages_are_dropped() -> None:
    assert classify(row(12, is_from_me=True, sender="+15550001000")) is None
    assert isinstance(classify(row(12)), Inbound)  # control: the same row from someone else


def test_tapback_is_secondary_with_its_target() -> None:
    event = classify(
        row(
            13,
            "",
            is_reaction=True,
            reaction_type="like",
            is_reaction_add=True,
            reacted_to_guid="GUID-10",
        )
    )
    assert isinstance(event, Secondary)
    assert (event.kind, event.target_guid, event.advances_cursor) == ("reaction", "GUID-10", True)
    assert event.detail == {"reaction_type": "like", "is_reaction_add": True}


def test_edits_and_unsends_are_secondary_and_do_not_advance() -> None:
    for kind in ("edited", "unsent"):
        event = classify(row(10, "fixed", type=kind))
        assert isinstance(event, Secondary)
        assert (event.kind, event.target_guid, event.advances_cursor) == (kind, "GUID-10", False)


def test_one_poll_starts_exactly_one_turn() -> None:
    """Phase 0 (docs/phase0-check.md): a poll is a created row, a caption row, and vote rows;
    poll rows carry placeholder text. Only the caption may reach the agent."""
    poll_meta = {"bundle_id": "com.apple.messages.Polls", "payload_bytes": 900}
    created = row(
        20,
        "�",
        balloon_bundle_id="com.apple.messages.Polls",
        poll={
            "kind": "created",
            "event": "imessage.poll.created",
            "poll_guid": "POLL-1",
            "question": "Dinner?",
            "creator": COLE,
            "participants": [COLE],
            "options": [{"id": "o1", "text": "Pizza"}, {"id": "o2", "text": "Sushi"}],
            "metadata": poll_meta,
        },
    )
    caption = row(21, "Dinner?", reply_to_guid="GUID-20")
    vote = {"event_type": "add", "option_id": "o1", "option_text": "Pizza", "participant": COLE}
    voted = row(
        22,
        " ",
        balloon_bundle_id="com.apple.messages.Polls",
        poll={
            "kind": "vote",
            "event": "imessage.poll.voted",
            "poll_guid": "POLL-1",
            "original_guid": "GUID-20",
            "participants": [COLE],
            "vote": vote,
            "votes": [vote],
            "metadata": poll_meta,
        },
    )
    outcomes = [classify(r) for r in (created, caption, voted)]
    inbound = [o for o in outcomes if isinstance(o, Inbound)]
    assert len(inbound) == 1 and inbound[0].text == "Dinner?"
    assert inbound[0].reply_to_guid == "GUID-20"
    polls = [o for o in outcomes if isinstance(o, Secondary)]
    assert [p.kind for p in polls] == ["poll", "poll"]
    assert [p.detail["event"] for p in polls] == ["imessage.poll.created", "imessage.poll.voted"]
    assert all(p.target_guid == "POLL-1" for p in polls)


def test_attachment_only_message_is_inbound() -> None:
    attachment = {
        "filename": "IMG_1.HEIC",
        "transfer_name": "IMG_1.HEIC",
        "mime_type": "image/heic",
        "total_bytes": 2048,
        "original_path": "/Users/x/Library/Messages/A/IMG_1.HEIC",
        "missing": False,
    }
    event = classify(row(30, "", attachments=[attachment]))
    assert isinstance(event, Inbound) and event.text == ""
    (a,) = event.attachments
    assert (a.filename, a.mime_type, a.total_bytes, a.missing) == (
        "IMG_1.HEIC",
        "image/heic",
        2048,
        False,
    )
    assert a.path is not None and a.path.endswith("IMG_1.HEIC")


def test_empty_rows_and_unroutable_rows_are_dropped() -> None:
    assert classify(row(40, "")) is None  # a group rename arrives like this (imsg #372)
    assert classify(row(41, "   ")) is None
    assert classify(row(42, sender="")) is None
    assert classify(row(43, guid="")) is None
    assert classify(row(44, chat_guid="")) is None
    assert classify({**row(45), "id": "45"}) is None  # imsg row ids are integers
    assert isinstance(classify(row(46, "x")), Inbound)  # control
