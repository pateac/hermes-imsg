"""imsg's own health report, surfaced to Hermes, and a liveness check on the child."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .rpc import RpcClient, RpcError, RpcTimeout
from .transport import TransportError

log = logging.getLogger(__name__)


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def health(client: RpcClient) -> dict[str, Any]:
    """The last `status` imsg gave, flattened. No message content is in it."""
    status = client.status or {}
    database, bridge = _dict(status.get("database")), _dict(status.get("bridge"))
    return {
        "transport": client.transport.describe(),
        "connected": client.ready,
        "imsg_version": status.get("version"),
        "database_ready": database.get("ready") is True,
        "database_error": database.get("error"),
        "bridge_ready": bridge.get("ready") is True,  # the helper; needs SIP off (phase 5)
        "methods": list(status.get("methods") or []),
    }


async def watch(client: RpcClient, interval: float = 60.0, timeout: float = 15.0) -> None:
    """Refresh `status` every `interval` s. A child that stops answering is restarted (the
    supervisor reconnects and resubscribes); a database that turns unreadable is logged."""
    database_ready: bool | None = None
    while True:
        await asyncio.sleep(interval)
        try:
            client.status = await client.call("status", timeout=timeout)
        except RpcTimeout:
            log.warning("imsg did not answer status in %gs; restarting it", timeout)
            client.restart()
            continue
        except (TransportError, RpcError) as exc:  # not connected, or imsg refused: keep watching
            log.debug("status check skipped: %s", exc)
            continue
        now = health(client)["database_ready"]
        if now != database_ready:
            if now:
                log.info("imsg can read the Messages database")
            else:
                log.warning("imsg cannot read the Messages database: check Full Disk Access")
            database_ready = now
