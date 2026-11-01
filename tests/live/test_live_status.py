"""Opt-in, needs a real Mac: read-only `status` through the transport you will use.

    HERMES_IMSG_LIVE=1 HERMES_IMSG_LIVE_HOST=mac.local \
    HERMES_IMSG_LIVE_KEY=~/.ssh/hermes_imsg HERMES_IMSG_LIVE_IMSG=/opt/homebrew/bin/imsg \
    pytest tests/live -v -s

Leave HERMES_IMSG_LIVE_HOST unset to run `imsg rpc` locally (Hermes on the Mac).
Sends nothing and changes nothing on the Mac.
"""

from __future__ import annotations

import os

import pytest
from conftest import run

from hermes_imsg.rpc import RpcClient
from hermes_imsg.transport import LocalTransport, SshTransport, Transport

pytestmark = pytest.mark.skipif(
    os.environ.get("HERMES_IMSG_LIVE") != "1", reason="live: set HERMES_IMSG_LIVE=1 on a Mac"
)


def transport() -> Transport:
    imsg = os.environ.get("HERMES_IMSG_LIVE_IMSG", "imsg")
    host = os.environ.get("HERMES_IMSG_LIVE_HOST")
    if not host:
        return LocalTransport(imsg)
    return SshTransport(host, identity_file=os.environ.get("HERMES_IMSG_LIVE_KEY"), imsg_path=imsg)


def test_health_check_reads_the_database() -> None:
    status = run(transport().health_check())
    print(status)  # the version, methods and bridge state, for the record
    assert status["protocol_version"] == 1
    assert status["database"]["ready"] is True, status["database"].get("error")


def test_rpc_session_lists_chats() -> None:
    async def scenario() -> list[object]:
        async with RpcClient(transport()) as client:
            await client.wait_ready(20)
            result = await client.call("chats.list", {"limit": 1})
            return list(result["chats"])

    assert isinstance(run(scenario()), list)
