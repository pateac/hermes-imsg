"""rpc.py against the fake imsg: framing, ids, timeouts, reconnect, error mapping."""

from __future__ import annotations

import asyncio
import itertools
import logging
import os
import signal

import pytest
from conftest import FAKE_IMSG, eventually, run
from fake_imsg.store import FakeDB

from hermes_imsg import rpc
from hermes_imsg.rpc import RpcClient
from hermes_imsg.transport import LocalTransport


def test_call_returns_result_and_sends_protocol_version(local: LocalTransport, db: FakeDB) -> None:
    chat = db.add_chat("+15551230001")

    async def scenario() -> None:
        async with RpcClient(local) as client:
            await client.wait_ready(10)
            assert client.status is not None and client.status["protocol_version"] == 1
            result = await client.call("chats.get", {"chat_id": chat["id"]})
            assert result["guid"] == "any;-;+15551230001"

    run(scenario())
    assert db.calls("initialize")[0]["params"] == {"protocol_version": 1}


def test_concurrent_calls_are_matched_by_id_even_out_of_order(
    local: LocalTransport, db: FakeDB
) -> None:
    db.add_chat("+15551230001")
    db.add_fault("chats.list", "delay", ms=400)

    async def scenario() -> list[str]:
        order: list[str] = []
        async with RpcClient(local) as client:
            await client.wait_ready(10)

            async def call(method: str) -> object:
                result = await client.call(method)
                order.append(method)
                return result

            chats, status = await asyncio.gather(call("chats.list"), call("status"))
        assert "chats" in chats and "protocol_version" in status  # type: ignore[operator]
        return order

    assert run(scenario()) == ["status", "chats.list"]  # the slow one finished last


def test_timeout_then_late_reply_is_ignored(local: LocalTransport, db: FakeDB) -> None:
    db.add_fault("status", "delay", ms=800)

    async def scenario() -> None:
        async with RpcClient(local) as client:
            await client.wait_ready(10)
            with pytest.raises(rpc.RpcTimeout) as exc:
                await client.call("status", timeout=0.2)
            assert exc.value.retry_safe is False
            await asyncio.sleep(0.8)  # the late reply arrives while nothing waits for it
            assert (await client.call("chats.list"))["chats"] == []

    run(scenario())


@pytest.mark.parametrize(
    ("method", "params", "setup", "error", "retry_safe", "text"),
    [
        ("chats.get", {"chat_id": 99}, None, rpc.InvalidParams, True, "Chat not found: 99"),
        ("chats.list", {"limit": "5"}, None, rpc.InvalidParams, True, "limit must be int"),
        ("chats.list", {"chatId": 1}, None, rpc.InvalidParams, True, "Unknown parameter"),
        ("no.such", {}, None, rpc.MethodNotFound, True, "does not implement"),
        ("chats.list", {}, ("db_ready", "0"), rpc.DatabaseUnavailable, True, "unable to open"),
        ("handles.check", {}, None, rpc.BridgeUnavailable, True, "imsg launch"),
        ("tapback", {}, None, rpc.RpcError, True, "ready lock is absent"),
        (
            "send",
            {"chat_guid": "any;-;nobody", "text": "x"},
            None,
            rpc.RpcError,
            True,
            "Chat not found",
        ),
    ],
)
def test_error_mapping(
    local: LocalTransport,
    db: FakeDB,
    method: str,
    params: dict[str, object],
    setup: tuple[str, str] | None,
    error: type[rpc.RpcError],
    retry_safe: bool,
    text: str,
) -> None:
    if setup:
        db.set(*setup)

    async def scenario() -> rpc.RpcError:
        async with RpcClient(local) as client:
            await client.wait_ready(10)
            with pytest.raises(rpc.RpcError) as exc:
                await client.call(method, params)
            return exc.value

    err = run(scenario())
    assert type(err) is error
    assert err.retry_safe is retry_safe
    assert text in str(err)


