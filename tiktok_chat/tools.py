"""Gemini Live tools for the TikTok chat plugin."""

from __future__ import annotations

import logging
from typing import Any

from google.genai import types

from src.tools._base import BaseTool

logger = logging.getLogger(__name__)


_CONNECT_DESC = (
    "Connect to a TikTok Live stream and start watching its chat. The username "
    "is the @handle (with or without the leading @). Once connected, comments, "
    "gifts, and follows are buffered and surfaced via `getRecentTikTokChat`, "
    "and — depending on the relay mode — may also be auto-injected into the "
    "live conversation as viewer context.\n"
    "**Invocation Condition:** Call when the user asks you to read, react to, "
    "or follow along with their TikTok live chat."
)

_DISCONNECT_DESC = (
    "Disconnect from the current TikTok Live stream and stop the relay loop.\n"
    "**Invocation Condition:** Call when the user is done streaming, has gone "
    "offline, or asks you to stop watching chat."
)

_GET_RECENT_DESC = (
    "Return the most recent buffered TikTok Live events (comments, gifts, "
    "follows). Use `sinceIndex` to fetch only new events past a previous "
    "snapshot. Use `kinds` to filter (e.g. ['comment'] for comments only)."
)

_STATUS_DESC = (
    "Return whether the TikTok client is connected, the username, the relay "
    "mode, buffer fill level, lifetime event counts, and seconds connected."
)

_SET_RELAY_DESC = (
    "Switch how chat flows into the live session.\n"
    "  * `buffer`      — stay silent; the AI only sees chat when it calls "
    "`getRecentTikTokChat`.\n"
    "  * `live_silent` — periodically inject new chat as background context "
    "(no auto-reply trigger).\n"
    "  * `live_reply`  — same batching, but each batch ends a turn so the AI "
    "is prompted to actually reply to chat.\n"
    "**Invocation Condition:** Use when the user wants the AI to be more or "
    "less reactive to chat, or to run silently while still seeing messages."
)

_CLEAR_DESC = (
    "Wipe the local TikTok chat ring buffer. Does not disconnect — new events "
    "will keep being recorded after this call.\n"
    "**Invocation Condition:** Use to start a clean log when switching topics "
    "or to drop spammy backlog before reading."
)


class TikTokChatTools(BaseTool):
    tool_key = "tiktok_chat"

    def declarations(self, config=None):
        if config is not None:
            if config.get("plugins", "tiktok_chat", "enabled", default=True) is False:
                return []
        return [
            types.FunctionDeclaration(
                name="connectTikTokChat",
                description=_CONNECT_DESC,
                parameters={
                    "type": "OBJECT",
                    "properties": {
                        "username": {
                            "type": "STRING",
                            "description": "TikTok @handle to watch (with or without leading @).",
                        },
                    },
                    "required": ["username"],
                },
            ),
            types.FunctionDeclaration(
                name="disconnectTikTokChat",
                description=_DISCONNECT_DESC,
                parameters={"type": "OBJECT", "properties": {}},
            ),
            types.FunctionDeclaration(
                name="getRecentTikTokChat",
                description=_GET_RECENT_DESC,
                parameters={
                    "type": "OBJECT",
                    "properties": {
                        "limit": {
                            "type": "INTEGER",
                            "description": "Maximum events to return (most recent first). Default 50.",
                        },
                        "sinceIndex": {
                            "type": "INTEGER",
                            "description": "Only return events with index > this. Use the highest index from a prior call to paginate.",
                        },
                        "kinds": {
                            "type": "ARRAY",
                            "items": {"type": "STRING"},
                            "description": "Filter by event kind: comment, gift, follow, like, share. Omit for all.",
                        },
                    },
                },
            ),
            types.FunctionDeclaration(
                name="getTikTokChatStatus",
                description=_STATUS_DESC,
                parameters={"type": "OBJECT", "properties": {}},
            ),
            types.FunctionDeclaration(
                name="setTikTokRelayMode",
                description=_SET_RELAY_DESC,
                parameters={
                    "type": "OBJECT",
                    "properties": {
                        "mode": {
                            "type": "STRING",
                            "description": "buffer / live_silent / live_reply",
                        },
                        "intervalSeconds": {
                            "type": "NUMBER",
                            "description": "Seconds between live-mode flush batches (>= 2).",
                        },
                    },
                    "required": ["mode"],
                },
            ),
            types.FunctionDeclaration(
                name="clearTikTokChatBuffer",
                description=_CLEAR_DESC,
                parameters={"type": "OBJECT", "properties": {}},
            ),
        ]

    @property
    def _client(self):
        return getattr(self.handler, "tiktok_chat", None)

    async def handle(self, name, args):
        client = self._client
        known = {
            "connectTikTokChat",
            "disconnectTikTokChat",
            "getRecentTikTokChat",
            "getTikTokChatStatus",
            "setTikTokRelayMode",
            "clearTikTokChatBuffer",
        }
        if name not in known:
            return None
        if client is None:
            return {
                "result": "error",
                "message": "tiktok_chat plugin is not running (TikTokLive missing or startup failed).",
            }
        args = args or {}

        if name == "connectTikTokChat":
            return await client.connect(str(args.get("username") or ""))

        if name == "disconnectTikTokChat":
            return await client.disconnect()

        if name == "getRecentTikTokChat":
            kinds = args.get("kinds")
            kinds_list: list[str] | None
            if isinstance(kinds, list):
                kinds_list = [str(k) for k in kinds]
            else:
                kinds_list = None
            items = client.get_recent(
                limit=int(args.get("limit") or 50),
                since_index=(int(args["sinceIndex"]) if args.get("sinceIndex") is not None else None),
                kinds=kinds_list,
            )
            return {
                "result": "ok",
                "items": items,
                "count": len(items),
                "highestIndex": items[-1]["index"] if items else None,
                "status": client.status(),
            }

        if name == "getTikTokChatStatus":
            return {"result": "ok", **client.status()}

        if name == "setTikTokRelayMode":
            return await client.set_relay_mode(
                str(args.get("mode") or "").lower(),
                interval_seconds=args.get("intervalSeconds"),
            )

        if name == "clearTikTokChatBuffer":
            cleared: int = client.clear_buffer()
            return {"result": "ok", "cleared": cleared}

        return None
