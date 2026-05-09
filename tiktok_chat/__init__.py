"""TikTok Live chat plugin.

Hooks `TikTokLive` into the Gabriel runtime so the AI can read (and optionally
react to) a TikTok Live stream's chat.

Architecture:
  * `TikTokChatClient` (in `client.py`) owns the TikTokLive connection, a ring
    buffer of recent events (comments / gifts / follows / likes), and a relay
    loop that periodically batches new lines into the Gemini Live session.
  * `TikTokChatTools` exposes connect/disconnect/status/getRecent/setRelay
    tools to the AI.
  * The plugin attaches the client as `tool_handler.tiktok_chat` so tools can
    dispatch through the standard handler path.

The plugin is **disabled by default** because it depends on the third-party
`TikTokLive` package, which scrapes TikTok's webcast endpoints and may need a
sign-server API key (Eulerstream, SignAPI, etc.) to stay reliable.
"""

from __future__ import annotations

import logging

from src.plugins import Plugin, PluginContext

from .client import TikTokChatClient
from .tools import TikTokChatTools

logger = logging.getLogger(__name__)


class TikTokChatPlugin(Plugin):
    name = "tiktok_chat"
    version = "1.0.0"
    description = (
        "Connect to a TikTok Live stream and let the AI read chat. "
        "Tools: connectTikTokChat / disconnectTikTokChat / getRecentTikTokChat / "
        "getTikTokChatStatus / setTikTokRelayMode / clearTikTokChatBuffer."
    )
    author = "Sketch494"

    def setup(self, ctx: PluginContext):
        ctx.register_tool(TikTokChatTools)
        ctx.subscribe("startup", lambda: self._on_startup(ctx))
        ctx.subscribe("shutdown", lambda: self._on_shutdown(ctx))
        ctx.register_prompt_contributor("tiktok_chat", lambda: self._prompt_blurb(ctx))

    def _on_startup(self, ctx: PluginContext):
        if ctx.tool_handler is None:
            ctx.logger.warning("tiktok_chat startup: tool_handler missing, cannot init")
            return
        cfg = ctx.plugin_config() or {}
        try:
            client = TikTokChatClient(cfg, ctx.tool_handler, ctx.logger)
            ctx.tool_handler.tiktok_chat = client
        except Exception as e:
            ctx.logger.error(f"tiktok_chat init failed: {e}", exc_info=True)
            return

        username = (cfg.get("default_username") or "").strip()
        auto = bool(cfg.get("auto_connect"))
        if username and auto:
            ctx.logger.info(f"tiktok_chat: auto-connecting to @{username.lstrip('@')}")
            try:
                client.schedule_connect(username)
            except Exception as e:
                ctx.logger.warning(f"tiktok_chat: auto-connect failed to schedule: {e}")

    async def _on_shutdown(self, ctx: PluginContext):
        client = getattr(ctx.tool_handler, "tiktok_chat", None) if ctx.tool_handler else None
        if client is not None:
            try:
                await client.stop()
            except Exception as e:
                ctx.logger.error(f"tiktok_chat shutdown stop() failed: {e}")
            try:
                ctx.tool_handler.tiktok_chat = None
            except Exception:
                pass
        ctx.logger.info("tiktok_chat plugin shut down")

    async def teardown(self, ctx: PluginContext):
        await self._on_shutdown(ctx)

    @staticmethod
    def _prompt_blurb(ctx: PluginContext) -> str | None:
        client = getattr(ctx.tool_handler, "tiktok_chat", None) if ctx.tool_handler else None
        if client is None:
            return None
        if not client.is_connected:
            return (
                "TikTok Live chat available but not connected. Call `connectTikTokChat` with a "
                "username when the user asks you to read or react to their TikTok live."
            )
        mode = client.relay_mode
        base = (
            f"TikTok Live chat: connected to @{client.username}. Relay mode = {mode}. "
            f"Lines are tagged with the `[TikTok]` prefix and look like "
            f"`[TikTok] viewer_name: their comment`. Treat each as a real viewer in the "
            f"stream chat — they are NOT the operator who is in the room with you."
        )
        if mode == "live_reply":
            base += (
                " The host injects new chat for you in batches and ends the turn, which is "
                "your cue to actually respond to chat out loud. Acknowledge viewers by name "
                "when it makes sense, keep replies short and conversational, and don't "
                "repeat every line — pick the highlights or the most recent comment. If "
                "chat is just spamming emoji or 'hi', you can skip a beat instead of "
                "responding to every batch."
            )
        elif mode == "live_silent":
            base += (
                " The host injects new chat for you as background context but does NOT "
                "force a reply — only respond when the operator references chat or when a "
                "comment is clearly directed at you. Otherwise stay focused on whatever "
                "you're already doing."
            )
        else:  # buffer
            base += (
                " The host is silent — chat is buffered but you only see it when you "
                "call `getRecentTikTokChat`. Pull it whenever the operator asks 'what's "
                "chat saying' or similar."
            )
        return base


plugin = TikTokChatPlugin
