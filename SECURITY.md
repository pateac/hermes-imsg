# Security

## Reporting a vulnerability

Use GitHub's private vulnerability reporting: **Security → Report a vulnerability** on this
repository. Please do not open a public issue for a security problem. You will get a reply within
a week. The disclosure window is 90 days from the report, sooner if a fix ships.

Supported versions: the latest release only (none yet; phase 1 is pre-release).

## Threat model

**The trust boundary is the SSH key.** Whoever holds the private key can run `imsg rpc` on the
Mac, which means reading every message in Messages and sending as that account. Treat the key
like the account's password.

**Pin the key to one command and one host.** In `~/.ssh/authorized_keys` on the Mac, with the
Hermes host's fixed address and the path that `command -v imsg` prints:

```
from="192.0.2.10",restrict,command="/opt/homebrew/bin/imsg rpc" ssh-ed25519 AAAA... hermes-imsg
```

`from=` refuses the key from any other address; `restrict` turns off port, agent and X11
forwarding and PTY allocation; `command=` makes sshd run `imsg rpc` whatever the client asks for.
The test suite checks this form against a real `sshd`, and it was checked on a real Mac on Nov 5
([phase 0](docs/phase0-check.md)): a request for any other command runs `imsg rpc` instead.

Remote Login must allow the Mac user (System Settings → General → Sharing → Remote Login →
Allow access for). macOS lists only Administrators by default, and a standard user missing from
the list is refused after its key is accepted, with nothing but "Connection closed" on the client.

The pin limits *what runs*, not *what sshd can read*. `imsg` over SSH needs Full Disk Access for
Remote Login, and that grant covers every SSH login to the Mac. So, where you can:

- a dedicated key, used for nothing else;
- a dedicated macOS user signed into Messages, if your setup allows it;
- a Mac that does nothing else.

**No listener.** Neither this plugin nor `imsg` opens a port. Nothing on the LAN can reach
Messages through this path. The only connection is outbound SSH from the Hermes host.

**Host keys.** The SSH transport runs `ssh -o BatchMode=yes` and leaves host-key checking at
ssh's default, so an unknown or changed Mac host key fails closed. Put the Mac's key in
`known_hosts` once, by hand.

**The helper (SIP off).** `imsg`'s optional helper puts third-party code inside Messages and
needs System Integrity Protection off. This plugin never installs it, never changes SIP,
library validation or signing, and reads `imsg status` to see whether it is there. Rich actions
cost a weaker Mac; that choice is the Mac owner's. Do not combine SIP off, computer-use agents
and personal sign-ins on one Mac.

**Message bodies are data.** The plugin does not log message text at any level, and writes it
nowhere but the Hermes event. The SSH key path never appears in logs or errors: child stderr is
redacted before it is logged. Both are tested.

**What the plugin stores.** No credentials of any kind. The SSH private key lives with Hermes,
where you put it. From v0.1, a cursor (a message row id) per account.

**Not a policy engine.** A sender filter in config is a courtesy, not a security boundary.
Allowlists, approvals and who may talk to the agent belong in the Hermes host.
