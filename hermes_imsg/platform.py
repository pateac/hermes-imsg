"""The Hermes platform adapter: iMessage through `imsg rpc`, locally or over SSH.

The only module that imports Hermes (`gateway.*`, tag v2026.9.24). Identity
follows Hermes's BlueBubbles adapter so a host's routing carries over: the chat
id is the chat GUID, the user id is the sender's handle, the message id is the
message GUID, and an attachment-only message reads "(attachment)".

config.yaml `gateway.platforms.imsg.extra` keys, each overridable by env:
  host (IMSG_SSH_HOST)                 SSH destination; set it for the ssh transport
  identity_file (IMSG_SSH_KEY)         private key for that host
  ssh_config (IMSG_SSH_CONFIG)         ssh config file (-F), optional
  transport (IMSG_TRANSPORT)           "local" when Hermes runs on the Mac itself
  imsg_path (IMSG_PATH)                imsg on the Mac (default "imsg")
  state_dir (IMSG_STATE_DIR)           where the cursor lives (default <hermes home>/imsg)
  sends_per_minute (IMSG_SENDS_PER_MINUTE)  rate limit (default 30)
  max_attachment_mb (IMSG_MAX_ATTACHMENT_MB) inbound size cap (default 25; local transport)
  require_mention, mention_patterns    group gating, as the BlueBubbles adapter
  observe_only (IMSG_OBSERVE_ONLY)     record each row to <state_dir>/observe.jsonl, start no
                                       turns and send nothing: for a run beside another relay
Who may talk to the agent is Hermes's job: IMSG_ALLOWED_USERS / IMSG_ALLOW_ALL_USERS.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from gateway.config import Platform
from gateway.platforms._shared import extra_or_secret, get_scoped_secret, seed_extra_from_env
from gateway.platforms.base import (
    BasePlatformAdapter,
    SendResult,
    cache_audio_from_bytes_async,
    cache_document_from_bytes_async,
    cache_image_from_bytes_async,
    cache_image_from_url,
)
from gateway.platforms.event import MessageEvent, MessageType
from gateway.platforms.helpers import (
    MessageDeduplicator,
    cancel_task,
    compile_mention_patterns,
    strip_markdown,
)
from hermes_constants import get_hermes_home

from . import events, status
from . import send as outbound
from .cursor import Cursor
from .rpc import NotConnected, Notification, RpcClient
from .transport import LocalTransport, SshTransport, Transport, TransportError

log = logging.getLogger(__name__)

NAME = "imsg"
TRUTHY = {"1", "true", "yes", "on"}
# Hermes's wake words, as its BlueBubbles adapter uses for `require_mention`.
DEFAULT_MENTION_PATTERNS = [r"(?<![\w@])@?hermes\s+agent\b[,:\-]?", r"(?<![\w@])@?hermes\b[,:\-]?"]
SecondaryListener = Callable[[events.Secondary], Awaitable[None]]
OBSERVE_ONLY = "imsg is observe-only: nothing is sent"


def _setting(extra: dict[str, Any], key: str, env: str, default: Any = "") -> Any:
    return extra_or_secret(extra, key, env, default)


def transport_from(extra: dict[str, Any]) -> Transport | None:
    """SSH when a host is set, local only when asked for; None when not configured."""
    imsg_path = str(_setting(extra, "imsg_path", "IMSG_PATH", "imsg"))
    host = str(_setting(extra, "host", "IMSG_SSH_HOST") or "")
    if host:
        return SshTransport(
            host,
            identity_file=str(_setting(extra, "identity_file", "IMSG_SSH_KEY") or "") or None,
            ssh_config=str(_setting(extra, "ssh_config", "IMSG_SSH_CONFIG") or "") or None,
            imsg_path=imsg_path,
        )
    if str(_setting(extra, "transport", "IMSG_TRANSPORT") or "").lower() == "local":
        return LocalTransport(imsg_path)
    return None


class ImsgAdapter(BasePlatformAdapter):  # type: ignore[misc]  # Hermes ships no type hints
    MAX_MESSAGE_LENGTH = 4000  # as the BlueBubbles adapter; iMessage itself has no hard limit
    splits_long_messages = True  # send() chunks with truncate_message

    def __init__(self, config: Any, **kwargs: Any) -> None:
        super().__init__(config=config, platform=Platform(NAME))
        extra: dict[str, Any] = getattr(config, "extra", {}) or {}
        self.transport = transport_from(extra)
        self.state_dir = str(_setting(extra, "state_dir", "IMSG_STATE_DIR") or "")
        self.limiter = outbound.RateLimiter(
            float(_setting(extra, "sends_per_minute", "IMSG_SENDS_PER_MINUTE", 30))
        )
        self.max_attachment_bytes = int(
            float(_setting(extra, "max_attachment_mb", "IMSG_MAX_ATTACHMENT_MB", 25)) * 1024 * 1024
        )
        require = extra.get("require_mention")
        if require is None:
            require = get_scoped_secret("IMSG_REQUIRE_MENTION")
        self.require_mention = str(require).strip().lower() in TRUTHY
        observe = _setting(extra, "observe_only", "IMSG_OBSERVE_ONLY")
        self.observe_only = str(observe).strip().lower() in TRUTHY
        raw_patterns = (
            extra["mention_patterns"]
            if "mention_patterns" in extra
            else get_scoped_secret("IMSG_MENTION_PATTERNS")
        )
        self._mention_patterns = compile_mention_patterns(
            raw_patterns, log_prefix=NAME, defaults=DEFAULT_MENTION_PATTERNS
        )
        self._dedup = MessageDeduplicator()  # the runner carries its ids over a reconnect
        self._client: RpcClient | None = None
        self._cursor: Cursor | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._chat_names: dict[str, str] = {}
        self.secondary_listeners: list[SecondaryListener] = []  # tapbacks, edits, polls
        self.counters = dict.fromkeys(
            ("received", "delivered", "duplicates", "secondary", "send_failures"), 0
        )

    @property
    def name(self) -> str:
        return "iMessage (imsg)"

    def health(self) -> dict[str, Any]:
        if self._client is None:
            return {"connected": False}
        return {**status.health(self._client), **self.counters}

    # --- lifecycle ---------------------------------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if self.transport is None:
            message = "set IMSG_SSH_HOST (or host:), or transport: local on the Mac"
            self._set_fatal_error("config_missing", message, retryable=False)
            return False
        where = self.transport.describe()
        if not self._acquire_platform_lock(NAME, where, f"imsg via {where}"):
            return False
        state_dir = Path(self.state_dir) if self.state_dir else Path(get_hermes_home()) / NAME
        self._cursor = Cursor(state_dir / "cursor.json")
        client = RpcClient(self.transport, on_connect=self._subscribe)
        client.start()
        try:
            await client.wait_ready(30)
        except NotConnected as exc:
            await client.close()
            with contextlib.suppress(Exception):
                self._release_platform_lock()
            self._set_fatal_error("connect_failed", str(exc), retryable=True)
            return False
        self._client = client
        self._tasks = [
            asyncio.create_task(self._consume(client)),
            asyncio.create_task(status.watch(client)),
        ]
        self._mark_connected()
        self._wire_plugin_handlers(None)
        log.info("imsg: connected via %s", where)
        return True

    async def disconnect(self) -> None:
        with contextlib.suppress(Exception):
            self._release_platform_lock()
        self._mark_disconnected()
        for task in self._tasks:
            await cancel_task(task)
        self._tasks = []
        if self._client is not None:
            await self._client.close()
            self._client = None

    async def _subscribe(self, client: RpcClient) -> None:
        """on_connect: resume from the cursor. imsg replays rows after it, then streams live."""
        assert self._cursor is not None
        database = (client.status or {}).get("database") or {}
        self._cursor.bind(str(database.get("path") or ""))
        params: dict[str, Any] = {"include_reactions": True, "attachments": True}
        if self._cursor.rowid:
            params["since_rowid"] = self._cursor.rowid
        await client.call("watch.subscribe", params)

    # --- inbound -----------------------------------------------------------------------

    async def _consume(self, client: RpcClient) -> None:
        while True:
            note = await client.notifications.get()
            try:
                await self._on_notification(client, note)
            except Exception:  # one bad row must not stop the stream; it is logged, not retried
                row = note.params.get("message")
                rowid = row.get("id") if isinstance(row, dict) else None
                log.exception(
                    "imsg: failed to handle a %s notification (row %s)", note.method, rowid
                )

    async def _on_notification(self, client: RpcClient, note: Notification) -> None:
        assert self._cursor is not None
        if note.method == "watch.overflow":
            log.warning("imsg: watch buffer overflowed; resubscribing from the cursor")
            await self._subscribe(client)
            return
        row = note.params.get("message")
        if note.method != "message" or not isinstance(row, dict):
            return
        rowid = row.get("id")
        new_rowid: int | None = None  # edits and unsends reuse an old row id; they never advance
        if "type" not in row and isinstance(rowid, int) and not isinstance(rowid, bool):
            new_rowid = rowid
        if new_rowid is not None and new_rowid <= self._cursor.rowid:
            self.counters["duplicates"] += 1  # a replay after reconnect or restart
            return
        event = events.classify(row)
        if isinstance(event, events.Inbound):
            self.counters["received"] += 1
            if self._dedup.is_duplicate(event.guid):
                self.counters["duplicates"] += 1
            elif self.observe_only:
                self._observe("inbound", event.rowid, event.guid, event.chat_guid, event.sender)
            else:
                await self._dispatch(event)
        elif isinstance(event, events.Secondary):
            self.counters["secondary"] += 1
            if self.observe_only:
                self._observe(event.kind, event.rowid, event.guid, event.chat_guid, event.sender)
            else:
                await self._notify_secondary(event)
        if new_rowid is not None:
            self._cursor.advance(new_rowid)  # after Hermes accepted it: at-least-once

    async def _notify_secondary(self, event: events.Secondary) -> None:
        for listener in list(self.secondary_listeners):
            try:
                await listener(event)
            except Exception:
                log.exception("imsg: a secondary-event listener failed")

    def _observe(self, kind: str, rowid: int, guid: str, chat_guid: str, sender: str) -> None:
        """observe_only: one JSON line per row the adapter would have handed on; no message text."""
        assert self._cursor is not None
        record = {
            "at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "kind": kind,
            "rowid": rowid,
            "guid": guid,
            "chat_guid": chat_guid,
            "sender": sender,
        }
        with open(self._cursor.path.with_name("observe.jsonl"), "a") as f:
            f.write(json.dumps(record) + "\n")

    async def _dispatch(self, event: events.Inbound) -> None:
        text = event.text
        if event.is_group and self.require_mention:
            if not any(p.search(text) for p in self._mention_patterns):
                return
            text = self._strip_wake_word(text)
        media_urls, media_types = await self._fetch_attachments(event.attachments)
        if not text.strip() and event.attachments:
            text = "(attachment)"
        self._chat_names[event.chat_guid] = event.chat_name or event.chat_identifier
        source = self.build_source(
            chat_id=event.chat_guid,
            chat_name=event.chat_name or event.chat_identifier or event.sender,
            chat_type="group" if event.is_group else "dm",
            user_id=event.sender,
            user_name=event.sender,
            chat_id_alt=event.chat_identifier or None,
        )
        await self.handle_message(
            MessageEvent(
                text=text,
                message_type=_message_type(media_types),
                source=source,
                message_id=event.guid,
                reply_to_message_id=event.reply_to_guid,
                media_urls=media_urls,
                media_types=media_types,
                timestamp=_local_time(event.created_at),
            )
        )
        self.counters["delivered"] += 1

    def _strip_wake_word(self, text: str) -> str:
        """Strip a leading wake word only, as the BlueBubbles adapter does."""
        stripped = text.lstrip()
        for pattern in self._mention_patterns:
            if match := pattern.match(stripped):
                return stripped[match.end() :].lstrip(" ,:-") or text
        return text

    async def _fetch_attachments(
        self, attachments: tuple[events.Attachment, ...]
    ) -> tuple[list[str], list[str]]:
        assert self.transport is not None
        urls: list[str] = []
        types: list[str] = []
        for a in attachments:
            if a.missing or not a.path or a.total_bytes > self.max_attachment_bytes:
                log.info("imsg: skipped an attachment (%s, %d bytes)", a.mime_type, a.total_bytes)
                continue
            try:  # over SSH through the gate on the Mac, which serves only Messages attachments
                data = await self.transport.fetch(a.path, self.max_attachment_bytes)
                mime = a.mime_type.lower()
                ext = os.path.splitext(a.filename)[1] or ""
                if mime.startswith("image/"):
                    path = await cache_image_from_bytes_async(data, ext or ".jpg")
                elif mime.startswith("audio/"):
                    path = await cache_audio_from_bytes_async(data, ext or ".m4a")
                else:
                    path = await cache_document_from_bytes_async(data, a.filename or "attachment")
            except Exception as exc:
                log.warning("imsg: could not read an attachment (%s): %s", a.mime_type, exc)
                continue
            urls.append(path)
            types.append(mime or "application/octet-stream")
        return urls, types

    # --- outbound ----------------------------------------------------------------------

    def format_message(self, content: str) -> str:
        return str(strip_markdown(content, keep_link_targets=True))  # Messages renders no markdown

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        # reply_to is ignored: threaded replies need the imsg helper (SIP off, phase 5).
        if self.observe_only:
            return SendResult(success=False, error=OBSERVE_ONLY)
        if self._client is None:
            return SendResult(success=False, error="Not connected", retryable=True)
        message_id = None
        for index, chunk in enumerate(self.truncate_message(content, self.MAX_MESSAGE_LENGTH)):
            await self.limiter.wait()
            result = await outbound.send(self._client, chat_id, text=chunk)
            if not result.ok:
                self.counters["send_failures"] += 1
                # A retry resends every chunk, so only a first chunk that never left may retry.
                return SendResult(
                    success=False, error=result.error, retryable=result.retryable and index == 0
                )
            message_id = result.message_id
        return SendResult(success=True, message_id=message_id)

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: str | None = None,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        try:
            path = await cache_image_from_url(image_url)
        except Exception as exc:
            return SendResult(success=False, error=f"could not download the image: {exc}")
        return await self._send_file(chat_id, path, caption)

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: str | None = None,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> SendResult:
        return await self._send_file(chat_id, image_path, caption)

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: str | None = None,
        file_name: str | None = None,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> SendResult:
        return await self._send_file(chat_id, file_path, caption, file_name)

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: str | None = None,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> SendResult:
        # shortcut: arrives as an audio file; a voice bubble needs the imsg helper (SIP off, v0.2)
        return await self._send_file(chat_id, audio_path, caption)

    async def send_video(
        self,
        chat_id: str,
        video_path: str,
        caption: str | None = None,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> SendResult:
        return await self._send_file(chat_id, video_path, caption)

    async def _send_file(
        self, chat_id: str, path: str, caption: str | None, name: str | None = None
    ) -> SendResult:
        if self.observe_only:
            return SendResult(success=False, error=OBSERVE_ONLY)
        if self._client is None or self.transport is None:
            return SendResult(success=False, error="Not connected", retryable=True)
        try:  # over SSH the file goes to the gate's outbox on the Mac first
            remote = await self.transport.put(path, name or os.path.basename(path))
        except (OSError, TransportError) as exc:
            self.counters["send_failures"] += 1
            return SendResult(success=False, error=f"could not place the file on the Mac: {exc}")
        await self.limiter.wait()
        result = await outbound.send(self._client, chat_id, text=caption, file=remote)
        if not result.ok:
            self.counters["send_failures"] += 1
        return SendResult(
            success=result.ok,
            message_id=result.message_id,
            error=result.error,
            retryable=result.retryable,
        )

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        name = self._chat_names.get(chat_id) or chat_id
        return {"name": name, "type": "group" if ";+;" in chat_id else "dm", "chat_id": chat_id}


def _message_type(media_types: list[str]) -> MessageType:
    if not media_types:
        return MessageType.TEXT
    if media_types[0].startswith("image/"):
        return MessageType.PHOTO
    if media_types[0].startswith("audio/"):
        return MessageType.AUDIO
    return MessageType.DOCUMENT


def _local_time(created_at: str) -> datetime:
    """imsg's ISO 8601 time as the naive local time Hermes events use; now if unparseable."""
    try:
        return datetime.fromisoformat(created_at).astimezone().replace(tzinfo=None)
    except ValueError:
        return datetime.now()


