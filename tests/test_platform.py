"""platform.ImsgAdapter against Hermes v2026.9.24's real base class and the fake imsg.

Needs Hermes importable (CI installs the pinned tag; HERMES_IMSG_REQUIRE_HERMES=1 turns
the skip into a failure there). `handle_message` is replaced by a recorder: these tests
check what the adapter hands Hermes, not Hermes's own pipeline.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from conftest import FAKE_IMSG, eventually, run
from fake_imsg.store import FakeDB

if os.environ.get("HERMES_IMSG_REQUIRE_HERMES"):
    import gateway.platforms.base  # noqa: F401  # fail loudly where Hermes must be present
else:
    pytest.importorskip("gateway.platforms.base", reason="needs Hermes Agent v2026.9.24")

from gateway.config import Platform, PlatformConfig
from gateway.platform_registry import PlatformEntry, platform_registry
from gateway.platforms.event import MessageType

import hermes_imsg
from hermes_imsg.events import Secondary
from hermes_imsg.platform import ImsgAdapter, validate_config

COLE = "+15551230001"


class _Ctx:
    """The one PluginContext call the plugin makes, forwarded to Hermes's real registry."""

    def register_platform(self, **kwargs: Any) -> None:
        platform_registry.register(PlatformEntry(source="plugin", plugin_name="imsg", **kwargs))


@pytest.fixture(autouse=True)
def hermes_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    hermes_imsg.register(_Ctx())
    yield home


def make(db: FakeDB, tmp_path: Path, **extra: Any) -> tuple[ImsgAdapter, list[Any]]:
    config = PlatformConfig(
        enabled=True,
        extra={
            "transport": "local",
            "imsg_path": FAKE_IMSG,
            "state_dir": str(tmp_path / "state"),
            "sends_per_minute": 6000,
            **extra,
        },
    )
    adapter = ImsgAdapter(config)
    seen: list[Any] = []

    async def record(event: Any) -> None:
        seen.append(event)

    adapter.handle_message = record  # type: ignore[method-assign]
    return adapter, seen


async def connected(adapter: ImsgAdapter) -> ImsgAdapter:
    assert await adapter.connect() is True
    return adapter


def test_register_makes_the_platform_known() -> None:
    assert Platform("imsg").value == "imsg"
    assert platform_registry.get("imsg").pii_safe is True
    assert validate_config(SimpleNamespace(extra={})) is False
    assert validate_config(SimpleNamespace(extra={"transport": "local"})) is True
    assert validate_config(SimpleNamespace(extra={"host": "mac.local"})) is True


def test_a_dm_becomes_one_event_with_bluebubbles_identity(db: FakeDB, tmp_path: Path) -> None:
    chat = db.add_chat(COLE)
    adapter, seen = make(db, tmp_path)

    async def scenario() -> None:
        await connected(adapter)
        db.add_message(chat["id"], "dinner at 7?")
        await eventually(lambda: len(seen) == 1)
        await adapter.disconnect()

    run(scenario())
    (event,) = seen
    assert event.text == "dinner at 7?" and event.message_type is MessageType.TEXT
    assert event.source.chat_id == chat["guid"] and event.source.chat_type == "dm"
    assert event.source.user_id == COLE and event.source.chat_id_alt == COLE
    assert event.message_id and event.message_id == db.messages_after(0)[-1]["guid"]


