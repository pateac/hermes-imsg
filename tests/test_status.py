"""status.py: the health object, and the liveness check that restarts a silent child."""

from __future__ import annotations

import asyncio
import logging

import pytest
from conftest import eventually, run
from fake_imsg.store import FakeDB

from hermes_imsg import status
from hermes_imsg.rpc import RpcClient
from hermes_imsg.transport import LocalTransport


def test_health_reports_imsg_state(local: LocalTransport, db: FakeDB) -> None:
    async def scenario() -> dict[str, object]:
        async with RpcClient(local) as client:
            await client.wait_ready(10)
            return status.health(client)

    h = run(scenario())
    assert h["connected"] is True and h["imsg_version"] == "0.21.0-fake"
    assert h["database_ready"] is True and h["bridge_ready"] is False
    assert "send" in h["methods"]  # type: ignore[operator]


def test_a_child_that_stops_answering_is_restarted(local: LocalTransport, db: FakeDB) -> None:
    db.add_fault("status", "drop", times=1)  # the first periodic check gets no answer

    async def scenario() -> int:
        async with RpcClient(local, backoff_initial=0.05) as client:
            await client.wait_ready(10)
            watcher = asyncio.create_task(status.watch(client, interval=0.05, timeout=0.3))
            await eventually(lambda: client.connects == 2)
            watcher.cancel()
            return client.connects

    assert run(scenario()) == 2
    assert len(db.spawns()) == 2


def test_an_unreadable_database_is_logged(
    local: LocalTransport, db: FakeDB, caplog: pytest.LogCaptureFixture
) -> None:
    async def scenario() -> None:
        async with RpcClient(local) as client:
            await client.wait_ready(10)
            db.set("db_ready", "0")
            watcher = asyncio.create_task(status.watch(client, interval=0.05, timeout=2))
            await eventually(lambda: status.health(client)["database_ready"] is False)
            await asyncio.sleep(0.2)  # several more checks: still one warning
            watcher.cancel()

    with caplog.at_level(logging.WARNING, logger="hermes_imsg"):
        run(scenario())
    assert caplog.text.count("cannot read the Messages database") == 1