def test_delivery_unknown_is_not_retry_safe(local: LocalTransport, db: FakeDB) -> None:
    data = {
        "retry_safe": False,
        "disposition": "may_have_completed",
        "transport": "applescript",
        "operation": "send",
        "detail": "no receipt",
    }
    db.add_fault("send", "error", code=-32001, message="Delivery outcome unknown", data=data)
    chat = db.add_chat("+15551230001")

    async def scenario() -> rpc.RpcError:
        async with RpcClient(local) as client:
            await client.wait_ready(10)
            with pytest.raises(rpc.DeliveryUnknown) as exc:
                await client.call("send", {"chat_id": chat["id"], "text": "hi"})
            return exc.value

    err = run(scenario())
    assert err.retry_safe is False and err.data["disposition"] == "may_have_completed"


def test_non_json_stdout_line_is_skipped_without_logging_it(
    local: LocalTransport, db: FakeDB, caplog: pytest.LogCaptureFixture
) -> None:
    db.add_fault("status", "garbage", text="NOT-JSON-PAYLOAD")

    async def scenario() -> None:
        async with RpcClient(local) as client:
            await client.wait_ready(10)
            assert (await client.call("status"))["protocol_version"] == 1

    with caplog.at_level(logging.DEBUG, logger="hermes_imsg"):
        run(scenario())
    assert "ignored a non-JSON line" in caplog.text
    assert "NOT-JSON-PAYLOAD" not in caplog.text


def test_lines_larger_than_asyncio_default_limit(local: LocalTransport, db: FakeDB) -> None:
    chat = db.add_chat("+15551230001")
    big = "x" * 300_000
    assert len(big) > 2**16  # control: over asyncio's default 64 KiB readline limit
    db.add_message(chat["id"], big)

    async def scenario() -> str:
        async with RpcClient(local) as client:
            await client.wait_ready(10)
            result = await client.call("messages.history", {"chat_id": chat["id"]})
            return str(result["messages"][0]["text"])

    assert run(scenario()) == big


def test_notifications_and_resubscribe_after_the_child_is_killed(
    local: LocalTransport, db: FakeDB
) -> None:
    chat = db.add_chat("+15551230001")

    async def on_connect(client: RpcClient) -> None:
        await client.call("watch.subscribe", {})

    async def scenario() -> list[str]:
        client = RpcClient(local, on_connect=on_connect, backoff_initial=0.05)
        async with client:
            await client.wait_ready(10)
            db.add_message(chat["id"], "before")
            first = await asyncio.wait_for(client.notifications.get(), 5)
            os.kill(db.spawns()[-1]["pid"], signal.SIGKILL)
            await eventually(lambda: client.connects == 2)
            db.add_message(chat["id"], "after")
            second = await asyncio.wait_for(client.notifications.get(), 5)
        return [first.params["message"]["text"], second.params["message"]["text"]]

    assert run(scenario()) == ["before", "after"]
    assert len(db.spawns()) == 2
    assert len(db.calls("watch.subscribe")) == 2  # on_connect ran again on the new child


def test_pending_call_fails_as_connection_lost_when_the_child_dies(
    local: LocalTransport, db: FakeDB
) -> None:
    chat = db.add_chat("+15551230001")
    db.add_fault("send", "exit", code=3)  # dies after receiving, before replying

    async def scenario() -> rpc.ConnectionLost:
        async with RpcClient(local, backoff_initial=0.05) as client:
            await client.wait_ready(10)
            with pytest.raises(rpc.ConnectionLost) as exc:
                await client.call("send", {"chat_id": chat["id"], "text": "hi"})
            await eventually(lambda: client.connects == 2)
            return exc.value

    err = run(scenario())
    assert err.retry_safe is False
    assert len(db.calls("send")) == 1  # imsg did receive it: "may have run" is the honest answer


