from __future__ import annotations

import asyncio
import getpass
import os
import shutil
import socket
import subprocess
import time
from collections.abc import Awaitable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TypeVar

import pytest
from fake_imsg.store import FakeDB

from hermes_imsg.transport import LocalTransport

FAKE_IMSG = str(Path(__file__).parent / "fake_imsg" / "imsg")
T = TypeVar("T")


def run(coro: Awaitable[T], timeout: float = 30.0) -> T:
    async def bounded() -> T:
        return await asyncio.wait_for(coro, timeout)

    return asyncio.run(bounded())


async def eventually(check: Any, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not check():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeDB]:
    monkeypatch.setenv("FAKE_IMSG_DB", str(tmp_path / "chat.db"))
    fake = FakeDB(tmp_path / "chat.db")
    yield fake
    fake.close()


@pytest.fixture
def local(db: FakeDB) -> LocalTransport:
    return LocalTransport(FAKE_IMSG)


def _keygen(path: Path) -> str:
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(path)], check=True)
    return path.with_suffix(".pub").read_text().strip()


@pytest.fixture(scope="session")
def sshd(tmp_path_factory: pytest.TempPathFactory) -> Iterator[SimpleNamespace]:
    """A throwaway sshd on 127.0.0.1 whose only key is forced to run the fake imsg."""
    sshd_bin = shutil.which("sshd") or "/usr/sbin/sshd"
    if not (os.path.exists(sshd_bin) and shutil.which("ssh") and shutil.which("ssh-keygen")):
        if os.environ.get("HERMES_IMSG_REQUIRE_SSH"):
            pytest.fail("HERMES_IMSG_REQUIRE_SSH is set but ssh/sshd/ssh-keygen are missing")
        pytest.skip("needs ssh, sshd and ssh-keygen")
    d = tmp_path_factory.mktemp("sshd")
    host_pub = _keygen(d / "host_key")
    client_pub = _keygen(d / "client_key")
    db = FakeDB(d / "chat.db")
    # The line SECURITY.md recommends, pointed at the fake instead of /opt/homebrew/bin/imsg.
    forced = f'restrict,command="{FAKE_IMSG} rpc --db {db.path}" {client_pub}\n'
    (d / "authorized_keys").write_text(forced)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    (d / "sshd_config").write_text(
        f"Port {port}\nListenAddress 127.0.0.1\nHostKey {d / 'host_key'}\n"
        f"AuthorizedKeysFile {d / 'authorized_keys'}\nPidFile {d / 'sshd.pid'}\n"
        "StrictModes no\nUsePAM no\nPasswordAuthentication no\n"
        "KbdInteractiveAuthentication no\nPubkeyAuthentication yes\nLogLevel ERROR\n"
    )
    (d / "known_hosts").write_text(f"[127.0.0.1]:{port} {host_pub}\n")
    (d / "ssh_config").write_text(
        f"Host fakemac\n  HostName 127.0.0.1\n  Port {port}\n  User {getpass.getuser()}\n"
        f"  UserKnownHostsFile {d / 'known_hosts'}\n"
    )
    if os.geteuid() == 0:
        os.makedirs("/run/sshd", exist_ok=True)  # sshd's privilege-separation dir
    proc = subprocess.Popen([sshd_bin, "-D", "-e", "-f", str(d / "sshd_config")])
    deadline = time.monotonic() + 10
    while True:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            break
        except OSError:
            if proc.poll() is not None or time.monotonic() > deadline:
                proc.kill()
                pytest.fail(f"sshd did not start (exit {proc.poll()})")
            time.sleep(0.05)
    yield SimpleNamespace(dir=d, db=db, key=str(d / "client_key"), config=str(d / "ssh_config"))
    proc.terminate()
    proc.wait(5)
    db.close()


@pytest.fixture
def ssh(sshd: SimpleNamespace) -> SimpleNamespace:
    sshd.db.reset()
    return sshd
