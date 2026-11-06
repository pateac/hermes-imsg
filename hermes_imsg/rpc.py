"""JSON-RPC 2.0 client for one long-lived `imsg rpc` child.

Framing is one JSON object per line on stdin/stdout (imsg `docs/rpc.md`).
Calls multiplex over the child by id; notifications go to `notifications`.
If the child dies, pending calls fail with `ConnectionLost` and the client
respawns it with exponential backoff, then runs `on_connect` again (that is
where a caller re-subscribes and back-fills from its cursor).

Every error says whether re-sending is safe (`retry_safe`). Nothing here
retries a call by itself: a send that may have run must not be sent twice.
Message bodies are never logged, at any level.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
from asyncio.subprocess import Process
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from .transport import LINE_LIMIT, Transport, TransportError

log = logging.getLogger(__name__)

PROTOCOL_VERSION = 1


class NotConnected(TransportError):
    """No live child within the call's timeout. The request was never sent."""

    retry_safe = True


class ConnectionLost(TransportError):
    """The child went away after the request was written. It may have run."""

    retry_safe = False


class RpcTimeout(TransportError):
    """The request was written but no reply came in time. It may have run."""

    retry_safe = False


class RpcError(Exception):
    """imsg answered with a JSON-RPC error object."""

    def __init__(self, method: str, code: int, message: str, data: Any = None) -> None:
        detail = data.get("detail") if isinstance(data, dict) else data
        super().__init__(f"{method}: {message}" + (f": {detail}" if detail else ""))
        self.method, self.code, self.message, self.data = method, code, message, data

    @property
    def retry_safe(self) -> bool:
        """True only when imsg says, or the code implies, that nothing ran."""
        if isinstance(self.data, dict):
            return bool(self.data.get("retry_safe", self.data.get("retryable", False)))
        return self.code in _REJECTED_BEFORE_RUNNING


class ServerBusy(RpcError):  # -32000: request limit or shutting down
    pass


class DeliveryUnknown(RpcError):  # -32001: may have completed; never retry automatically
    pass


class DatabaseUnavailable(RpcError):  # -32002: chat.db unreadable (path or Full Disk Access)
    pass


class BridgeUnavailable(RpcError):  # -32003: helper not loaded (`imsg launch` not run)
    pass


class MutationLaneBlocked(RpcError):  # -32004: an earlier send is still in flight
    pass


class MethodNotFound(RpcError):  # -32601
    pass


class InvalidParams(RpcError):  # -32602: includes "Chat not found: <id>"
    pass


_ERRORS: dict[int, type[RpcError]] = {
    -32000: ServerBusy,
    -32001: DeliveryUnknown,
    -32002: DatabaseUnavailable,
    -32003: BridgeUnavailable,
    -32004: MutationLaneBlocked,
    -32601: MethodNotFound,
    -32602: InvalidParams,
}
_REJECTED_BEFORE_RUNNING = {-32700, -32600, -32601, -32602, -32000}


def error_from(method: str, error: Any) -> RpcError:
    if not isinstance(error, dict) or type(error.get("code")) is not int:
        return RpcError(method, -32603, "malformed error object")
    cls = _ERRORS.get(error["code"], RpcError)
    return cls(method, error["code"], str(error.get("message", "")), error.get("data"))


@dataclass(frozen=True)
class Notification:
    method: str  # "message", "watch.overflow", "bridge.event", ...
    params: dict[str, Any]


OnConnect = Callable[["RpcClient"], Awaitable[None]]


