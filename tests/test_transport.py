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
from conftest import FAKE_IMSG, GATE, eventually, run
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


# --- the gate: the forced command that also moves attachment files ------------------------


def _gated(ssh: SimpleNamespace) -> SshTransport:
    return SshTransport("fakemac", ssh_config=ssh.config, identity_file=ssh.gated_key)


def test_gate_runs_imsg_rpc_and_nothing_else(ssh: SimpleNamespace) -> None:
    assert run(_gated(ssh).health_check())["protocol_version"] == 1
    secret = ssh.dir / "not-for-you.txt"
    secret.write_text("CONTENTS-OF-ANOTHER-FILE")
    proc = subprocess.run(
        ["ssh", "-T", "-o", "BatchMode=yes", "-F", ssh.config, "-i", ssh.gated_key, "fakemac",
         f"cat {secret}"],
        input=b"", capture_output=True, timeout=20,
    )  # fmt: skip
    assert b"CONTENTS-OF-ANOTHER-FILE" not in proc.stdout + proc.stderr
    assert ssh.db.spawns()[-1]["ssh_command"] == f"cat {secret}"  # imsg rpc ran instead
    assert os.access(GATE, os.X_OK)


def test_gate_fetch_serves_only_messages_attachments(ssh: SimpleNamespace) -> None:
    t = _gated(ssh)
    folder = ssh.home / "Library" / "Messages" / "Attachments" / "ab" / "12"
    folder.mkdir(parents=True, exist_ok=True)
    picture = folder / "IMG 0001; $(touch x).png"  # a space and shell syntax: never parsed
    picture.write_bytes(b"\x89PNG fake bytes")
    assert run(t.fetch(str(picture), 1024)) == b"\x89PNG fake bytes"

    outside = ssh.dir / "not-for-you.txt"
    outside.write_text("secret")
    link = folder / "escape.txt"
    if not link.exists():
        link.symlink_to(outside)
    traversal = f"{folder}/../../../../../not-for-you.txt"
    for path in (str(outside), str(link), traversal, "/etc/hostname"):
        with pytest.raises(TransportError, match="refused"):
            run(t.fetch(path, 1024))
    # macOS realpath fails on a missing file; GNU realpath accepts it and the -f check refuses
    with pytest.raises(TransportError, match=r"no such file|not a file"):
        run(t.fetch(str(folder / "missing.png"), 1024))
    with pytest.raises(TransportError, match="over the 4-byte cap"):
        run(t.fetch(str(picture), 4))
    assert not (ssh.home / "x").exists()


def test_gate_put_lands_only_in_the_outbox(ssh: SimpleNamespace, tmp_path: Path) -> None:
    t = _gated(ssh)
    local = tmp_path / "photo.jpg"
    local.write_bytes(b"jpeg bytes")
    remote = run(t.put(str(local), "../../evil; rm -rf ~ $(id).jpg"))
    outbox = ssh.home / "Library" / "Caches" / "hermes-imsg" / "outbox"
    assert Path(remote).resolve().parent.parent == outbox.resolve()
    assert Path(remote).read_bytes() == b"jpeg bytes"
    assert "/" not in Path(remote).name and ";" not in remote and "$" not in remote
    before = set(outbox.iterdir())
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * 5000)  # the test key's cap is 4096 bytes
    with pytest.raises(TransportError, match="larger than 4096"):
        run(t.put(str(big), "big.bin"))
    assert set(outbox.iterdir()) == before  # the refused file left nothing behind


def test_without_the_gate_files_fail_loudly(ssh: SimpleNamespace, tmp_path: Path) -> None:
    plain = _ssh(ssh)  # the key forced to plain `imsg rpc`
    with pytest.raises(TransportError, match="is the gate installed"):
        run(plain.fetch("/Users/x/Library/Messages/Attachments/a.png", 1024))
    local = tmp_path / "a.txt"
    local.write_text("x")
    with pytest.raises(TransportError, match="is the gate installed"):
        run(plain.put(str(local), "a.txt"))
