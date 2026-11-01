"""A stand-in for `imsg rpc` (imsg 0.20.0 `docs/rpc.md`) so tests need no Mac.

It speaks the same newline-delimited JSON-RPC 2.0 on stdin/stdout, with the
same strictness: unknown params and wrong types are rejected with -32602, as
imsg does, so a client bug shows up here rather than on the Mac. It is a
protocol stand-in, not an emulator: methods it does not implement answer
-32601 saying so, and the shapes below follow imsg's docs and source as read
on Nov 1, 2026. Where imsg's behaviour was not confirmed (a send to an unknown
chat GUID), the fake picks the documented bridge behaviour and says so.

State lives in a SQLite file (`--db PATH`, as `imsg rpc --db`, or the
FAKE_IMSG_DB environment variable); see `store.py`. Stdlib only, Python 3.9+.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import sys
import uuid
from typing import Any

from .store import FakeDB

VERSION = "0.20.0-fake"

PARAMS: dict[str, dict[str, type]] = {
    "initialize": {"protocol_version": int},
    "status": {},
    "chats.list": {"limit": int},
    "chats.get": {"chat_id": int},
    "messages.history": {"chat_id": int, "limit": int, "attachments": bool},
    "messages.after": {
        "since_rowid": int,
        "chat_id": int,
        "limit": int,
        "attachments": bool,
        "include_reactions": bool,
    },
    "watch.subscribe": {
        "chat_id": int,
        "since_rowid": int,
        "attachments": bool,
        "include_reactions": bool,
        "debounce_ms": int,
        "buffer_limit": int,
    },
    "watch.unsubscribe": {"subscription": int},
    "send": {
        "to": str,
        "chat_id": int,
        "chat_identifier": str,
        "chat_guid": str,
        "text": str,
        "file": str,
        "service": str,
        "region": str,
        "allow_sms_fallback": bool,
        "transport": str,
    },
    "message.send_status": {"guid": str},
}
REQUIRED = {
    "chats.get": {"chat_id"},
    "messages.history": {"chat_id"},
    "messages.after": {"since_rowid"},
    "watch.unsubscribe": {"subscription"},
    "message.send_status": {"guid"},
}
DATABASE_METHODS = {
    "chats.list",
    "chats.get",
    "messages.history",
    "messages.after",
    "watch.subscribe",
    "message.send_status",
}
BRIDGE_MUTATIONS = {
    "send.rich",
    "send.attachment",
    "send.multipart",
    "send.sticker",
    "tapback",
    "message.edit",
    "message.unsend",
    "poll.send",
    "poll.vote",
    "poll.unvote",
    "chats.create",
}
BRIDGE_READS = {"handles.check", "bridge.events.subscribe"}
BRIDGE_DOWN = "The bridge is not started. Run imsg launch explicitly before using bridge methods."


class Fail(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


def invalid(detail: str) -> Fail:
    return Fail(-32602, "Invalid params", detail)


def not_started(operation: str, transport: str, detail: str) -> Fail:
    return Fail(
        -32603,
        "Delivery failed before dispatch",
        {
            "retry_safe": True,
            "disposition": "not_started",
            "transport": transport,
            "operation": operation,
            "detail": detail,
        },
    )


class Server:
    def __init__(self, db: FakeDB) -> None:
        self.db = db
        self.watches: dict[int, asyncio.Task[None]] = {}
        self.sub_ids = itertools.count(1)
        self.requests: set[asyncio.Task[None]] = set()
        self.mutations = asyncio.Lock()  # imsg runs mutations FIFO, one at a time

    # --- output --------------------------------------------------------------

    def write(self, obj: dict[str, Any]) -> None:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    def reply(self, rid: Any, result: Any) -> None:
        self.write({"jsonrpc": "2.0", "id": rid, "result": result})

    def error(self, rid: Any, code: int, message: str, data: Any = None) -> None:
        err: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            err["data"] = data
        self.write({"jsonrpc": "2.0", "id": rid, "error": err})

    # --- loop ----------------------------------------------------------------

    async def run(self) -> None:
        startup = self.db.take_fault("$startup")
        if startup:
            self.apply_side_fault(*startup)
        while True:
            line = await asyncio.to_thread(sys.stdin.buffer.readline)
            if not line:
                break
            if line.strip():
                self.accept(line)
        # EOF: like imsg, stop watches, drain accepted requests, flush, exit 0.
        for task in self.watches.values():
            task.cancel()
        await asyncio.gather(*self.watches.values(), return_exceptions=True)
        await asyncio.gather(*self.requests, return_exceptions=True)

    def accept(self, line: bytes) -> None:
        try:
            req = json.loads(line)
        except ValueError:
            return self.error(None, -32700, "Parse error")
        if not isinstance(req, dict):
            return self.error(None, -32600, "Invalid Request")
        notify = "id" not in req
        rid = req.get("id")
        id_ok = rid is None or (isinstance(rid, (str, int)) and not isinstance(rid, bool))
        method = req.get("method")
        if req.get("jsonrpc") != "2.0" or not isinstance(method, str) or not method or not id_ok:
            if not notify:
                self.error(rid if id_ok else None, -32600, "Invalid Request")
            return
        task = asyncio.ensure_future(self.dispatch(rid, notify, method, req.get("params", {})))
        self.requests.add(task)
        task.add_done_callback(self.requests.discard)

    def apply_side_fault(self, action: str, arg: dict[str, Any]) -> None:
        if action == "exit":
            os._exit(arg.get("code", 1))
        if action == "garbage":
            sys.stdout.write(arg.get("text", "this is not json") + "\n")
            sys.stdout.flush()
        if action == "stderr":
            sys.stderr.write(arg["text"] + "\n")
            sys.stderr.flush()

    async def dispatch(self, rid: Any, notify: bool, method: str, params: Any) -> None:
        self.db.record_call(method, params)
        fault = self.db.take_fault(method)
        if fault:
            action, arg = fault
            if action == "drop":
                return
            if action == "delay":
                await asyncio.sleep(arg["ms"] / 1000)
            if action == "error":
                if not notify:
                    self.error(rid, arg["code"], arg.get("message", "Injected"), arg.get("data"))
                return
            self.apply_side_fault(action, arg)
        try:
            result = await self.handle(method, params)
        except Fail as fail:
            if not notify:
                self.error(rid, fail.code, fail.message, fail.data)
            return
        if not notify:
            self.reply(rid, result)

    # --- methods -------------------------------------------------------------

    async def handle(self, method: str, params: Any) -> Any:
        if not isinstance(params, dict):
            raise invalid("params must be an object")
        bridge_ready = self.db.get("bridge_ready", "0") == "1"
        if method in BRIDGE_MUTATIONS:
            if not bridge_ready:
                raise not_started(
                    method,
                    "bridge_v2",
                    "The bridge ready lock is absent. Run imsg launch explicitly before retrying.",
                )
            return {"ok": True}
        if method in BRIDGE_READS:
            if not bridge_ready:
                raise Fail(-32003, "Bridge unavailable", {"detail": BRIDGE_DOWN, "retryable": True})
            return {"ok": True}
        spec = PARAMS.get(method)
        if spec is None:
            raise Fail(-32601, "Method not found", f"fake imsg does not implement {method}")
        for key, value in params.items():
            if key not in spec:
                raise invalid(f"Unknown parameter: {key}")
            if type(value) is not spec[key]:  # type-strict, as imsg: True is not an int
                raise invalid(f"{key} must be {spec[key].__name__}")
        missing = sorted(REQUIRED.get(method, set()) - params.keys())
        if missing:
            raise invalid(f"Missing parameter: {missing[0]}")
        if method in DATABASE_METHODS and self.db.get("db_ready", "1") != "1":
            raise Fail(
                -32002,
                "Database unavailable",
                {"path": self.db.path, "detail": "unable to open database file", "retryable": True},
            )
        handler = getattr(self, "m_" + method.replace(".", "_"))
        return await handler(**params)

    def status(self) -> dict[str, Any]:
        db_ready = self.db.get("db_ready", "1") == "1"
        bridge_ready = self.db.get("bridge_ready", "0") == "1"
        methods = ["initialize", "status", "watch.unsubscribe", "send"]
        if db_ready:
            methods += sorted(DATABASE_METHODS)
        if bridge_ready:
            methods += sorted(BRIDGE_MUTATIONS | BRIDGE_READS)
        database: dict[str, Any] = {"path": self.db.path, "ready": db_ready}
        if not db_ready:
            database["error"] = "unable to open database file"
        bridge: dict[str, Any] = {"ready": bridge_ready}
        if not bridge_ready:
            bridge["error"] = BRIDGE_DOWN
        return {
            "version": VERSION,
            "protocol_version": 1,
            "database": database,
            "bridge": bridge,
            "contacts": {"available": False},
            "methods": methods,
            "supported_methods": sorted(set(PARAMS) | BRIDGE_MUTATIONS | BRIDGE_READS),
        }

    async def m_initialize(self, protocol_version: int = 1) -> dict[str, Any]:
        if protocol_version != 1:
            raise invalid(f"unsupported protocol_version {protocol_version}")
        return self.status()

    async def m_status(self) -> dict[str, Any]:
        return self.status()

    async def m_chats_list(self, limit: int = 20) -> dict[str, Any]:
        return {"chats": self.db.chats()[:limit]}

    def need_chat(self, chat_id: int) -> dict[str, Any]:
        chat = self.db.chat(chat_id)
        if chat is None:
            raise invalid(f"Chat not found: {chat_id}")
        return chat

    async def m_chats_get(self, chat_id: int) -> dict[str, Any]:
        chat = self.need_chat(chat_id)
        keys = ("id", "identifier", "guid", "name", "service", "is_group", "participants")
        return {k: chat[k] for k in keys}

    @staticmethod
    def shape(row: dict[str, Any], attachments: bool) -> dict[str, Any]:
        return row if attachments else {**row, "attachments": []}

    async def m_messages_history(
        self, chat_id: int, limit: int = 50, attachments: bool = False
    ) -> dict[str, Any]:
        self.need_chat(chat_id)
        rows = [r for r in self.db.messages_after(0, chat_id) if not r.get("is_reaction")]
        return {"messages": [self.shape(r, attachments) for r in reversed(rows)][:limit]}

    async def m_messages_after(
        self,
        since_rowid: int,
        chat_id: int | None = None,
        limit: int = 100,
        attachments: bool = False,
        include_reactions: bool = False,
    ) -> dict[str, Any]:
        if since_rowid < 0 or not 1 <= limit <= 500:
            raise invalid("since_rowid must be >= 0 and limit 1...500")
        out: list[dict[str, Any]] = []
        next_rowid, has_more = since_rowid, False
        for row in self.db.messages_after(since_rowid, chat_id):
            if include_reactions or not row.get("is_reaction"):
                if len(out) == limit:
                    has_more = True
                    break
                out.append(self.shape(row, attachments))
            next_rowid = row["id"]  # the physical scan cursor, past skipped rows too
        return {"messages": out, "next_rowid": next_rowid, "has_more": has_more}

    async def m_watch_subscribe(
        self,
        chat_id: int | None = None,
        since_rowid: int = 0,
        attachments: bool = False,
        include_reactions: bool = False,
        debounce_ms: int = 500,
        buffer_limit: int = 256,
    ) -> dict[str, Any]:
        if chat_id is not None:
            self.need_chat(chat_id)
        if not 1 <= buffer_limit <= 4096:
            raise invalid("buffer_limit must be 1...4096")
        # 0 (or omitted) starts at the tail, -1 replays everything, n > 0 resumes after n.
        cursor = self.db.max_rowid() if since_rowid == 0 else max(since_rowid, 0)
        sid = next(self.sub_ids)
        # The reply is written before this task first runs, as imsg promises.
        self.watches[sid] = asyncio.ensure_future(
            self.watch(sid, chat_id, cursor, attachments, include_reactions, buffer_limit)
        )
        return {"subscription": sid, "buffer_limit": buffer_limit}

    async def watch(
        self,
        sid: int,
        chat_id: int | None,
        cursor: int,
        attachments: bool,
        include_reactions: bool,
        buffer_limit: int,
    ) -> None:
        # shortcut: debounce_ms is accepted but not modelled; the fake emits on its next poll.
        seq = self.db.max_mutation_seq()  # edits before the subscription are the baseline
        while True:
            rows = self.db.messages_after(cursor, chat_id)
            eligible = [r for r in rows if include_reactions or not r.get("is_reaction")]
            if rows:
                cursor = rows[-1]["id"]
            for i, row in enumerate(eligible):
                if i == buffer_limit:
                    self.write(
                        {
                            "jsonrpc": "2.0",
                            "method": "watch.overflow",
                            "params": {
                                "subscription": sid,
                                "resume_after_rowid": eligible[i - 1]["id"],
                                "reason": "buffer_limit_exceeded",
                                "terminal": True,
                            },
                        }
                    )
                    self.watches.pop(sid, None)
                    return
                self.notify(sid, self.shape(row, attachments))
            for update_seq, update in self.db.mutations_after(seq):
                seq = update_seq
                if chat_id is None or update["chat_id"] == chat_id:
                    self.notify(sid, self.shape(update, attachments))
            await asyncio.sleep(0.02)

    def notify(self, sid: int, message: dict[str, Any]) -> None:
        self.write(
            {
                "jsonrpc": "2.0",
                "method": "message",
                "params": {"subscription": sid, "message": message},
            }
        )

    async def m_watch_unsubscribe(self, subscription: int) -> dict[str, Any]:
        task = self.watches.pop(subscription, None)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return {"ok": True}

    async def m_send(
        self,
        to: str | None = None,
        chat_id: int | None = None,
        chat_identifier: str | None = None,
        chat_guid: str | None = None,
        text: str | None = None,
        file: str | None = None,
        service: str = "auto",
        region: str = "US",
        allow_sms_fallback: bool = True,
        transport: str = "auto",
    ) -> dict[str, Any]:
        targets = [t for t in (to, chat_id, chat_identifier, chat_guid) if t is not None]
        if len(targets) != 1:
            raise invalid("send needs exactly one of to, chat_id, chat_identifier, chat_guid")
        if not text and not file:
            raise invalid("send needs text or file")
        async with self.mutations:
            if chat_id is not None:
                chat = self.need_chat(chat_id)
            elif to is not None:
                chat = self.db.find_chat("identifier", to) or self.db.add_chat(to)
            else:
                field, value = ("guid", chat_guid) if chat_guid else ("identifier", chat_identifier)
                found = self.db.find_chat(field, str(value))
                if found is None:  # unverified on a Mac: modelled on imsg's bridge behaviour
                    raise not_started("send", "applescript", "Chat not found")
                chat = found
            extra: dict[str, Any] = {}
            if file:
                name = os.path.basename(file)
                extra["attachments"] = [
                    {
                        "filename": name,
                        "transfer_name": name,
                        "original_path": file,
                        "missing": False,
                        "mime_type": "application/octet-stream",
                    }
                ]
            guid = str(uuid.uuid4()).upper()
            rowid = self.db.add_message(chat["id"], text or "", is_from_me=True, guid=guid, **extra)
        return {"ok": True, "transport": "applescript", "id": rowid, "guid": guid}

    async def m_message_send_status(self, guid: str) -> dict[str, Any]:
        sent = [r for r in self.db.messages_after(0) if r["guid"] == guid and r["is_from_me"]]
        if not sent:
            return {"ok": True, "guid": guid, "send_state": "pending", "status_fields": None}
        return {
            "ok": True,
            "guid": guid,
            "send_state": "delivered",
            "service": "iMessage",
            "status_fields": {"is_sent": True, "is_delivered": True, "error": 0},
        }


def main(argv: list[str]) -> int:
    args = list(argv)
    db_path = os.environ.get("FAKE_IMSG_DB")
    if "--db" in args:
        i = args.index("--db")
        db_path = args[i + 1]
        del args[i : i + 2]
    if args != ["rpc"] or not db_path:
        sys.stderr.write("usage: imsg rpc [--db PATH] (fake: needs --db or FAKE_IMSG_DB)\n")
        return 64
    db = FakeDB(db_path)
    db.record_spawn(argv)
    asyncio.run(Server(db).run())
    return 0
