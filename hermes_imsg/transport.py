"""How `imsg rpc` is started: on this Mac (local) or on a Mac over SSH.

A transport only knows how to spawn the child and run a one-off health check.
The long-lived JSON-RPC session lives in `hermes_imsg.rpc`.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
from asyncio.subprocess import PIPE, Process
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

    async def spawn(self) -> Process:
        try:
            return await asyncio.create_subprocess_exec(
                *self.argv(), stdin=PIPE, stdout=PIPE, stderr=PIPE, limit=LINE_LIMIT
            )
        except OSError as exc:
            raise TransportError(f"cannot start {self.describe()}: {exc.strerror or exc}") from None

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


class SshTransport(Transport):
    """Hermes runs elsewhere: `ssh -T -o BatchMode=yes <host> imsg rpc`.

    `host` is anything ssh accepts as a destination (an alias from your ssh
    config, `user@host`, `ssh://user@host:port`). On the Mac, pin the key to
    one command (SECURITY.md):

        restrict,command="/opt/homebrew/bin/imsg rpc" ssh-ed25519 AAAA... hermes-imsg

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

    def argv(self) -> list[str]:
        argv = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
        # A dead link is noticed in about 45 s instead of at the next TCP timeout.
        argv += ["-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3"]
        if self.ssh_config:
            argv += ["-F", self.ssh_config]
        if self.identity_file:
            argv += ["-i", self.identity_file, "-o", "IdentitiesOnly=yes"]
        return [*argv, "--", self.host, f"{shlex.quote(self.imsg_path)} rpc"]

    def describe(self) -> str:
        return f"ssh {self.host}"

    def redact(self, text: str) -> str:
        return text.replace(self.identity_file, "<identity_file>") if self.identity_file else text
