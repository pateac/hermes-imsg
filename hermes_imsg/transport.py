"""How `imsg rpc` is reached: on this Mac (local) or on a Mac over SSH.

A transport spawns the child, runs a one-off health check, and moves attachment
files: `fetch` reads one from the Mac, `put` places one there for `imsg send`.
Over SSH both go through the gate script on the Mac (`scripts/hermes-imsg-gate`).
The long-lived JSON-RPC session lives in `hermes_imsg.rpc`.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shlex
from asyncio.subprocess import PIPE, Process
from pathlib import Path
from typing import Any

# imsg lines can be large (long texts, poll metadata); asyncio's 64 KiB default would raise.
LINE_LIMIT = 16 * 1024 * 1024


class TransportError(Exception):
    """The imsg child could not be started, or did not answer."""


class Transport:
    """Base class. Never log `argv()`: for SSH it carries the key path."""

    def argv(self) -> list[str]:
        raise NotImplementedError

    def describe(self) -> str:
        """A log-safe name for this transport."""
        raise NotImplementedError

    def redact(self, text: str) -> str:
        """Strip anything secret-adjacent from child stderr before it is logged."""
        return text

    async def spawn(self, argv: list[str] | None = None) -> Process:
        try:
            return await asyncio.create_subprocess_exec(
                *(argv or self.argv()), stdin=PIPE, stdout=PIPE, stderr=PIPE, limit=LINE_LIMIT
            )
        except OSError as exc:
            raise TransportError(f"cannot start {self.describe()}: {exc.strerror or exc}") from None

    async def fetch(self, path: str, max_bytes: int) -> bytes:
        """The bytes of an attachment imsg reported at `path` on the Mac."""
        raise NotImplementedError

    async def put(self, local_path: str, name: str) -> str:
        """Place a file where `imsg send` on the Mac can attach it; return that path."""
        raise NotImplementedError

    async def _run(self, argv: list[str], stdin: bytes, what: str, timeout: float) -> bytes:
        """Run a one-shot command to completion; its stdout, or TransportError with its stderr."""
        proc = await self.spawn(argv)
        try:
            out, err = await asyncio.wait_for(proc.communicate(stdin), timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise TransportError(
                f"{self.describe()}: {what} timed out after {timeout:g}s"
            ) from None
        if proc.returncode != 0:
            lines = err.decode(errors="replace").strip().splitlines()
            detail = self.redact(lines[-1]) if lines else f"exit code {proc.returncode}"
            raise TransportError(f"{self.describe()}: {what} failed ({detail})")
        return out

    async def health_check(self, timeout: float = 15.0) -> dict[str, Any]:
        """Start a one-off `imsg rpc`, ask for `status`, close stdin, return the status object."""
        proc = await self.spawn()
        request = json.dumps({"jsonrpc": "2.0", "id": "health", "method": "status"})
        try:
            out, err = await asyncio.wait_for(proc.communicate(request.encode() + b"\n"), timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise TransportError(f"{self.describe()}: no status reply in {timeout:g}s") from None
        for line in out.splitlines():
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if isinstance(msg, dict) and msg.get("id") == "health":
                result = msg.get("result")
                if isinstance(result, dict):
                    return result
                detail = self.redact(json.dumps(msg.get("error")))
                raise TransportError(f"{self.describe()}: status failed: {detail}")
        lines = err.decode(errors="replace").strip().splitlines()
        detail = self.redact(lines[-1]) if lines else f"exit code {proc.returncode}"
        raise TransportError(f"{self.describe()}: no status reply ({detail})")


class LocalTransport(Transport):
    """Hermes runs on the Mac itself: start `imsg rpc` directly."""

    def __init__(self, imsg_path: str = "imsg") -> None:
        self.imsg_path = imsg_path

    def argv(self) -> list[str]:
        return [self.imsg_path, "rpc"]

    def describe(self) -> str:
        return f"local {self.imsg_path}"

    async def fetch(self, path: str, max_bytes: int) -> bytes:
        return _capped(await asyncio.to_thread(Path(path).expanduser().read_bytes), max_bytes)

    async def put(self, local_path: str, name: str) -> str:
        return local_path  # already on the Mac


class SshTransport(Transport):
    """Hermes runs elsewhere: `ssh -T -o BatchMode=yes <host> imsg rpc`.

    `host` is anything ssh accepts as a destination (an alias from your ssh
    config, `user@host`, `ssh://user@host:port`). On the Mac, pin the key to
    this host and one command (SECURITY.md):

        from="192.0.2.10",restrict,command="/Users/me/.local/bin/hermes-imsg-gate" ssh-ed25519 ...

    Without that, note that a non-interactive SSH shell on macOS often lacks
    /opt/homebrew/bin on PATH; pass the full `imsg_path`.
    """

    def __init__(
        self,
        host: str,
        *,
        identity_file: str | None = None,
        ssh_config: str | None = None,
        imsg_path: str = "imsg",
    ) -> None:
        if not host or host.startswith("-"):
            raise ValueError("host must be an ssh destination, not an option")
        self.host = host
        self.identity_file = os.path.expanduser(identity_file) if identity_file else None
        self.ssh_config = os.path.expanduser(ssh_config) if ssh_config else None
        self.imsg_path = imsg_path

    def argv(self, remote: str | None = None) -> list[str]:
        argv = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
        # A dead link is noticed in about 45 s instead of at the next TCP timeout.
        argv += ["-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3"]
        if self.ssh_config:
            argv += ["-F", self.ssh_config]
        if self.identity_file:
            argv += ["-i", self.identity_file, "-o", "IdentitiesOnly=yes"]
        return [*argv, "--", self.host, remote or f"{shlex.quote(self.imsg_path)} rpc"]

    def describe(self) -> str:
        return f"ssh {self.host}"

    def redact(self, text: str) -> str:
        return text.replace(self.identity_file, "<identity_file>") if self.identity_file else text

    async def fetch(self, path: str, max_bytes: int, timeout: float = 120.0) -> bytes:
        out = await self._run(self.argv(f"fetch {_b64(path)}"), b"", "fetch", timeout)
        if not out:  # a key forced to plain `imsg rpc` answers a fetch with nothing at all
            raise TransportError(
                f"{self.describe()}: fetch returned nothing (is the gate installed?)"
            )
        return _capped(out, max_bytes)

    async def put(self, local_path: str, name: str, timeout: float = 300.0) -> str:
        data = await asyncio.to_thread(Path(local_path).read_bytes)
        out = await self._run(self.argv(f"put {_b64(name)}"), data, "put", timeout)
        lines = out.decode(errors="replace").strip().splitlines()
        if not lines or not lines[-1].startswith("/"):
            raise TransportError(
                f"{self.describe()}: put returned no path (is the gate installed?)"
            )
        return lines[-1]


def _b64(text: str) -> str:
    """The gate's argument encoding: no path or name is ever parsed by a shell."""
    return base64.b64encode(text.encode()).decode()


def _capped(data: bytes, max_bytes: int) -> bytes:
    if len(data) > max_bytes:
        raise TransportError(f"attachment of {len(data)} bytes is over the {max_bytes}-byte cap")
    return data