def test_own_messages_tapbacks_and_polls_start_no_extra_turns(db: FakeDB, tmp_path: Path) -> None:
    chat = db.add_chat(COLE)
    adapter, seen = make(db, tmp_path)
    secondary: list[Secondary] = []

    async def listen(event: Secondary) -> None:
        secondary.append(event)

    adapter.secondary_listeners.append(listen)

    async def scenario() -> None:
        await connected(adapter)
        assert (await adapter.send(chat["guid"], "on my way")).success  # comes back is_from_me
        poll_row = db.add_message(
            chat["id"],
            "�",
            poll={"kind": "created", "event": "imessage.poll.created", "poll_guid": "POLL-1"},
        )
        db.add_message(chat["id"], "Dinner?", reply_to_guid="CAPTION-TARGET")  # the caption
        db.add_message(
            chat["id"],
            " ",
            poll={"kind": "vote", "event": "imessage.poll.voted", "poll_guid": "POLL-1"},
        )
        db.add_message(
            chat["id"],
            "",
            is_reaction=True,
            reaction_type="like",
            is_reaction_add=True,
            reacted_to_guid="X",
        )
        db.add_message(chat["id"], "control: a plain message after them")
        await eventually(lambda: len(seen) == 2 and len(secondary) == 3)
        assert poll_row
        await adapter.disconnect()

    run(scenario())
    assert [e.text for e in seen] == ["Dinner?", "control: a plain message after them"]
    assert [s.kind for s in secondary] == ["poll", "poll", "reaction"]


def test_killed_child_mid_stream_nothing_missed_nothing_doubled(db: FakeDB, tmp_path: Path) -> None:
    """Scope §6: kill the RPC child mid-stream; on reconnect, a row-id audit shows every
    message delivered exactly once."""
    chat = db.add_chat(COLE)
    adapter, seen = make(db, tmp_path)

    async def scenario() -> None:
        await connected(adapter)
        assert adapter._client is not None
        adapter._client.backoff_initial = 0.2
        for i in range(5):
            db.add_message(chat["id"], f"before {i}")
        await eventually(lambda: len(seen) >= 3)
        os.kill(db.spawns()[-1]["pid"], signal.SIGKILL)
        for i in range(5):
            db.add_message(chat["id"], f"while down {i}")  # written before the child is back
        await eventually(lambda: adapter._client is not None and adapter._client.connects == 2)
        for i in range(2):
            db.add_message(chat["id"], f"after {i}")
        await eventually(lambda: len(seen) == 12)
        await asyncio.sleep(0.3)  # anything doubled would show up now
        await adapter.disconnect()

    run(scenario())
    expected = [r["guid"] for r in db.messages_after(0)]
    assert [e.message_id for e in seen] == expected  # each once, in row order
    assert len(db.spawns()) == 2  # control: the child really was replaced


def test_restart_resumes_after_the_cursor_and_first_start_skips_history(
    db: FakeDB, tmp_path: Path
) -> None:
    chat = db.add_chat(COLE)
    db.add_message(chat["id"], "old history, before the plugin ever ran")
    first, seen_first = make(db, tmp_path)

    async def phase_one() -> None:
        await connected(first)
        db.add_message(chat["id"], "live 1")
        await eventually(lambda: len(seen_first) == 1)
        await first.disconnect()

    run(phase_one())
    for i in range(3):
        db.add_message(chat["id"], f"while Hermes was stopped {i}")
    second, seen_second = make(db, tmp_path)

    async def phase_two() -> None:
        await connected(second)
        await eventually(lambda: len(seen_second) == 3)
        await asyncio.sleep(0.3)
        await second.disconnect()

    run(phase_two())
    assert [e.text for e in seen_first] == ["live 1"]  # no history on the very first start
    assert [e.text for e in seen_second] == [f"while Hermes was stopped {i}" for i in range(3)]


def test_send_results(db: FakeDB, tmp_path: Path) -> None:
    chat = db.add_chat(COLE)
    adapter, _ = make(db, tmp_path)

    async def scenario() -> list[Any]:
        await connected(adapter)
        results = [
            await adapter.send(chat["guid"], "hello"),
            await adapter.send("any;-;+15559999999", "nobody there"),
            await adapter.send(chat["guid"], "a" * 4000 + "\n\n" + "b" * 100),
        ]
        await adapter.disconnect()
        return results

    ok, unknown, long = run(scenario())
    assert ok.success and ok.message_id
    assert not unknown.success and unknown.retryable is False
    assert long.success and len([c for c in db.calls("send") if c["params"].get("text")]) == 4
    plain = adapter.format_message("**bold** and [a link](https://example.com)")
    assert "**" not in plain and "[" not in plain  # Messages shows no markdown
    assert "bold" in plain and "https://example.com" in plain  # the link target survives


