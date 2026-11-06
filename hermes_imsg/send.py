"""Outbound: one imsg `send`, a send rate limit, and what a failure means.

`retryable` is true only when imsg certainly did not send: the child was not
connected, or imsg refused the request as busy. Every other failure, including a
timeout or "delivery outcome unknown", may already have reached the recipient,
so it is never retried (imsg `docs/rpc.md`, delivery dispositions).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from .rpc import NotConnected, RpcClient, RpcError, ServerBusy
from .transport import TransportError


def target(chat_id: str) -> dict[str, str]:
    """A chat GUID (`any;-;+15551234567`, `any;+;chat123`) or a bare phone/email handle."""
    return {"chat_guid": chat_id} if ";" in chat_id else {"to": chat_id}


@dataclass(frozen=True)
class Outcome:
    ok: bool
    message_id: str | None = None
    error: str | None = None
    retryable: bool = False


async def send(
    client: RpcClient,
    chat_id: str,
    *,
    text: str | None = None,
    file: str | None = None,
    timeout: float = 60.0,
) -> Outcome:
    params: dict[str, Any] = target(chat_id)
    if text:
        params["text"] = text
    if file:
        params["file"] = file  # a path on the Mac
    try:
        result = await client.call("send", params, timeout=timeout)
    except (NotConnected, ServerBusy) as exc:
        return Outcome(False, error=str(exc), retryable=True)
    except (RpcError, TransportError) as exc:
        return Outcome(False, error=str(exc))
    guid = result.get("guid") if isinstance(result, dict) else None
    return Outcome(True, message_id=guid if isinstance(guid, str) else None)


class RateLimiter:
    """Token bucket: up to `burst` sends at once, then one every 60/`per_minute` seconds."""

    def __init__(self, per_minute: float = 30.0, burst: int = 5) -> None:
        self.interval = 60.0 / per_minute
        self.burst = burst
        self.tokens = float(burst)
        self.updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            self.tokens = min(self.burst, self.tokens + (now - self.updated) / self.interval)
            self.updated = now
            if self.tokens < 1:
                await asyncio.sleep((1 - self.tokens) * self.interval)
                self.tokens, self.updated = 1.0, time.monotonic()
            self.tokens -= 1
