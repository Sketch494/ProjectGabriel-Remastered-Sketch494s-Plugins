"""Gemini Live tools for the YouTube playback plugin."""

from __future__ import annotations

import logging

from google.genai import types

from src.tools._base import BaseTool

logger = logging.getLogger(__name__)


_PLAY_DESC = (
    "Play audio from a YouTube video. The query can be a search string "
    "('lofi beats to study to'), a YouTube video URL, a youtu.be link, or a "
    "raw 11-character video ID. Stops the current track and starts the new "
    "one. The host audio output handles voice ducking automatically.\n"
    "**Invocation Condition:** Call when the user asks you to play something "
    "from YouTube, search for a song, or react to a specific YouTube video's "
    "audio."
)

_SEARCH_DESC = (
    "Search YouTube and return the top matches without playing anything. "
    "Useful for confirming the right video before calling `playYouTube`.\n"
    "**Invocation Condition:** Call when the user asks 'find me' or 'show me "
    "options for' a track on YouTube without committing to playback."
)

_QUEUE_DESC = (
    "Queue another query/URL/ID after the currently playing track. If nothing "
    "is playing, this just calls `playYouTube` directly."
)

_SKIP_DESC = (
    "Skip to the next queued track (or stop if the queue is empty)."
)

_STOP_DESC = (
    "Stop YouTube playback and clear the queue."
)

_PAUSE_DESC = (
    "Pause YouTube playback (decoder keeps running so resume is instant)."
)

_RESUME_DESC = (
    "Resume YouTube playback after a pause."
)

_VOLUME_DESC = (
    "Set YouTube playback volume. 0-100 is normal, up to 200 for boosted."
)

_STATUS_DESC = (
    "Return current track title, uploader, position, queue length, etc."
)

_CLEAR_QUEUE_DESC = (
    "Drop every queued track without stopping the currently playing one."
)

_WATCH_LC_DESC = (
    "Start reading YouTube live or replay chat for a watch URL or 11-char video ID. "
    "If videoIdOrUrl is omitted, uses the currently playing YouTube track. "
    "Requires `pip install chat-downloader`. By default (plugins.youtube."
    "live_chat_relay_mode) batched chat is injected so the model replies aloud "
    "(`live_reply`); use `setYouTubeLiveChatRelayMode` or config to change that. "
    "When `live_chat_save_memories` is enabled (default), viewer lines are also "
    "stored in Gabriel memory for later recall.\n"
    "**Invocation Condition:** Call when the user wants to follow chat on a "
    "stream or VOD, react to chat, or monitor superchats while audio plays."
)

_STOP_LC_DESC = (
    "Stop the YouTube live-chat reader, relay task, and periodic memory saver. Playback is unchanged."
)

_GET_LC_DESC = (
    "Return recent buffered chat lines (author, text, index). Use sinceIndex "
    "to poll only new messages after the last batch."
)

_CLEAR_LC_DESC = (
    "Clear the in-memory YouTube chat buffer and reset indices. Does not stop the reader."
)

_RELAY_LC_DESC = (
    "Control how chat reaches the model. `live_reply` (default): batched chat is injected "
    "with turn_complete so the model can reply out loud to viewers. `live_silent`: same "
    "batches as background context without ending your turn. `buffer`: no injection — "
    "only `getYouTubeLiveChatMessages` exposes chat. Optional intervalSeconds overrides "
    "plugins.youtube.live_chat_relay_interval_seconds."
)


