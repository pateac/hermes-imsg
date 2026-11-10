"""`python -m hermes_imsg observe ...` and `python -m hermes_imsg audit ...` (also `hermes-imsg`).

observe  record every row imsg sees (ids, never text), starting no turns and sending nothing
audit    compare that record with the message ids another relay handed Hermes
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime
from pathlib import Path

from . import observe
from .transport import LocalTransport, SshTransport, Transport


def _when(text: str) -> float:
    """An ISO 8601 time; without a zone, local time."""
    return datetime.fromisoformat(text).astimezone().timestamp()


def _show(ts: float) -> str:
    return datetime.fromtimestamp(ts).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def _mask(handle: str) -> str:
    return f"...{handle[-4:]}" if len(handle) > 4 else "..."


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hermes-imsg",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="command", required=True)

    o = sub.add_parser("observe", help="record every row imsg sees; answer and send nothing")
    where = o.add_mutually_exclusive_group(required=True)
    where.add_argument("--host", help="the Mac, as ssh names it (an ssh config alias or user@host)")
    where.add_argument("--local", action="store_true", help="imsg runs on this Mac")
    o.add_argument("--identity-file", help="private key for --host")
    o.add_argument("--ssh-config", help="an ssh config file (-F)")
    o.add_argument("--imsg-path", default="imsg", help="imsg on the Mac (default: imsg)")
    o.add_argument(
        "--state-dir",
        required=True,
        type=Path,
        help="where observe.jsonl and the cursor live (keep it on an encrypted disk)",
    )

    a = sub.add_parser("audit", help="compare a record with what another relay handed Hermes")
    a.add_argument("--record", required=True, type=Path, help="observe.jsonl")
    a.add_argument(
        "--hermes-db",
        required=True,
        type=Path,
        action="append",
        help="a Hermes state.db; repeat for every profile (read-only)",
    )
    a.add_argument("--source", default="bluebubbles", help="the relay's platform name in Hermes")
    a.add_argument("--since", type=_when, help="window start, ISO 8601 (default: first record)")
    a.add_argument("--until", type=_when, help="window end, ISO 8601 (default: last record)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "observe":
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
        transport: Transport = (
            LocalTransport(args.imsg_path)
            if args.local
            else SshTransport(
                args.host,
                identity_file=args.identity_file,
                ssh_config=args.ssh_config,
                imsg_path=args.imsg_path,
            )
        )
        try:
            asyncio.run(observe.run(transport, args.state_dir))
        except KeyboardInterrupt:
            pass
        return 0

    result = observe.audit(
        args.record, args.hermes_db, source=args.source, since=args.since, until=args.until
    )
    print(f"window: {_show(result.since)} to {_show(result.until)}")
    print(
        f"{args.source} messages handed to Hermes in the window: {result.delivered}"
        f" (and {result.unidentified} with no message id, which cannot be checked)"
    )
    print(f"inbound rows recorded in the window: {result.recorded}")
    if result.unreadable_lines:
        print(f"unreadable record lines: {result.unreadable_lines}")
    print(f"missed (handed to Hermes, not recorded): {len(result.missed)}")
    for guid, ts in result.missed:
        print(f"  {_show(ts)}  {guid}")
    print(f"doubled (recorded more than once): {len(result.doubled)}")
    for guid in result.doubled:
        print(f"  {guid}")
    print(f"extra (recorded, not handed to Hermes; each needs a reason): {len(result.extra)}")
    for r in result.extra:
        kind = "group" if ";+;" in r.get("chat_guid", "") else "dm"
        print(f"  {_show(r['ts'])}  {r['guid']}  {kind}  sender {_mask(r.get('sender', ''))}")
    print("PASS" if result.passed else "FAIL")
    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(main())
