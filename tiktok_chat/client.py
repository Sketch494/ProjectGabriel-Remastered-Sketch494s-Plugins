"""TikTok Live chat client + buffer + relay loop.

Wraps the third-party ``TikTokLive`` package and gives the rest of the plugin
a small, async-friendly surface:

    client = TikTokChatClient(cfg, tool_handler, logger)
    await client.connect("someuser")
    snapshot = client.get_recent(limit=20)
    await client.set_relay_mode("live_silent", interval_seconds=10)
    await client.disconnect()

Three relay modes decide what the chat does to the live Gemini session:

    * ``buffer``      — never auto-inject. Tools must pull via getRecentTikTokChat.
    * ``live_silent`` — periodically inject a batched chunk as **user context**
                        with ``turn_complete=False`` so the AI sees it without
                        being forced to immediately respond.
    * ``live_reply``  — same batch, but ``turn_complete=True`` so the AI is
                        prompted to actually reply to the chat in real time.

Thread/loop boundaries: TikTokLive runs its own asyncio event handlers. We
attach event handlers that record messages into a thread-safe ring buffer and
re-emit ``message_in`` events into the plugin bus. The relay loop is its own
asyncio task owned by the host loop.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from typing import Any

_TIKTOK_IMPORT_ERROR: str | None = None
try:
    from TikTokLive import TikTokLiveClient  # type: ignore[import-not-found]
    from TikTokLive.events import (  # type: ignore[import-not-found]
        CommentEvent,
        ConnectEvent,
        DisconnectEvent,
        FollowEvent,
        GiftEvent,
        LikeEvent,
        ShareEvent,
    )
except Exception as _e:  # pragma: no cover - optional dep
    _TIKTOK_IMPORT_ERROR = f"{type(_e).__name__}: {_e}"
    TikTokLiveClient = None  # type: ignore[assignment]
    CommentEvent = ConnectEvent = DisconnectEvent = None  # type: ignore[assignment]
    FollowEvent = GiftEvent = LikeEvent = ShareEvent = None  # type: ignore[assignment]


_VALID_MODES = {"buffer", "live_silent", "live_reply"}


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


class TikTokChatClient:
    def __init__(self, cfg: dict[str, Any], tool_handler: Any, logger: logging.Logger):
        self._cfg = cfg or {}
        self._handler = tool_handler
        self._log = logger

        self._client = None  # TikTokLiveClient | None
        self._username: str = ""
        self._connect_task: asyncio.Task | None = None
        self._relay_task: asyncio.Task | None = None
        self._connected = False
        self._connected_at: float | None = None
        self._connect_error: str | None = None

        self._buffer: deque[dict[str, Any]] = deque(maxlen=int(self._cfg.get("buffer_size", 200) or 200))
        self._next_index = 0
        self._counts = {"comment": 0, "gift": 0, "follow": 0, "like": 0, "share": 0}
        self._last_event_at: float | None = None

        self._relay_mode: str = str(self._cfg.get("relay_mode") or "buffer").lower()
        if self._relay_mode not in _VALID_MODES:
            self._relay_mode = "buffer"
        self._relay_interval = float(self._cfg.get("relay_interval_seconds") or 8)
        self._relay_min_messages = int(self._cfg.get("relay_min_messages") or 1)
        self._last_relayed_index = 0

        self._include_gifts = bool(self._cfg.get("include_gifts", True))
        self._include_follows = bool(self._cfg.get("include_follows", True))
        self._include_likes = bool(self._cfg.get("include_likes", False))
        self._include_shares = bool(self._cfg.get("include_shares", False))
        self._prefix = str(self._cfg.get("prefix") or "[TikTok]")
        self._filter_min_chars = int(self._cfg.get("filter_min_chars") or 1)

    # ---- public surface --------------------------------------------------

    @property
    def is_available(self) -> bool:
        return TikTokLiveClient is not None

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def username(self) -> str:
        return self._username

    @property
    def relay_mode(self) -> str:
        return self._relay_mode

    def status(self) -> dict[str, Any]:
        return {
            "available": self.is_available,
            "connected": self._connected,
            "username": self._username,
            "relayMode": self._relay_mode,
            "relayIntervalSeconds": self._relay_interval,
            "bufferSize": len(self._buffer),
            "bufferCapacity": self._buffer.maxlen,
            "counts": dict(self._counts),
            "connectedSeconds": (time.time() - self._connected_at) if self._connected_at else 0.0,
            "lastError": self._connect_error,
            "lastEventAt": self._last_event_at,
            "importError": _TIKTOK_IMPORT_ERROR,
        }

    def get_recent(
        self,
        *,
        limit: int = 50,
        since_index: int | None = None,
        kinds: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        items = list(self._buffer)
        if since_index is not None:
            items = [it for it in items if it["index"] > since_index]
        if kinds:
            allow = {k.lower() for k in kinds}
            items = [it for it in items if it["kind"] in allow]
        if limit and limit > 0:
            items = items[-limit:]
        return items

    def clear_buffer(self) -> int:
        n = len(self._buffer)
        self._buffer.clear()
        return n

    # ---- connection lifecycle -------------------------------------------

    def schedule_connect(self, username: str) -> None:
        """Fire-and-forget connect from synchronous contexts (startup hook)."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None:
            return
        loop.create_task(self.connect(username))

    async def connect(self, username: str) -> dict[str, Any]:
        if not self.is_available:
            hint = (
                "Install into this project's .venv (the same interpreter as run.bat), "
                "e.g. uv pip install --python .venv/Scripts/python.exe TikTokLive 'pyee>=11,<12'."
            )
            detail = _TIKTOK_IMPORT_ERROR or "TikTokLive module not found"
            return {"result": "error", "message": f"TikTokLive unavailable ({detail}). {hint}"}
        username = (username or "").strip().lstrip("@")
        if not username:
            return {"result": "error", "message": "username is required"}

        if self._connected and self._username == username:
            return {"result": "ok", "message": f"already connected to @{username}", **self.status()}

        if self._connected:
            await self.disconnect()

        api_key = self._resolve_api_key()
        try:
            kwargs: dict[str, Any] = {"unique_id": f"@{username}"}
            if api_key:
                kwargs["sign_api_key"] = api_key
            self._client = TikTokLiveClient(**kwargs)  # type: ignore[misc]
        except Exception as e:
            self._connect_error = f"client init failed: {e}"
            self._log.error(f"tiktok_chat: {self._connect_error}", exc_info=True)
            return {"result": "error", "message": self._connect_error}

        self._wire_events(self._client)
        self._username = username
        self._connect_error = None

        try:
            self._connect_task = asyncio.create_task(
                self._run_client(self._client),
                name=f"tiktok-chat-@{username}",
            )
        except Exception as e:
            self._connect_error = f"connect task failed: {e}"
            return {"result": "error", "message": self._connect_error}

        if self._relay_mode in {"live_silent", "live_reply"}:
            self._ensure_relay_loop()

        # Wait briefly for connect/disconnect to materialize so the AI gets useful feedback.
        for _ in range(50):  # ~5s
            await asyncio.sleep(0.1)
            if self._connected or self._connect_error:
                break
        if self._connect_error:
            return {"result": "error", "message": self._connect_error}
        return {"result": "ok", "message": f"connected to @{username}", **self.status()}

    async def disconnect(self) -> dict[str, Any]:
        if not self._connected and self._client is None:
            return {"result": "ok", "message": "not connected", **self.status()}
        client = self._client
        self._client = None
        if client is not None:
            try:
                disconnect = getattr(client, "disconnect", None)
                if disconnect is not None:
                    res = disconnect()
                    if asyncio.iscoroutine(res):
                        await asyncio.wait_for(res, timeout=5)
            except Exception as e:
                self._log.debug(f"tiktok_chat disconnect: {e}")
        await self._cancel_task("_connect_task")
        await self._cancel_task("_relay_task")
        self._connected = False
        self._connected_at = None
        prev = self._username
        self._username = ""
        return {"result": "ok", "message": f"disconnected from @{prev}" if prev else "disconnected", **self.status()}

    async def stop(self) -> None:
        await self.disconnect()

    # ---- relay configuration --------------------------------------------

    async def set_relay_mode(self, mode: str, interval_seconds: float | None = None) -> dict[str, Any]:
        mode = (mode or "").strip().lower()
        if mode not in _VALID_MODES:
            return {
                "result": "error",
                "message": f"invalid mode '{mode}'. Allowed: {sorted(_VALID_MODES)}",
            }
        self._relay_mode = mode
        if interval_seconds is not None:
            self._relay_interval = max(2.0, float(interval_seconds))
        if mode == "buffer":
            await self._cancel_task("_relay_task")
        else:
            self._ensure_relay_loop()
        return {"result": "ok", "relayMode": mode, "relayIntervalSeconds": self._relay_interval}

    # ---- internals -------------------------------------------------------

    def _resolve_api_key(self) -> str:
        key = str(self._cfg.get("api_key") or "").strip()
        if key:
            return key
        env = str(self._cfg.get("api_key_env") or "").strip()
        if env:
            return os.environ.get(env, "") or ""
        return ""

    def _wire_events(self, client: Any) -> None:
        if CommentEvent is None:
            return

        @client.on(ConnectEvent)
        async def _on_connect(event):  # noqa: ARG001
            self._connected = True
            self._connected_at = time.time()
            self._connect_error = None
            self._log.info(f"tiktok_chat: connected to @{self._username}")

        @client.on(DisconnectEvent)
        async def _on_disconnect(event):  # noqa: ARG001
            self._connected = False
            self._log.info(f"tiktok_chat: disconnected from @{self._username}")

        @client.on(CommentEvent)
        async def _on_comment(event):
            text = (getattr(event, "comment", "") or "").strip()
            if len(text) < self._filter_min_chars:
                return
            user = self._user_label(event)
            self._record("comment", user, text)

        if self._include_gifts and GiftEvent is not None:
            @client.on(GiftEvent)
            async def _on_gift(event):
                user = self._user_label(event)
                gift = getattr(getattr(event, "gift", None), "name", "gift")
                count = getattr(event, "repeat_count", None) or getattr(event, "count", None) or 1
                self._record("gift", user, f"sent {count}x {gift}")

        if self._include_follows and FollowEvent is not None:
            @client.on(FollowEvent)
            async def _on_follow(event):
                user = self._user_label(event)
                self._record("follow", user, "followed")

        if self._include_likes and LikeEvent is not None:
            @client.on(LikeEvent)
            async def _on_like(event):
                user = self._user_label(event)
                count = getattr(event, "count", None) or 1
                self._record("like", user, f"liked x{count}")

        if self._include_shares and ShareEvent is not None:
            @client.on(ShareEvent)
            async def _on_share(event):
                user = self._user_label(event)
                self._record("share", user, "shared the stream")

    @staticmethod
    def _user_label(event: Any) -> str:
        u = getattr(event, "user", None)
        if u is None:
            return "viewer"
        nick = getattr(u, "nickname", None)
        if nick:
            return str(nick)
        uid = getattr(u, "unique_id", None)
        if uid:
            return f"@{uid}"
        return "viewer"

    def _record(self, kind: str, user: str, text: str) -> None:
        idx = self._next_index
        self._next_index += 1
        item = {
            "index": idx,
            "kind": kind,
            "user": user,
            "text": text,
            "timestamp": _now_iso(),
        }
        self._buffer.append(item)
        self._counts[kind] = self._counts.get(kind, 0) + 1
        self._last_event_at = time.time()

        try:
            from src.plugins import emit_event
            emit_event("tiktok_chat_event", item)
        except Exception:
            pass

    async def _run_client(self, client: Any) -> None:
        try:
            start = getattr(client, "start", None)
            if start is None:
                connect = getattr(client, "connect", None)
                if connect is None:
                    raise RuntimeError("TikTokLive client has neither .start() nor .connect()")
                res = connect()
                if asyncio.iscoroutine(res):
                    await res
                return
            res = start()
            if asyncio.iscoroutine(res):
                await res
            else:
                # Older builds returned a Task — await it.
                await res
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._connect_error = str(e)
            self._connected = False
            self._log.error(f"tiktok_chat client error: {e}")

    def _ensure_relay_loop(self) -> None:
        if self._relay_task is not None and not self._relay_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._relay_task = loop.create_task(self._relay_loop(), name="tiktok-chat-relay")

    async def _relay_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(max(2.0, float(self._relay_interval)))
                if self._relay_mode == "buffer":
                    return
                if not self._connected:
                    continue
                items = [it for it in self._buffer if it["index"] > self._last_relayed_index]
                if len(items) < self._relay_min_messages:
                    continue
                lines = [self._format_line(it) for it in items]
                self._last_relayed_index = items[-1]["index"]
                turn_complete = self._relay_mode == "live_reply"
                await self._inject_lines(lines, turn_complete=turn_complete)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self._log.error(f"tiktok_chat relay loop error: {e}", exc_info=True)

    def _format_line(self, item: dict[str, Any]) -> str:
        return f"{self._prefix} {item['user']}: {item['text']}"

    async def _inject_lines(self, lines: list[str], *, turn_complete: bool) -> None:
        live = getattr(self._handler, "live_session", None)
        if live is None:
            return
        text = "\n".join(lines)
        try:
            from google.genai import types
            turns = types.Content(parts=[types.Part(text=text)], role="user")
            await live.send_client_content_safe(turns, turn_complete=turn_complete)
        except Exception as e:
            self._log.debug(f"tiktok_chat inject failed: {e}")

    async def _cancel_task(self, attr: str) -> None:
        task = getattr(self, attr, None)
        if task is None:
            return
        if not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        setattr(self, attr, None)
