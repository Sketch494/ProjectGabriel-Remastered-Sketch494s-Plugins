"""YouTube playback manager.

Wraps yt-dlp for search + URL resolution and owns the active player + queue.
All blocking yt-dlp calls run inside ``asyncio.to_thread``.

Public surface (used by ``YouTubeTools``):

    * ``await play(query, autoSearch=True)`` — resolve and play immediately.
    * ``await enqueue(query)`` — append a track to the queue.
    * ``await search(query, limit=5)`` — return search results without playing.
    * ``stop()`` / ``skip()`` / ``pause()`` / ``resume()`` / ``set_volume(...)``
    * ``status_dict()`` — for getYouTubeStatus.

The manager flips ``audio.set_external_music_active`` mirroring the existing
Suno/local-music behavior so the AI's voice ducks while music plays and the
host's various idle/vision pause heuristics behave as expected.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections import deque
from dataclasses import asdict
from typing import Any

try:
    from yt_dlp import YoutubeDL  # type: ignore[import-not-found]
except Exception:  # pragma: no cover - optional dep
    YoutubeDL = None  # type: ignore[assignment]

from .player import YouTubePlayer, YouTubeTrack

logger = logging.getLogger(__name__)


_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_URL_HINT_RE = re.compile(r"^(https?://|www\.|youtu\.be/|youtube\.com/)", re.IGNORECASE)


def _looks_like_video_id(s: str) -> bool:
    return bool(_VIDEO_ID_RE.match(s.strip()))


def _looks_like_url(s: str) -> bool:
    return bool(_URL_HINT_RE.match(s.strip()))


def _normalize_target(query: str) -> str:
    """Turn a free-text query / URL / video ID into something yt-dlp can resolve.

    Free text gets the ``ytsearch1:`` prefix so yt-dlp routes it through the
    YouTube search extractor instead of trying its generic URL extractor and
    erroring with "is not a valid URL".
    """
    q = (query or "").strip()
    if not q:
        return q
    if _looks_like_url(q):
        return q
    if _looks_like_video_id(q):
        return f"https://www.youtube.com/watch?v={q}"
    return f"ytsearch1:{q}"


class YouTubeManager:
    def __init__(self, cfg: dict[str, Any], audio_mgr: Any, log: logging.Logger):
        self._cfg = cfg or {}
        self._audio = audio_mgr
        self._log = log
        self._volume = int(self._cfg.get("default_volume", 80))
        self._search_limit = int(self._cfg.get("search_limit", 5))
        self._max_duration = int(self._cfg.get("max_duration_seconds", 1800) or 0)
        self._block_livestreams = bool(self._cfg.get("block_livestreams", False))
        self._cookies_file = str(self._cfg.get("cookies_file") or "").strip() or None
        self._format_str = str(self._cfg.get("ytdlp_format") or "bestaudio/best")
        self._geo_bypass = bool(self._cfg.get("geo_bypass", True))
        self._resolve_timeout = float(self._cfg.get("resolve_timeout_seconds", 120) or 120)
        self._search_timeout = float(self._cfg.get("search_timeout_seconds", 90) or 90)
        self._save_while_playing = bool(self._cfg.get("save_while_playing", True))
        self._save_dir = str(self._cfg.get("save_dir") or "sfx/youtube").strip() or "sfx/youtube"
        self._last_saved_path: str | None = None

        self._queue: deque[YouTubeTrack] = deque()
        self._history: deque[dict[str, Any]] = deque(maxlen=int(self._cfg.get("history_size", 50)))
        self._player: YouTubePlayer | None = None
        self._player_lock = asyncio.Lock()
        self._auto_advance_task: asyncio.Task | None = None

    # ---- properties ------------------------------------------------------

    @property
    def is_available(self) -> bool:
        return YoutubeDL is not None

    @property
    def is_playing(self) -> bool:
        p = self._player
        return p is not None and not p.state.finished

    # ---- ytdlp helpers ---------------------------------------------------

    def _ydl_opts(self, *, search: bool = False) -> dict[str, Any]:
        opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "skip_download": True,
            "format": self._format_str,
            "extract_flat": False,
            "geo_bypass": self._geo_bypass,
        }
        if self._cookies_file:
            opts["cookiefile"] = self._cookies_file
        if search:
            opts["extract_flat"] = "in_playlist"
        return opts

    def _download_track_blocking(self, track: YouTubeTrack) -> str | None:
        """Save audio under ``save_dir`` as ``<video_id>.<ext>``; skip if already present."""
        import glob

        if YoutubeDL is None:
            return None
        os.makedirs(self._save_dir, exist_ok=True)
        pattern = os.path.join(self._save_dir, f"{track.video_id}.*")
        existing = glob.glob(pattern)
        if existing:
            return existing[0]
        outtmpl = os.path.join(self._save_dir, f"{track.video_id}.%(ext)s")
        opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "format": self._format_str,
            "outtmpl": outtmpl,
            "geo_bypass": self._geo_bypass,
        }
        if self._cookies_file:
            opts["cookiefile"] = self._cookies_file
        url = track.webpage_url or f"https://www.youtube.com/watch?v={track.video_id}"
        with YoutubeDL(opts) as ydl:
            ydl.download([url])
        after = glob.glob(pattern)
        return after[0] if after else None

    def _schedule_save(self, track: YouTubeTrack) -> None:
        if not self._save_while_playing or not track.video_id:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        async def _job() -> None:
            try:
                path = await asyncio.to_thread(self._download_track_blocking, track)
                if path:
                    self._last_saved_path = path
                    self._log.info(f"youtube: saved track {track.video_id} -> {path}")
            except Exception as e:
                self._log.warning(f"youtube: background save failed ({track.video_id}): {e}")

        loop.create_task(_job(), name=f"yt-save-{track.video_id}")

    def _info_to_track(self, info: dict[str, Any]) -> YouTubeTrack | None:
        if not isinstance(info, dict):
            return None
        # search results often nest under "entries"
        if "entries" in info and isinstance(info["entries"], list):
            return None
        url = info.get("url") or ""
        if not url:
            # Some flat responses lack a direct url; resolve it explicitly.
            return None
        vid = str(info.get("id") or info.get("display_id") or "")
        wp = str(info.get("webpage_url") or "")
        if vid and not wp:
            wp = f"https://www.youtube.com/watch?v={vid}"
        return YouTubeTrack(
            video_id=vid,
            title=str(info.get("title") or "Unknown title"),
            uploader=str(info.get("uploader") or info.get("channel") or "Unknown"),
            duration=float(info.get("duration") or 0.0),
            stream_url=str(url),
            webpage_url=wp,
            thumbnail=str(info.get("thumbnail") or ""),
        )

    def _check_constraints(self, info: dict[str, Any]) -> str | None:
        if self._block_livestreams and info.get("is_live"):
            return "livestreams are blocked by config"
        dur = float(info.get("duration") or 0.0)
        if self._max_duration and dur > self._max_duration:
            return f"video too long ({dur:.0f}s > max {self._max_duration}s)"
        return None

    def _resolve_blocking(self, target: str) -> dict[str, Any]:
        if YoutubeDL is None:
            raise RuntimeError("yt-dlp is not installed (pip install yt-dlp)")
        with YoutubeDL(self._ydl_opts()) as ydl:
            info = ydl.extract_info(target, download=False)
        return info or {}

    def _search_blocking(self, query: str, limit: int) -> list[dict[str, Any]]:
        if YoutubeDL is None:
            raise RuntimeError("yt-dlp is not installed (pip install yt-dlp)")
        target = f"ytsearch{max(1, int(limit))}:{query}"
        opts = self._ydl_opts(search=True)
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(target, download=False) or {}
        entries = info.get("entries") or []
        rows: list[dict[str, Any]] = []
        for e in entries:
            if not isinstance(e, dict):
                continue
            rows.append({
                "id": e.get("id"),
                "title": e.get("title"),
                "uploader": e.get("uploader") or e.get("channel"),
                "duration": e.get("duration"),
                "url": e.get("webpage_url") or (f"https://www.youtube.com/watch?v={e['id']}" if e.get("id") else None),
            })
        return rows

    async def _resolve(self, target: str) -> YouTubeTrack:
        info = await asyncio.to_thread(self._resolve_blocking, target)
        # Some resolves return a search list when the input wasn't URL/ID
        if isinstance(info, dict) and info.get("entries"):
            entries = info["entries"]
            if not entries:
                raise RuntimeError("no results")
            info = entries[0]
            # the entry may be flat (no stream URL); re-resolve by id
            if not info.get("url") and info.get("id"):
                info = await asyncio.to_thread(self._resolve_blocking, f"https://www.youtube.com/watch?v={info['id']}")
        problem = self._check_constraints(info)
        if problem:
            raise RuntimeError(problem)
        track = self._info_to_track(info)
        if track is None:
            raise RuntimeError("could not resolve a playable stream URL")
        return track

    # ---- public surface --------------------------------------------------

    async def search(self, query: str, limit: int | None = None) -> list[dict[str, Any]]:
        if not self.is_available:
            return []
        n = int(limit if limit and limit > 0 else self._search_limit)
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._search_blocking, query, n),
                timeout=max(5.0, self._search_timeout),
            )
        except asyncio.TimeoutError:
            self._log.warning(f"yt search timed out after {self._search_timeout:.0f}s")
            return []
        except Exception as e:
            self._log.warning(f"yt search failed: {e}")
            return []
        if not self.is_available:
            return {"result": "error", "message": "yt-dlp is not installed (pip install yt-dlp)"}
        q = (query or "").strip()
        if not q:
            return {"result": "error", "message": "query/URL is required"}

        is_url_or_id = _looks_like_url(q) or _looks_like_video_id(q)
        if not is_url_or_id and not auto_search:
            return {"result": "error", "message": "input is not a URL/ID and auto_search is disabled"}

        try:
            track = await asyncio.wait_for(
                self._resolve(_normalize_target(q)),
                timeout=max(15.0, self._resolve_timeout),
            )
        except asyncio.TimeoutError:
            return {
                "result": "error",
                "message": (
                    f"yt-dlp resolve timed out after {self._resolve_timeout:.0f}s — "
                    "try again or increase plugins.youtube.resolve_timeout_seconds in config."
                ),
            }
        except Exception as e:
            return {"result": "error", "message": str(e)}

        async with self._player_lock:
            await self._stop_internal()
            self._player = YouTubePlayer(track, self._audio, volume=self._volume)
            self._player.set_on_finished(self._on_player_finished)
            self._audio.set_external_music_active(True)
            ok = self._player.start()
            if not ok:
                self._audio.set_external_music_active(False)
                err = self._player.state.error or "failed to start playback"
                self._player = None
                return {"result": "error", "message": err}
            self._history.appendleft({
                "track": asdict(track),
                "started_at": time.time(),
            })
        self._schedule_save(track)
        return {"result": "ok", "track": self._track_dict(track), "queueLength": len(self._queue)}

    async def enqueue(self, query: str) -> dict[str, Any]:
        if not self.is_available:
            return {"result": "error", "message": "yt-dlp is not installed (pip install yt-dlp)"}
        if not self.is_playing:
            return await self.play(query)
        q = (query or "").strip()
        if not q:
            return {"result": "error", "message": "query/URL is required"}
        try:
            track = await asyncio.wait_for(
                self._resolve(_normalize_target(q)),
                timeout=max(15.0, self._resolve_timeout),
            )
        except asyncio.TimeoutError:
            return {
                "result": "error",
                "message": (
                    f"yt-dlp resolve timed out after {self._resolve_timeout:.0f}s — "
                    "try again or increase plugins.youtube.resolve_timeout_seconds."
                ),
            }
        except Exception as e:
            return {"result": "error", "message": str(e)}
        self._queue.append(track)
        return {
            "result": "ok",
            "queued": self._track_dict(track),
            "queueLength": len(self._queue),
        }

    async def stop(self) -> dict[str, Any]:
        async with self._player_lock:
            await self._stop_internal()
            self._queue.clear()
        return {"result": "ok", "stopped": True}

    async def skip(self) -> dict[str, Any]:
        try:
            async with self._player_lock:
                await self._stop_internal()
            if self._queue:
                track = self._queue.popleft()
                self._player = YouTubePlayer(track, self._audio, volume=self._volume)
                self._player.set_on_finished(self._on_player_finished)
                self._audio.set_external_music_active(True)
                if not self._player.start():
                    self._audio.set_external_music_active(False)
                    err = self._player.state.error or "failed to start next track"
                    self._player = None
                    return {"result": "error", "message": err}
                self._schedule_save(track)
                return {"result": "ok", "now": self._track_dict(track), "queueLength": len(self._queue)}
            return {"result": "ok", "now": None, "queueLength": 0}
        except Exception as e:
            self._log.error(f"youtube skip failed: {e}", exc_info=True)
            try:
                self._audio.set_external_music_active(False)
            except Exception:
                pass
            return {"result": "error", "message": str(e)}

    def pause(self) -> dict[str, Any]:
        if self._player is None or not self.is_playing:
            return {"result": "error", "message": "nothing is playing"}
        self._player.pause()
        return {"result": "ok", "paused": True}

    def resume(self) -> dict[str, Any]:
        if self._player is None:
            return {"result": "error", "message": "nothing to resume"}
        self._player.resume()
        return {"result": "ok", "paused": False}

    def set_volume(self, volume_pct: int) -> dict[str, Any]:
        v = max(0, min(200, int(volume_pct)))
        self._volume = v
        if self._player is not None:
            self._player.set_volume(v)
        return {"result": "ok", "volume": v}

    def clear_queue(self) -> dict[str, Any]:
        n = len(self._queue)
        self._queue.clear()
        return {"result": "ok", "cleared": n}

    def status_dict(self) -> dict[str, Any]:
        track = self._player.state.track if self._player else None
        is_paused = bool(self._player and self._player.is_paused)
        return {
            "available": self.is_available,
            "isPlaying": self.is_playing,
            "isPaused": is_paused,
            "title": getattr(track, "title", None),
            "uploader": getattr(track, "uploader", None),
            "videoId": getattr(track, "video_id", None),
            "duration": getattr(track, "duration", None),
            "position": self._player.position_seconds() if self._player else 0.0,
            "volume": self._volume,
            "queueLength": len(self._queue),
            "queue": [self._track_dict(t) for t in list(self._queue)[:10]],
            "historyCount": len(self._history),
            "saveWhilePlaying": self._save_while_playing,
            "saveDir": self._save_dir,
            "lastSavedPath": self._last_saved_path,
        }

    async def stop_all(self) -> None:
        await self.stop()

    # ---- internals -------------------------------------------------------

    async def _stop_internal(self) -> None:
        p = self._player
        self._player = None
        if p is not None:
            try:
                p.stop()
            except Exception:
                pass
        try:
            self._audio.set_external_music_active(False)
        except Exception:
            pass

    def _on_player_finished(self, _player: YouTubePlayer) -> None:
        # Called from the player's pump thread. Hop back to the host loop.
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            return
        try:
            loop.call_soon_threadsafe(lambda: self._schedule_next())
        except Exception:
            pass

    def _schedule_next(self) -> None:
        if self._auto_advance_task and not self._auto_advance_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._auto_advance_task = loop.create_task(self._advance_queue())

    async def _advance_queue(self) -> None:
        async with self._player_lock:
            # Don't trample a manually-started new track
            if self._player is not None and not self._player.state.finished:
                return
            try:
                self._audio.set_external_music_active(False)
            except Exception:
                pass
            if not self._queue:
                self._player = None
                return
            track = self._queue.popleft()
            self._player = YouTubePlayer(track, self._audio, volume=self._volume)
            self._player.set_on_finished(self._on_player_finished)
            self._audio.set_external_music_active(True)
            if not self._player.start():
                self._audio.set_external_music_active(False)
                self._player = None
                return
            self._schedule_save(track)

    @staticmethod
    def _track_dict(track: YouTubeTrack) -> dict[str, Any]:
        return {
            "videoId": track.video_id,
            "title": track.title,
            "uploader": track.uploader,
            "duration": track.duration,
            "url": track.webpage_url,
            "thumbnail": track.thumbnail,
        }