def test_group_messages_need_a_mention_when_configured(db: FakeDB, tmp_path: Path) -> None:
    group = db.add_chat("chat123", participants=[COLE, "+15551230002"], is_group=True, name="Fam")
    adapter, seen = make(db, tmp_path, require_mention=True)

    async def scenario() -> None:
        await connected(adapter)
        db.add_message(group["id"], "anyone seen the keys?", sender=COLE)
        db.add_message(group["id"], "hermes, what's for dinner?", sender=COLE)
        await eventually(lambda: len(seen) == 1)
        await asyncio.sleep(0.2)
        await adapter.disconnect()

    run(scenario())
    (event,) = seen
    assert event.text == "what's for dinner?" and event.source.chat_type == "group"
    assert event.source.chat_name == "Fam"


def test_local_attachments_are_copied_into_hermes_cache(db: FakeDB, tmp_path: Path) -> None:
    from PIL import Image

    picture = tmp_path / "Messages" / "IMG_0001.png"
    picture.parent.mkdir()
    Image.new("RGB", (4, 4), "red").save(picture)
    chat = db.add_chat(COLE)
    adapter, seen = make(db, tmp_path)
    attachment = {
        "filename": picture.name,
        "transfer_name": picture.name,
        "mime_type": "image/png",
        "total_bytes": picture.stat().st_size,
        "original_path": str(picture),
        "missing": False,
    }

    async def scenario() -> None:
        await connected(adapter)
        db.add_message(chat["id"], "", attachments=[attachment])
        await eventually(lambda: len(seen) == 1)
        await adapter.disconnect()

    run(scenario())
    (event,) = seen
    assert event.text == "(attachment)" and event.message_type is MessageType.PHOTO
    (cached,) = event.media_urls
    assert Path(cached).read_bytes() == picture.read_bytes() and Path(cached) != picture
    assert picture.exists()  # the Mac's copy is never moved


def _ssh_adapter(ssh: SimpleNamespace, tmp_path: Path, key: str) -> tuple[ImsgAdapter, list[Any]]:
    config = PlatformConfig(enabled=True, extra={
        "host": "fakemac", "ssh_config": ssh.config, "identity_file": key,
        "imsg_path": FAKE_IMSG, "state_dir": str(tmp_path / "state"), "sends_per_minute": 6000,
    })  # fmt: skip
    adapter = ImsgAdapter(config)
    seen: list[Any] = []

    async def record(event: Any) -> None:
        seen.append(event)

    adapter.handle_message = record  # type: ignore[method-assign]
    return adapter, seen


def test_over_ssh_files_go_both_ways_through_the_gate(ssh: SimpleNamespace, tmp_path: Path) -> None:
    from PIL import Image

    chat = ssh.db.add_chat(COLE)
    folder = ssh.home / "Library" / "Messages" / "Attachments" / "cd" / "34"
    folder.mkdir(parents=True, exist_ok=True)
    picture = folder / "IMG 0002.png"
    Image.new("RGB", (4, 4), "blue").save(picture)
    inbound = {
        "filename": picture.name,
        "transfer_name": picture.name,
        "mime_type": "image/png",
        "total_bytes": picture.stat().st_size,
        "original_path": str(picture),
        "missing": False,
    }
    outgoing = tmp_path / "report.pdf"
    outgoing.write_bytes(b"%PDF-1.4 tiny")
    adapter, seen = _ssh_adapter(ssh, tmp_path, ssh.gated_key)

    async def scenario() -> tuple[Any, Any]:
        await connected(adapter)
        ssh.db.add_message(chat["id"], "over ssh")
        ssh.db.add_message(chat["id"], "", attachments=[inbound])
        await eventually(lambda: len(seen) == 2)
        text = await adapter.send(chat["guid"], "reply over ssh")
        doc = await adapter.send_document(chat["guid"], str(outgoing), caption="the report")
        await adapter.disconnect()
        return text, doc

    text, doc = run(scenario())
    assert [e.text for e in seen] == ["over ssh", "(attachment)"]
    (cached,) = seen[1].media_urls
    assert Path(cached).read_bytes() == picture.read_bytes() and picture.exists()
    assert seen[1].message_type is MessageType.PHOTO
    assert text.success and doc.success
    (file_send,) = [c["params"] for c in ssh.db.calls("send") if "file" in c["params"]]
    remote = Path(file_send["file"])
    outbox = ssh.home / "Library" / "Caches" / "hermes-imsg" / "outbox"
    assert remote.resolve().parent.parent == outbox.resolve()
    assert remote.name == "report.pdf" and remote.read_bytes() == b"%PDF-1.4 tiny"
    assert file_send["chat_guid"] == chat["guid"] and file_send["text"] == "the report"


