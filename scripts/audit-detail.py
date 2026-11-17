#!/usr/bin/env python3
"""The observe week, in detail: counts and times only. Read-only; writes nothing.

    python3 -I audit-detail.py --code ~/hermes-imsg \\
        --record /path/to/observe.jsonl --agents /path/searched/for/state.db

`--code` is a hermes-imsg checkout; its `audit` makes the headline numbers, so they equal
`hermes-imsg audit`'s. The window is every whole local day from the one after the record's
first line to the start of today (or `--since` / `--until`, ISO 8601). The report names no
message, chat or text: senders appear as their last four characters.

Extras (recorded, not handed to Hermes) are given a reason, tried in this order:
  sender Hermes never routed   the sender is in no relay session in any state.db
  group chat                   a group chat Hermes did not answer from (its gating)
  folded into a nearby turn    the relay handed over another message in the same chat
                               within FOLD seconds after it (Hermes keeps one id per turn)
  no relay traffic nearby      nothing from the relay within QUIET seconds either side
                               (quiet hours, or the relay or Hermes down: not decidable here)
  other
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from itertools import pairwise
from pathlib import Path

FOLD = 180.0
QUIET = 900.0
DETAIL_SQL = (
    "SELECT m.platform_message_id, m.timestamp, s.user_id, s.chat_id FROM messages m "
    "JOIN sessions s ON s.id = m.session_id "
    "WHERE s.source = ? AND m.role = 'user' AND m.timestamp BETWEEN ? AND ?"
)
SENDERS_SQL = "SELECT DISTINCT user_id FROM sessions WHERE source = ?"


def mask(handle: str | None) -> str:
    return f"...{handle[-4:]}" if handle and len(handle) > 4 else "..."


def day(ts: float) -> str:
    return datetime.fromtimestamp(ts).astimezone().strftime("%a %b %d")


def when(ts: float) -> str:
    return datetime.fromtimestamp(ts).astimezone().strftime("%b %d %H:%M:%S %Z")


def midnight_after(ts: float) -> float:
    d = datetime.fromtimestamp(ts).astimezone()
    start = d.replace(hour=0, minute=0, second=0, microsecond=0)
    return (start if start == d else start + timedelta(days=1)).timestamp()


def find_dbs(root: str, depth: int = 4) -> tuple[list[Path], int]:
    found: list[Path] = []
    denied = 0

    def onerror(_: OSError) -> None:
        nonlocal denied
        denied += 1

    base = root.rstrip("/").count("/")
    for path, dirs, files in os.walk(root, onerror=onerror):
        if path.rstrip("/").count("/") - base >= depth:
            dirs[:] = []
        if "state.db" in files:
            found.append(Path(path) / "state.db")
    return sorted(found), denied


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--code", required=True, help="a hermes-imsg checkout")
    ap.add_argument("--record", required=True, type=Path)
    ap.add_argument("--agents", required=True, help="searched (4 levels) for state.db files")
    ap.add_argument("--source", default="bluebubbles")
    ap.add_argument("--since")
    ap.add_argument("--until")
    args = ap.parse_args()
    sys.path.insert(0, os.path.expanduser(args.code))
    from hermes_imsg import observe  # the tool's own audit

    rows = []
    for line in args.record.read_text().splitlines():
        try:
            r = json.loads(line)
            r["ts"] = datetime.fromisoformat(r["at"]).timestamp()
            rows.append(r)
        except (ValueError, KeyError, TypeError):
            continue
    if not rows:
        print("the record has no readable lines")
        return 1
    first, last = min(r["ts"] for r in rows), max(r["ts"] for r in rows)
    since = datetime.fromisoformat(args.since).astimezone().timestamp() if args.since else None
    until = datetime.fromisoformat(args.until).astimezone().timestamp() if args.until else None
    since = since if since is not None else midnight_after(first)
    if until is None:
        until = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        until = until.timestamp()
    if until <= since:
        print(f"no whole day yet: the record starts {when(first)}")
        return 1

    dbs, denied = find_dbs(args.agents)
    result = observe.audit(args.record, dbs, source=args.source, since=since, until=until)

    delivered: dict[str, tuple[float, str, str]] = {}  # id -> (time, sender, chat)
    senders_ever: set[str] = set()
    for db in dbs:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            g = observe.GRACE
            for mid, ts, user, chat in conn.execute(
                DETAIL_SQL, (args.source, since - g, until + g)
            ):
                if mid:
                    delivered[str(mid)] = (float(ts), user or "", chat or "")
            senders_ever |= {u for (u,) in conn.execute(SENDERS_SQL, (args.source,)) if u}
        finally:
            conn.close()

    window = [r for r in rows if since <= r["ts"] <= until]
    inbound = [r for r in window if r.get("kind") == "inbound"]
    inside = {g: v for g, v in delivered.items() if since <= v[0] <= until}

    print(f"record: {args.record} ({len(rows)} lines, {when(first)} to {when(last)})")
    print(f"state.db files read (read-only): {len(dbs)}; folders not readable: {denied}")
    print(f"window: {when(since)} to {when(until)}")
    print()
    print(f"handed to Hermes by {args.source}: {result.delivered}", end="")
    print(f" (no message id: {result.unidentified})")
    print(f"inbound recorded: {result.recorded}")
    print(f"missed: {len(result.missed)}   doubled: {len(result.doubled)}", end="")
    print(f"   extra: {len(result.extra)}")
    print("PASS" if result.passed else "FAIL")

    if result.missed:
        print("\nmissed, by time (and whether the record has any line within 10 min: observer up)")
        for _, ts in result.missed:
            near = any(abs(r["ts"] - ts) <= 600 for r in rows)
            print(f"  {when(ts)}  observer {'up' if near else 'silent'}")
    as_secondary = getattr(result, "as_secondary", [])  # older checkouts lack it
    if as_secondary:
        print(
            "\nhanded to Hermes as a message, recorded as a reaction, edit or poll"
            " (the relay made them turns)"
        )
        for kind, n in Counter(k for _, _, k in as_secondary).most_common():
            print(
                f"  {n:4}  {kind}: " + ", ".join(when(t) for _, t, k in as_secondary if k == kind)
            )
    if result.doubled:
        print(f"\ndoubled: {len(result.doubled)} message(s) recorded more than once")

    reasons: Counter[str] = Counter()
    extra_times: dict[str, list[float]] = defaultdict(list)
    for r in result.extra:
        sender, chat, ts = r.get("sender", ""), r.get("chat_guid", ""), r["ts"]
        near_same_chat = any(c == chat and 0 <= t - ts <= FOLD for t, _, c in delivered.values())
        near_any = any(abs(t - ts) <= QUIET for t, _, _ in delivered.values())
        if sender not in senders_ever:
            reason = "sender Hermes never routed"
        elif ";+;" in chat:
            reason = "group chat"
        elif near_same_chat:
            reason = "folded into a nearby turn"
        elif not near_any:
            reason = "no relay traffic nearby"
        else:
            reason = "other"
        reasons[reason] += 1
        extra_times[reason].append(ts)
    if result.extra:
        print("\nextras by reason")
        for reason, n in reasons.most_common():
            times = ", ".join(when(t) for t in sorted(extra_times[reason])[:8])
            more = " ..." if n > 8 else ""
            print(f"  {n:4}  {reason}: {times}{more}")

    print("\nby day (inbound recorded / handed to Hermes / missed / extra; other kinds recorded)")
    missed_days = Counter(day(ts) for _, ts in result.missed)
    extra_days = Counter(day(r["ts"]) for r in result.extra)
    kinds_by_day: dict[str, Counter[str]] = defaultdict(Counter)
    for r in window:
        if r.get("kind") != "inbound":
            kinds_by_day[day(r["ts"])][r.get("kind", "?")] += 1
    rec_days = Counter(day(r["ts"]) for r in inbound)
    del_days = Counter(day(v[0]) for v in inside.values())
    t = since
    while t < until:
        d = day(t + 1)
        other = ", ".join(f"{k} {n}" for k, n in sorted(kinds_by_day[d].items())) or "none"
        counts = f"{rec_days[d]:4} / {del_days[d]:4} / {missed_days[d]:3} / {extra_days[d]:3}"
        print(f"  {d}: {counts};  {other}")
        t = (
            (datetime.fromtimestamp(t).astimezone() + timedelta(days=1, hours=1))
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .timestamp()
        )

    print("\nby sender (inbound recorded / handed to Hermes / extra; known to Hermes)")
    by_rec = Counter(r.get("sender", "") for r in inbound)
    by_del = Counter(v[1] for v in inside.values())
    by_extra = Counter(r.get("sender", "") for r in result.extra)
    for s in sorted(set(by_rec) | set(by_del), key=lambda s: -(by_rec[s] + by_del[s])):
        known = "yes" if s in senders_ever else "no"
        print(f"  {mask(s):>8}: {by_rec[s]:4} / {by_del[s]:4} / {by_extra[s]:3};  {known}")

    print("\nby kind, recorded in the window")
    for k, n in Counter(r.get("kind", "?") for r in window).most_common():
        print(f"  {k}: {n}")

    gaps = sorted(((b["ts"] - a["ts"], a["ts"]) for a, b in pairwise(rows)), reverse=True)
    print("\nlongest silences in the record (a restart or reboot shows as one; so do quiet nights)")
    for gap, start in gaps[:5]:
        print(f"  {gap / 3600:5.1f} h  from {when(start)}")

    first_seen: dict[str, float] = {}
    for r in inbound:  # a doubled message counts once, at its first line
        first_seen.setdefault(r["guid"], r["ts"])
    lags = [t - inside[g][0] for g, t in first_seen.items() if g in inside]
    print("\nseconds from Hermes's time to the record's, messages both saw (record has 1 s steps)")
    if lags:
        lags.sort()
        p95 = lags[min(len(lags) - 1, int(0.95 * len(lags)))]
        print(
            f"  n {len(lags)}  min {lags[0]:.1f}  median {statistics.median(lags):.1f}"
            f"  p95 {p95:.1f}  max {lags[-1]:.1f}  (negative: the record saw it first)"
        )
    else:
        print("  none")
    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(main())