def test_backoff_grows_while_the_child_keeps_failing(local: LocalTransport, db: FakeDB) -> None:
    db.add_fault("$startup", "exit", times=3, code=1)

    async def scenario() -> None:
        async with RpcClient(local, backoff_initial=0.1) as client:
            await client.wait_ready(15)
            assert client.connects == 1

    run(scenario())
    starts = [s["at"] for s in db.spawns()]
    assert len(starts) == 4
    gaps = [b - a for a, b in itertools.pairwise(starts)]
    assert gaps[0] >= 0.1 and gaps[2] > gaps[0] + 0.2  # about 0.1, 0.2, 0.4 s plus start-up


def test_on_connect_failure_restarts_the_child(local: LocalTransport, db: FakeDB) -> None:
    attempts = 0

    async def on_connect(client: RpcClient) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("first subscribe failed")

    async def scenario() -> None:
        async with RpcClient(local, on_connect=on_connect, backoff_initial=0.05) as client:
            await client.wait_ready(10)
            assert client.connects == 1

    run(scenario())
    assert attempts == 2 and len(db.spawns()) == 2


def test_not_connected_when_imsg_cannot_start(
    tmp_path: os.PathLike[str], caplog: pytest.LogCaptureFixture
) -> None:
    missing = LocalTransport(os.path.join(tmp_path, "no-imsg-here"))

    async def scenario() -> rpc.NotConnected:
        async with RpcClient(missing, backoff_initial=0.05) as client:
            with pytest.raises(rpc.NotConnected) as exc:
                await client.call("status", timeout=0.3)
            return exc.value

    with caplog.at_level(logging.ERROR, logger="hermes_imsg"):
        err = run(scenario())
    assert err.retry_safe is True
    assert "No such file" in caplog.text


def test_close_drains_an_in_flight_call_then_refuses_new_ones(
    local: LocalTransport, db: FakeDB
) -> None:
    db.add_fault("status", "delay", ms=300)

    async def scenario() -> None:
        client = RpcClient(local)
        client.start()
        await client.wait_ready(10)
        in_flight = asyncio.create_task(client.call("status"))
        await asyncio.sleep(0.05)
        await client.close()
        assert (await in_flight)["protocol_version"] == 1  # imsg answers before exiting on EOF
        with pytest.raises(rpc.NotConnected):
            await client.call("status")

    run(scenario())
    assert len(db.spawns()) == 1  # closing is not a crash: no respawn


def test_message_bodies_never_reach_the_log(
    local: LocalTransport, db: FakeDB, caplog: pytest.LogCaptureFixture
) -> None:
    chat = db.add_chat("+15551230001")

    async def on_connect(client: RpcClient) -> None:
        await client.call("watch.subscribe", {"include_reactions": True})

    async def scenario() -> None:
        async with RpcClient(local, on_connect=on_connect) as client:
            await client.wait_ready(10)
            await client.call("send", {"chat_id": chat["id"], "text": "OUTBOUND-SECRET"})
            db.add_message(chat["id"], "INBOUND-SECRET")
            texts = set()
            while len(texts) < 2:
                note = await asyncio.wait_for(client.notifications.get(), 5)
                texts.add(note.params["message"]["text"])
            assert texts == {"OUTBOUND-SECRET", "INBOUND-SECRET"}  # control: the bodies flowed

    with caplog.at_level(logging.DEBUG, logger="hermes_imsg"):
        run(scenario())
    assert "-> send" in caplog.text  # control: DEBUG logging was on
    assert "SECRET" not in caplog.text


def test_fake_rejects_unknown_watch_params_like_imsg(local: LocalTransport, db: FakeDB) -> None:
    """Control for the fake itself: camelCase that imsg rejects is rejected here too."""

    async def scenario() -> None:
        async with RpcClient(local) as client:
            await client.wait_ready(10)
            with pytest.raises(rpc.InvalidParams):
                await client.call("watch.subscribe", {"sinceRowid": 1})
            ok = await client.call("watch.subscribe", {"since_rowid": 1})
            assert ok == {"subscription": 1, "buffer_limit": 256}

    run(scenario())


def test_fake_path_is_executable() -> None:
    assert os.access(FAKE_IMSG, os.X_OK)
