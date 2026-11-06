"""send.py against the fake imsg: GUID vs handle, typed failures, the rate limit."""

from __future__ import annotations

import asyncio
import time

from conftest import run
from fake_imsg.store import FakeDB

from hermes_imsg.rpc import RpcClient
from hermes_imsg.send import Outcome, RateLimiter, send, target
from hermes_imsg.transport import LocalTransport


def test_target_guid_vs_handle() -> None:
    assert target("any;-;+15551230001") == {"chat_guid": "any;-;+15551230001"}
    assert target("any;+;chat123") == {"chat_guid": "any;+;chat123"}
    assert target("+15551230001") == {"to": "+15551230001"}
    assert target("someone@example.com") == {"to": "someone@example.com"}


def _send_all(local: LocalTransport, *calls: dict[str, str]) -> list[Outcome]:
    async def scenario() -> list[Outcome]:
        async with RpcClient(local, backoff_initial=0.05) as client:
            await client.wait_ready(10)
            return [await send(client, c["chat"], text=c.get("text")) for c in calls]

    return run(scenario())


def test_send_by_guid_and_by_handle(local: LocalTransport, db: FakeDB) -> None:
    chat = db.add_chat("+15551230001")
    by_guid, by_handle = _send_all(
        local, {"chat": chat["guid"], "text": "one"}, {"chat": "+15551230002", "text": "two"}
    )
    assert by_guid.ok and by_guid.message_id and by_handle.ok
    sent = [c["params"] for c in db.calls("send")]
    assert sent == [
        {"chat_guid": chat["guid"], "text": "one"},
        {"to": "+15551230002", "text": "two"},
    ]


def test_unknown_chat_fails_typed_and_is_not_retried(local: LocalTransport, db: FakeDB) -> None:
    (out,) = _send_all(local, {"chat": "any;-;+15559999999", "text": "x"})
    assert not out.ok and out.retryable is False
    assert out.error is not None and "Chat not found" in out.error


def test_delivery_unknown_is_never_retried(local: LocalTransport, db: FakeDB) -> None:
    chat = db.add_chat("+15551230001")
    data = {"retry_safe": False, "disposition": "may_have_completed", "transport": "applescript",
            "operation": "send", "detail": "no receipt"}  # fmt: skip
    db.add_fault("send", "error", code=-32001, message="Delivery outcome unknown", data=data)
    (out,) = _send_all(local, {"chat": chat["guid"], "text": "x"})
    assert not out.ok and out.retryable is False and "outcome unknown" in (out.error or "")


def test_busy_is_retryable(local: LocalTransport, db: FakeDB) -> None:
    chat = db.add_chat("+15551230001")
    db.add_fault("send", "error", code=-32000, message="Server busy")
    first, second = _send_all(
        local, {"chat": chat["guid"], "text": "x"}, {"chat": chat["guid"], "text": "x"}
    )
    assert not first.ok and first.retryable is True
    assert second.ok  # control: the next send goes through


def test_not_connected_is_retryable(local: LocalTransport, db: FakeDB) -> None:
    async def scenario() -> Outcome:
        client = RpcClient(local)
        client.start()
        await client.wait_ready(10)
        await client.close()
        return await send(client, "any;-;+15551230001", text="x", timeout=0.2)

    out = run(scenario())
    assert not out.ok and out.retryable is True


def test_rate_limiter_allows_a_burst_then_spaces_sends() -> None:
    async def timed(n: int) -> float:
        limiter = RateLimiter(per_minute=600, burst=2)  # one every 0.1 s after two at once
        start = time.monotonic()
        for _ in range(n):
            await limiter.wait()
        return time.monotonic() - start

    assert asyncio.run(timed(2)) < 0.05  # the burst
    assert asyncio.run(timed(5)) >= 0.29  # three more at 0.1 s each