def test_over_ssh_without_the_gate_text_still_works_and_files_fail_loudly(
    ssh: SimpleNamespace, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    chat = ssh.db.add_chat(COLE)
    attachment = {
        "filename": "a.pdf",
        "transfer_name": "a.pdf",
        "mime_type": "application/pdf",
        "total_bytes": 10,
        "original_path": "/Users/x/a.pdf",
        "missing": False,
    }
    outgoing = tmp_path / "a.txt"
    outgoing.write_text("x")
    adapter, seen = _ssh_adapter(ssh, tmp_path, ssh.key)  # the key forced to plain imsg rpc

    async def scenario() -> Any:
        await connected(adapter)
        ssh.db.add_message(chat["id"], "", attachments=[attachment])
        await eventually(lambda: len(seen) == 1)
        result = await adapter.send_document(chat["guid"], str(outgoing))
        await adapter.disconnect()
        return result

    with caplog.at_level("WARNING", logger="hermes_imsg"):
        result = run(scenario())
    assert seen[0].text == "(attachment)" and seen[0].media_urls == []
    assert "could not read an attachment" in caplog.text
    assert not result.success and "gate" in (result.error or "")


def test_observe_only_records_every_row_once_and_never_sends(db: FakeDB, tmp_path: Path) -> None:
    """The parallel run beside another relay: rows are recorded (no text), no turn starts,
    nothing is sent, and the record survives a killed child without gaps or doubles."""
    chat = db.add_chat(COLE)
    adapter, seen = make(db, tmp_path, observe_only=True)

    async def scenario() -> Any:
        await connected(adapter)
        assert adapter._client is not None
        adapter._client.backoff_initial = 0.2
        db.add_message(chat["id"], "secret one")
        db.add_message(chat["id"], "", is_reaction=True, reaction_type="like", reacted_to_guid="X")
        await eventually(lambda: adapter.counters["secondary"] == 1)
        os.kill(db.spawns()[-1]["pid"], signal.SIGKILL)
        db.add_message(chat["id"], "secret two")
        await eventually(lambda: adapter.counters["received"] == 2)
        result = await adapter.send(chat["guid"], "must not go out")
        await asyncio.sleep(0.3)
        await adapter.disconnect()
        return result

    result = run(scenario())
    assert seen == [] and not result.success and "observe-only" in (result.error or "")
    assert db.calls("send") == []  # control for the refusal: imsg never saw a send
    log = (tmp_path / "state" / "observe.jsonl").read_text()
    records = [json.loads(line) for line in log.splitlines()]
    assert [r["kind"] for r in records] == ["inbound", "reaction", "inbound"]
    assert [r["guid"] for r in records] == [r["guid"] for r in db.messages_after(0)]
    assert records[0]["chat_guid"] == chat["guid"] and records[0]["sender"] == COLE
    assert "secret" not in log  # no message text in the record