class RpcClient:
    """One supervised `imsg rpc` child. Use `async with RpcClient(t) as c:`."""

    def __init__(
        self,
        transport: Transport,
        *,
        on_connect: OnConnect | None = None,
        call_timeout: float = 30.0,
        backoff_initial: float = 0.5,
        backoff_max: float = 30.0,
        stable_after: float = 30.0,
    ) -> None:
        self.transport = transport
        self.on_connect = on_connect
        self.call_timeout = call_timeout
        self.backoff_initial, self.backoff_max = backoff_initial, backoff_max
        self.stable_after = stable_after  # a connection must last this long to reset the backoff
        # shortcut: unbounded; imsg's per-subscription buffer_limit is the backpressure upstream.
        self.notifications: asyncio.Queue[Notification] = asyncio.Queue()
        self.status: dict[str, Any] | None = None  # the last `initialize` snapshot
        self.connects = 0
        self._ids = itertools.count(1)
        self._pending: dict[int, tuple[str, asyncio.Future[Any]]] = {}
        self._proc: Process | None = None
        self._live = asyncio.Event()  # child up: calls may be written
        self._ready = asyncio.Event()  # initialize and on_connect done
        self._stop = asyncio.Event()
        self._supervisor: asyncio.Task[None] | None = None

    async def __aenter__(self) -> RpcClient:
        self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    def start(self) -> None:
        if self._supervisor is None:
            self._supervisor = asyncio.create_task(self._supervise())

    @property
    def ready(self) -> bool:
        """Connected, initialized, and `on_connect` done."""
        return self._ready.is_set()

    def restart(self) -> None:
        """Kill the current child; the supervisor reconnects and runs `on_connect` again."""
        proc = self._proc
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()

    async def wait_ready(self, timeout: float) -> None:
        try:
            await asyncio.wait_for(self._ready.wait(), timeout)
        except TimeoutError:
            raise NotConnected(f"{self.transport.describe()}: not ready in {timeout:g}s") from None

    async def call(
        self, method: str, params: dict[str, Any] | None = None, *, timeout: float | None = None
    ) -> Any:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + (self.call_timeout if timeout is None else timeout)
        while not self._live.is_set():
            remaining = deadline - loop.time()
            if self._stop.is_set() or remaining <= 0:
                raise NotConnected(f"{method}: not connected to {self.transport.describe()}")
            try:
                await asyncio.wait_for(self._live.wait(), remaining)
            except TimeoutError:
                pass
        proc = self._proc
        assert proc is not None and proc.stdin is not None
        rid = next(self._ids)
        fut: asyncio.Future[Any] = loop.create_future()
        self._pending[rid] = (method, fut)
        request: dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            request["params"] = params
        try:
            proc.stdin.write(json.dumps(request, ensure_ascii=False).encode() + b"\n")
            log.debug("-> %s #%d", method, rid)
            await proc.stdin.drain()
            return await asyncio.wait_for(fut, max(0.0, deadline - loop.time()))
        except TimeoutError:
            raise RpcTimeout(f"{method}: no reply in time; it may have run") from None
        except (BrokenPipeError, ConnectionResetError):
            raise ConnectionLost(f"{method}: {self.transport.describe()} went away") from None
        finally:
            self._pending.pop(rid, None)

    async def close(self, timeout: float = 5.0) -> None:
        """Close stdin (imsg drains accepted requests and exits), then stop supervising."""
        self._stop.set()
        self._live.clear()
        self._ready.clear()
        proc = self._proc
        if proc is not None and proc.stdin is not None and proc.returncode is None:
            proc.stdin.close()
        if self._supervisor is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._supervisor), timeout)
            except TimeoutError:
                current = self._proc  # may be newer than `proc` if a respawn raced close()
                if current is not None and current.returncode is None:
                    current.kill()
                await self._supervisor

    async def _supervise(self) -> None:
        loop = asyncio.get_running_loop()
        delay = self.backoff_initial
        while not self._stop.is_set():
            up_since = None
            try:
                up_since = await self._run_once()
            except Exception:
                log.exception("unexpected error supervising %s", self.transport.describe())
            if self._stop.is_set():
                break
            if up_since is not None and loop.time() - up_since >= self.stable_after:
                delay = self.backoff_initial
            log.info("reconnecting to %s in %.1fs", self.transport.describe(), delay)
            try:
                await asyncio.wait_for(self._stop.wait(), delay)
            except TimeoutError:
                pass
            delay = min(delay * 2, self.backoff_max)

    async def _run_once(self) -> float | None:
        """Run one child to its end. Returns when it became ready, or None if it never did."""
        name = self.transport.describe()
        try:
            proc = await self.transport.spawn()
        except TransportError as exc:
            log.error("%s", exc)
            return None
        assert proc.stdin is not None
        self._proc = proc
        reader = asyncio.create_task(self._read(proc))
        stderr = asyncio.create_task(self._drain_stderr(proc))
        up_since = None
        try:
            if self._stop.is_set():  # close() raced the spawn
                proc.stdin.close()
            else:
                self._live.set()
                try:
                    params = {"protocol_version": PROTOCOL_VERSION}
                    self.status = await self.call("initialize", params)
                    if self.on_connect is not None:
                        await self.on_connect(self)
                except Exception as exc:  # any failure here leaves the child useless
                    log.warning("%s: setup failed: %s", name, exc)
                    if proc.returncode is None:
                        with contextlib.suppress(ProcessLookupError):
                            proc.kill()
                else:
                    self.connects += 1
                    up_since = asyncio.get_running_loop().time()
                    self._ready.set()
                    log.info("connected to %s", name)
            await reader
        finally:
            self._drop_link()
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):  # it may exit as we kill it
                    proc.kill()
            code = await proc.wait()
            reader.cancel()
            await asyncio.gather(reader, stderr, return_exceptions=True)
        if not self._stop.is_set():
            log.warning("lost %s (exit code %s)", name, code)
        return up_since

    def _drop_link(self) -> None:
        """Stop new writes and fail every pending call: the child can no longer answer."""
        self._live.clear()
        self._ready.clear()
        for method, fut in self._pending.values():
            if not fut.done():
                fut.set_exception(
                    ConnectionLost(
                        f"{method}: {self.transport.describe()} went away; it may have run"
                    )
                )

    async def _read(self, proc: Process) -> None:
        assert proc.stdout is not None
        try:
            while True:
                try:
                    line = await proc.stdout.readline()
                except ValueError:  # over LINE_LIMIT: restart rather than drop a row silently
                    log.error("imsg sent a line over %d bytes; restarting the child", LINE_LIMIT)
                    return
                if not line:
                    return
                self._dispatch(line)
        finally:  # here, not after setup: a call in initialize/on_connect must fail now
            self._drop_link()

    def _dispatch(self, line: bytes) -> None:
        try:
            msg = json.loads(line)
        except ValueError:
            log.warning("ignored a non-JSON line from imsg (%d bytes)", len(line))
            return
        if not isinstance(msg, dict):
            log.warning("ignored a non-object JSON line from imsg")
            return
        if "id" not in msg and isinstance(msg.get("method"), str):
            params = msg.get("params")
            self.notifications.put_nowait(
                Notification(msg["method"], params if isinstance(params, dict) else {})
            )
            return
        rid = msg.get("id")
        entry = self._pending.get(rid) if type(rid) is int else None
        if entry is None or entry[1].done():
            err = msg.get("error")
            if isinstance(err, dict):  # e.g. a parse error with id null
                log.warning(
                    "imsg error for no pending call: %s %s", err.get("code"), err.get("message")
                )
            else:
                log.debug("reply for id %r with no pending call (timed out?)", rid)
            return
        method, fut = entry
        log.debug("<- %s #%d", method, rid)
        if "error" in msg:
            fut.set_exception(error_from(method, msg["error"]))
        else:
            fut.set_result(msg.get("result"))

    async def _drain_stderr(self, proc: Process) -> None:
        assert proc.stderr is not None
        name = self.transport.describe()
        while True:
            try:
                line = await proc.stderr.readline()
            except ValueError:
                continue
            if not line:
                return
            text = self.transport.redact(line.decode(errors="replace").rstrip())
            log.warning("%s stderr: %s", name, text)