class YouTubeTools(BaseTool):
    tool_key = "youtube"

    def declarations(self, config=None):
        if config is not None:
            if config.get("plugins", "youtube", "enabled", default=True) is False:
                return []
        return [
            types.FunctionDeclaration(
                name="playYouTube",
                description=_PLAY_DESC,
                parameters={
                    "type": "OBJECT",
                    "properties": {
                        "query": {
                            "type": "STRING",
                            "description": "Search query, YouTube URL, youtu.be link, or 11-char video ID.",
                        },
                        "autoSearch": {
                            "type": "BOOLEAN",
                            "description": "If true (default) and the input isn't a URL/ID, treat it as a search query.",
                        },
                    },
                    "required": ["query"],
                },
            ),
            types.FunctionDeclaration(
                name="searchYouTube",
                description=_SEARCH_DESC,
                parameters={
                    "type": "OBJECT",
                    "properties": {
                        "query": {"type": "STRING"},
                        "limit": {
                            "type": "INTEGER",
                            "description": "Max results (defaults to plugins.youtube.search_limit).",
                        },
                    },
                    "required": ["query"],
                },
            ),
            types.FunctionDeclaration(
                name="queueYouTube",
                description=_QUEUE_DESC,
                parameters={
                    "type": "OBJECT",
                    "properties": {
                        "query": {"type": "STRING"},
                    },
                    "required": ["query"],
                },
            ),
            types.FunctionDeclaration(
                name="skipYouTube",
                description=_SKIP_DESC,
                parameters={"type": "OBJECT", "properties": {}},
            ),
            types.FunctionDeclaration(
                name="stopYouTube",
                description=_STOP_DESC,
                parameters={"type": "OBJECT", "properties": {}},
            ),
            types.FunctionDeclaration(
                name="pauseYouTube",
                description=_PAUSE_DESC,
                parameters={"type": "OBJECT", "properties": {}},
            ),
            types.FunctionDeclaration(
                name="resumeYouTube",
                description=_RESUME_DESC,
                parameters={"type": "OBJECT", "properties": {}},
            ),
            types.FunctionDeclaration(
                name="setYouTubeVolume",
                description=_VOLUME_DESC,
                parameters={
                    "type": "OBJECT",
                    "properties": {
                        "volume": {
                            "type": "INTEGER",
                            "description": "0-200 (100 = unity).",
                        },
                    },
                    "required": ["volume"],
                },
            ),
            types.FunctionDeclaration(
                name="clearYouTubeQueue",
                description=_CLEAR_QUEUE_DESC,
                parameters={"type": "OBJECT", "properties": {}},
            ),
            types.FunctionDeclaration(
                name="getYouTubeStatus",
                description=_STATUS_DESC,
                parameters={"type": "OBJECT", "properties": {}},
            ),
            types.FunctionDeclaration(
                name="watchYouTubeLiveChat",
                description=_WATCH_LC_DESC,
                parameters={
                    "type": "OBJECT",
                    "properties": {
                        "videoIdOrUrl": {
                            "type": "STRING",
                            "description": "Optional watch URL, youtu.be link, or video ID; defaults to now-playing.",
                        },
                    },
                },
            ),
            types.FunctionDeclaration(
                name="stopYouTubeLiveChat",
                description=_STOP_LC_DESC,
                parameters={"type": "OBJECT", "properties": {}},
            ),
            types.FunctionDeclaration(
                name="getYouTubeLiveChatMessages",
                description=_GET_LC_DESC,
                parameters={
                    "type": "OBJECT",
                    "properties": {
                        "limit": {
                            "type": "INTEGER",
                            "description": "Max messages to return (most recent). Default 40.",
                        },
                        "sinceIndex": {
                            "type": "INTEGER",
                            "description": "Only messages with index greater than this.",
                        },
                    },
                },
            ),
            types.FunctionDeclaration(
                name="clearYouTubeLiveChat",
                description=_CLEAR_LC_DESC,
                parameters={"type": "OBJECT", "properties": {}},
            ),
            types.FunctionDeclaration(
                name="setYouTubeLiveChatRelayMode",
                description=_RELAY_LC_DESC,
                parameters={
                    "type": "OBJECT",
                    "properties": {
                        "mode": {
                            "type": "STRING",
                            "description": "One of: buffer, live_silent, live_reply.",
                        },
                        "intervalSeconds": {
                            "type": "NUMBER",
                            "description": "Seconds between relay batches when using live_silent/live_reply.",
                        },
                    },
                    "required": ["mode"],
                },
            ),
        ]

    @property
    def _mgr(self):
        return getattr(self.handler, "youtube", None)

    async def handle(self, name, args):
        known = {
            "playYouTube",
            "searchYouTube",
            "queueYouTube",
            "skipYouTube",
            "stopYouTube",
            "pauseYouTube",
            "resumeYouTube",
            "setYouTubeVolume",
            "clearYouTubeQueue",
            "getYouTubeStatus",
            "watchYouTubeLiveChat",
            "stopYouTubeLiveChat",
            "getYouTubeLiveChatMessages",
            "clearYouTubeLiveChat",
            "setYouTubeLiveChatRelayMode",
        }
        if name not in known:
            return None
        mgr = self._mgr
        if mgr is None:
            return {
                "result": "error",
                "message": "youtube plugin is not running (yt-dlp missing or startup failed).",
            }
        args = args or {}

        try:
            if name == "playYouTube":
                return await mgr.play(
                    str(args.get("query") or ""),
                    auto_search=bool(args.get("autoSearch", True)),
                )

            if name == "searchYouTube":
                limit = args.get("limit")
                results = await mgr.search(
                    str(args.get("query") or ""),
                    limit=int(limit) if limit else None,
                )
                return {"result": "ok", "results": results, "count": len(results)}

            if name == "queueYouTube":
                return await mgr.enqueue(str(args.get("query") or ""))

            if name == "skipYouTube":
                return await mgr.skip()

            if name == "stopYouTube":
                return await mgr.stop()

            if name == "pauseYouTube":
                return mgr.pause()

            if name == "resumeYouTube":
                return mgr.resume()

            if name == "setYouTubeVolume":
                return mgr.set_volume(int(args.get("volume") or 80))

            if name == "clearYouTubeQueue":
                return mgr.clear_queue()

            if name == "getYouTubeStatus":
                return {"result": "ok", **mgr.status_dict()}

            if name == "watchYouTubeLiveChat":
                v = args.get("videoIdOrUrl")
                raw = str(v).strip() if v is not None and str(v).strip() else None
                return await mgr.start_live_chat_watch(raw)

            if name == "stopYouTubeLiveChat":
                return await mgr.stop_live_chat_watch()

            if name == "getYouTubeLiveChatMessages":
                lim = args.get("limit")
                since = args.get("sinceIndex")
                items = mgr.get_live_chat_recent(
                    limit=int(lim) if lim is not None else 40,
                    since_index=int(since) if since is not None else None,
                )
                return {"result": "ok", "messages": items, "count": len(items)}

            if name == "clearYouTubeLiveChat":
                return mgr.clear_live_chat_buffer()

            if name == "setYouTubeLiveChatRelayMode":
                interval = args.get("intervalSeconds")
                return await mgr.set_live_chat_relay_mode(
                    str(args.get("mode") or ""),
                    interval_seconds=float(interval) if interval is not None else None,
                )
        except Exception as e:
            logger.error(f"youtube tool {name} failed: {e}", exc_info=True)
            return {"result": "error", "message": str(e)}

        return None