# --- registration ---------------------------------------------------------------------


def check_requirements() -> bool:
    """Passive: nothing to import beyond the standard library (and `ssh` for that transport)."""
    return True


def validate_config(config: Any) -> bool:
    return transport_from(getattr(config, "extra", {}) or {}) is not None


def _env_enablement() -> dict[str, Any] | None:
    host = str(get_scoped_secret("IMSG_SSH_HOST", "") or "").strip()
    local = str(get_scoped_secret("IMSG_TRANSPORT", "") or "").strip().lower() == "local"
    if not (host or local):
        return None
    seeded: dict[str, Any] = seed_extra_from_env(
        (
            ("IMSG_SSH_HOST", "host", None),
            ("IMSG_SSH_KEY", "identity_file", None),
            ("IMSG_SSH_CONFIG", "ssh_config", None),
            ("IMSG_TRANSPORT", "transport", None),
            ("IMSG_PATH", "imsg_path", None),
        ),
        home_env="IMSG_HOME_CHANNEL",
    )
    return seeded


def register(ctx: Any) -> None:
    """Plugin entry point (Hermes calls it with its PluginContext)."""
    ctx.register_platform(
        name=NAME,
        label="iMessage (imsg)",
        adapter_factory=ImsgAdapter,
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=validate_config,
        required_env=[],
        install_hint="Needs imsg on a Mac signed into Messages; see the hermes-imsg README",
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="IMSG_HOME_CHANNEL",
        allowed_users_env="IMSG_ALLOWED_USERS",
        allow_all_env="IMSG_ALLOW_ALL_USERS",
        max_message_length=ImsgAdapter.MAX_MESSAGE_LENGTH,
        emoji="💬",
        pii_safe=True,  # chat and user ids are phone numbers and emails
        platform_hint=(
            "You are chatting over iMessage. Messages does not render markdown: write plain "
            "text. Bare URLs become links. Reactions and threaded replies are not available."
        ),
    )
