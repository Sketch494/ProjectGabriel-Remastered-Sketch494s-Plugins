"""YouTube playback plugin.

Adds tools that let the AI search YouTube and stream a video's audio into the
host's PyAudio output (the same path the Suno and local-music players use).

Architecture:
  * `YouTubeManager` (in `manager.py`) owns yt-dlp lookups, the play queue,
    and the active player. It also flips `audio.set_external_music_active(...)`
    so the existing voice-fade / vision-pause logic kicks in automatically.
  * `YouTubePlayer` (in `player.py`) handles ffmpeg → PyAudio for one stream.
  * `YouTubeTools` (in `tools.py`) exposes the function-call surface to Gemini.

Disabled by default in `plugin.yml`. Enable, `pip install yt-dlp`, restart.
"""

from __future__ import annotations

import logging

from src.plugins import Plugin, PluginContext

from .manager import YouTubeManager
from .tools import YouTubeTools

logger = logging.getLogger(__name__)


class YouTubePlugin(Plugin):
    name = "youtube"
    version = "1.0.0"
    description = (
        "Search YouTube and stream a video's audio through the host audio "
        "output. Tools: playYouTube, searchYouTube, queue/skip/clear, "
        "pause/resume/volume/status."
    )
    author = "Sketch494"

    def setup(self, ctx: PluginContext):
        ctx.register_tool(YouTubeTools)
        ctx.subscribe("startup", lambda: self._on_startup(ctx))
        ctx.subscribe("shutdown", lambda: self._on_shutdown(ctx))
        ctx.register_prompt_contributor("youtube", lambda: self._prompt_blurb(ctx))

        from .chatbox_source import YouTubeChatboxSource
        source = YouTubeChatboxSource(
            manager_getter=lambda: getattr(ctx.tool_handler, "youtube", None) if ctx.tool_handler else None,
            config=ctx.config,
            audio_getter=lambda: ctx.audio,
            plugin_cfg_getter=lambda: ctx.plugin_config() or {},
        )
        # Priority 30 sits between local music (10) and Suno (50) so YouTube
        # takes precedence over Suno but yields to a local file the user
        # explicitly started.
        ctx.register_chatbox_source("youtube", source, priority=30)

    def _on_startup(self, ctx: PluginContext):
        if ctx.tool_handler is None or ctx.audio is None:
            ctx.logger.warning("youtube startup: tool_handler or audio missing, cannot init")
            return
        try:
            cfg = ctx.plugin_config() or {}
            mgr = YouTubeManager(cfg, ctx.audio, ctx.logger)
            ctx.tool_handler.youtube = mgr
            ctx.logger.info("youtube plugin ready")
        except Exception as e:
            ctx.logger.error(f"youtube init failed: {e}", exc_info=True)

    async def _on_shutdown(self, ctx: PluginContext):
        mgr = getattr(ctx.tool_handler, "youtube", None) if ctx.tool_handler else None
        if mgr is not None:
            try:
                await mgr.stop_all()
            except Exception as e:
                ctx.logger.error(f"youtube shutdown failed: {e}")
            try:
                ctx.tool_handler.youtube = None
            except Exception:
                pass
        ctx.logger.info("youtube plugin shut down")

    async def teardown(self, ctx: PluginContext):
        await self._on_shutdown(ctx)

    @staticmethod
    def _prompt_blurb(ctx: PluginContext) -> str | None:
        mgr = getattr(ctx.tool_handler, "youtube", None) if ctx.tool_handler else None
        if mgr is None:
            return None
        if not mgr.is_playing:
            return (
                "YouTube playback available: call `playYouTube` with a search query, "
                "URL, or video ID to stream a video's audio through your voice output. "
                "It ducks your voice automatically while the song plays."
            )
        st = mgr.status_dict()
        return (
            f"YouTube playing: \"{st.get('title')}\" by {st.get('uploader')}. "
            f"Queue length: {st.get('queueLength')}. Use `skipYouTube`, `pauseYouTube`, "
            f"`stopYouTube`, or `setYouTubeVolume` to control playback."
        )


plugin = YouTubePlugin
