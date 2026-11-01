"""transport.py: argv shape, health check, and SSH end to end through a forced-command key."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import FAKE_IMSG, eventually, run
from fake_imsg.store import FakeDB

from hermes_imsg.rpc import RpcClient
from hermes_imsg.transport import LocalTransport, SshTransport, TransportError


def test_local_argv() -> None:
    assert LocalTransport("/opt/homebrew/bin/imsg").argv() == ["/opt/homebrew/bin/imsg", "rpc"]


def test_ssh_argv_is_batch_no_tty_and_quotes_the_remote_path() -> None:
    t = SshTransport("mac.local", identity_file="/keys/hermes", imsg_path="/opt/my imsg")
    argv = t.argv()
    assert argv[:2] == ["ssh", "-T"] and "BatchMode=yes" in argv
    assert argv[argv.index("-i") + 1] == "/keys/hermes" and "IdentitiesOnly=yes" in argv
    assert argv[-3:] == ["--", "mac.local", "'/opt/my imsg' rpc"]
    assert "/keys/hermes" not in t.describe()


def test_ssh_refuses_a_host_that_is_an_option() -> None:
    with pytest.raises(ValueError):
        SshTransport("-oProxyCommand=touch /tmp/pwned")


def test_ssh_redacts_the_key_path() -> None:
    t = SshTransport("mac", identity_file="~/.ssh/hermes_imsg")
    expanded = os.path.expanduser("~/.ssh/hermes_imsg")
    assert t.redact(f"Warning: Identity file {expanded} not accessible") == (
        "Warning: Identity file <identity_file> not accessible"
    )


def test_local_health_check(local: LocalTransport, db: FakeDB) -> None:
    status = run(local.health_check())
    assert status["protocol_version"] == 1 and status["database"]["ready"] is True
    assert status["bridge"]["ready"] is False


def test_health_check_reports_a_missing_binary(tmp_path: Path) -> None:
    with pytest.raises(TransportError, match="No such file"):
        run(LocalTransport(str(tmp_path / "nope")).health_check())


def test_health_check_reports_the_childs_last_stderr_line(
    local: LocalTransport, db: FakeDB
) -> None:
    db.add_fault("$startup", "stderr", text="imsg: Full Disk Access is missing")
    db.add_fault("status", "exit", code=1)
    with pytest.raises(TransportError, match=r"no status reply.*Full Disk Access is missing"):
        run(local.health_check())


def test_health_check_times_out(local: LocalTransport, db: FakeDB) -> None:
    db.add_fault("status", "delay", ms=3000)  # imsg drains on EOF, so a late reply, not none
    with pytest.raises(TransportError, match=r"no status reply in 0\.5s"):
        run(local.health_check(timeout=0.5))


# --- SSH end to end, against a real sshd on 127.0.0.1 ------------------------------------


def _ssh(ssh: SimpleNamespace, **kw: str) -> SshTransport:
    return SshTransport(
        "fakemac", ssh_config=ssh.config, imsg_path=FAKE_IMSG, **{"identity_file": ssh.key, **kw}
    )


def test_ssh_health_check(ssh: SimpleNamespace) -> None:
    status = run(_ssh(ssh).health_check())
    assert status["protocol_version"] == 1
    assert ssh.db.spawns()[0]["ssh_command"] == f"{FAKE_IMSG} rpc"  # what the client asked for


def test_ssh_rpc_session_survives_a_killed_child(ssh: SimpleNamespace) -> None:
    chat = ssh.db.add_chat("+15551230001")

    async def on_connect(client: RpcClient) -> None:
        await client.call("watch.subscribe", {})

    async def scenario() -> list[str]:
        async with RpcClient(_ssh(ssh), on_connect=on_connect, backoff_initial=0.1) as client:
            await client.wait_ready(15)
            sent = await client.call("send", {"chat_id": chat["id"], "text": "over ssh"})
            assert sent["ok"] is True
            first = await asyncio.wait_for(client.notifications.get(), 5)
            os.kill(ssh.db.spawns()[-1]["pid"], signal.SIGKILL)  # the remote imsg dies
            await eventually(lambda: client.connects == 2, timeout=15)
            ssh.db.add_message(chat["id"], "after reconnect")
            second = await asyncio.wait_for(client.notifications.get(), 5)
        return [first.params["message"]["text"], second.params["message"]["text"]]

    assert run(scenario()) == ["over ssh", "after reconnect"]


def test_forced_command_key_cannot_run_anything_else(ssh: SimpleNamespace) -> None:
    secret = ssh.dir / "not-for-you.txt"
    secret.write_text("CONTENTS-OF-ANOTHER-FILE")
    proc = subprocess.run(
        [
            "ssh",
            "-T",
            "-o",
            "BatchMode=yes",
            "-F",
            ssh.config,
            "-i",
            ssh.key,
            "fakemac",
            f"cat {secret}",
        ],
        input=b"",
        capture_output=True,
        timeout=20,
    )
    assert b"CONTENTS-OF-ANOTHER-FILE" not in proc.stdout + proc.stderr
    spawn = ssh.db.spawns()[-1]
    assert spawn["ssh_command"] == f"cat {secret}"  # control: the request reached sshd ...
    assert spawn["argv"][0] == "rpc"  # ... and the fake imsg ran instead


def test_ssh_unknown_host_key_fails_closed(ssh: SimpleNamespace, tmp_path: Path) -> None:
    config = (
        Path(ssh.config)
        .read_text()
        .replace(str(ssh.dir / "known_hosts"), str(tmp_path / "empty_known_hosts"))
    )
    (tmp_path / "ssh_config").write_text(config)
    t = SshTransport(
        "fakemac",
        ssh_config=str(tmp_path / "ssh_config"),
        identity_file=ssh.key,
        imsg_path=FAKE_IMSG,
    )
    with pytest.raises(TransportError, match="Host key verification failed"):
        run(t.health_check())
    assert ssh.db.spawns() == []


def test_ssh_key_path_never_reaches_errors_or_logs(
    ssh: SimpleNamespace, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    missing_key = str(tmp_path / "hermes-imsg-key-that-is-not-there")
    t = _ssh(ssh, identity_file=missing_key)
    with pytest.raises(TransportError) as exc:
        run(t.health_check())
    assert "<identity_file>" in str(exc.value) or "Permission denied" in str(exc.value)
    assert missing_key not in str(exc.value)

    async def scenario() -> None:
        async with RpcClient(t, backoff_initial=0.05) as client:
            with pytest.raises(TransportError):
                await client.wait_ready(3)

    with caplog.at_level(logging.DEBUG, logger="hermes_imsg"):
        run(scenario())
    assert "stderr" in caplog.text  # control: ssh's complaints were logged
    assert missing_key not in caplog.text
