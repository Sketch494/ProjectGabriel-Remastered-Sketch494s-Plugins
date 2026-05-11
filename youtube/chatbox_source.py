"""YouTube chatbox now-playing source.

Implements the chatbox source protocol from the plugin API:
  * `is_active() -> bool`     — True while a YouTube track is playing and the
                                host is not showing local pygame music (SFX).
  * `render() -> str | None`  — formatted text, ≤144 chars (VRChat cap).

Template: set ``chatbox_template`` under ``plugins.youtube`` in ``config.yml``.
Placeholders: ``{header}``, ``{title}``, ``{uploader}``, ``{bar}``, ``{time}``,
``{queue_suffix}``, ``{video_id}``. Omit ``chatbox_template`` to use the
built-in multiline layout.
"""

from __future__ import annotations


_BAR_WIDTH = 14


def _bar(progress: float) -> str:
    progress = max(0.0, min(1.0, float(progress)))
    exact = progress * _BAR_WIDTH
    filled = int(exact)
    fraction = exact - filled
    if filled >= _BAR_WIDTH:
        return "\u2588" * _BAR_WIDTH
    if fraction < 0.25:
        transition = "\u2591"
    elif fraction < 0.5:
        transition = "\u2592"
    elif fraction < 0.75:
        transition = "\u2593"
    else:
        transition = "\u2588"
    return "\u2588" * filled + transition + "\u2591" * (_BAR_WIDTH - filled - 1)


def _format_now_playing(status: dict[str, Any], paused: bool) -> str:
    title = (status.get("title") or "YouTube Video").strip()
    uploader = (status.get("uploader") or "").strip()
    pos = float(status.get("position") or 0.0)
    dur = float(status.get("duration") or 0.0)
    queue_len = int(status.get("queueLength") or 0)

    pos_m, pos_s = divmod(int(pos), 60)
    if dur > 0:
        dur_m, dur_s = divmod(int(dur), 60)
        time_str = f"{pos_m}:{pos_s:02d} / {dur_m}:{dur_s:02d}"
    else:
        time_str = f"{pos_m}:{pos_s:02d}"

    progress = (pos / dur) if dur > 0 else 0.0
    bar = _bar(progress)

    header = "\u25b6 YOUTUBE"
    if paused:
        header = "\u23f8 YOUTUBE (paused)"

    name = title
    max_name = 90
    if uploader:
        max_name = 60
    if len(name) > max_name:
        name = name[:max_name - 3] + "..."

    lines = [header, name]
    if uploader:
        u = uploader if len(uploader) <= 36 else uploader[:33] + "..."
        lines.append(f"by {u}")
    lines.append(bar)
    suffix = f"  +{queue_len} queued" if queue_len > 0 else ""
    lines.append(time_str + suffix)
    text = "\n".join(lines)
    if len(text) > 144:
        text = text[:144]
    return text


def _render_template(template: str, status: dict[str, Any], paused: bool) -> str:
    title = (status.get("title") or "YouTube Video").strip()
    uploader = (status.get("uploader") or "").strip()
    pos = float(status.get("position") or 0.0)
    dur = float(status.get("duration") or 0.0)
    queue_len = int(status.get("queueLength") or 0)
    pos_m, pos_s = divmod(int(pos), 60)
    if dur > 0:
        dur_m, dur_s = divmod(int(dur), 60)
        time_line = f"{pos_m}:{pos_s:02d} / {dur_m}:{dur_s:02d}"
    else:
        time_line = f"{pos_m}:{pos_s:02d}"
    progress = (pos / dur) if dur > 0 else 0.0
    bar = _bar(progress)
    header = "\u23f8 YT" if paused else "\u25b6 YT"
    queue_suffix = f" +{queue_len}" if queue_len > 0 else ""
    vid = str(status.get("videoId") or "")
    try:
        text = template.format(
            header=header,
            title=title,
            uploader=uploader,
            bar=bar,
            time=time_line,
            queue_suffix=queue_suffix,
            video_id=vid,
        )
    except (KeyError, ValueError, IndexError):
        text = _format_now_playing(status, paused)
    text = text.strip()
    if len(text) > 144:
        text = text[:141] + "..."
    return text


class YouTubeChatboxSource:
    """Adapts a YouTubeManager into a chatbox source the host iterates over."""

    def __init__(
        self,
        manager_getter: Callable[[], Any],
        config: Any,
        *,
        audio_getter: Callable[[], Any] | None = None,
        plugin_cfg_getter: Callable[[], dict[str, Any]] | None = None,
    ):
        self._get = manager_getter
        self._config = config
        self._audio_get = audio_getter
        self._plugin_cfg = plugin_cfg_getter or (lambda: {})

    def _local_pygame_music_busy(self) -> bool:
        """True when SFX / playMusic is driving pygame (chatbox belongs to host)."""
        audio = self._audio_get() if self._audio_get else None
        if audio is None:
            return False
        try:
            if not getattr(audio, "_pygame_ready", False):
                return False
            import pygame

            return bool(pygame.mixer.music.get_busy() or pygame.mixer.get_busy())
        except Exception:
            return False

    def _chatbox_cfg(self) -> dict[str, Any]:
        try:
            d = self._plugin_cfg()
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def is_active(self) -> bool:
        cfg = self._chatbox_cfg()
        if cfg.get("chatbox_enabled") is False:
            return False
        mgr = self._get()
        if not mgr or not getattr(mgr, "is_playing", False):
            return False
        if self._local_pygame_music_busy():
            return False
        return True

    def render(self):
        mgr = self._get()
        if mgr is None:
            return None
        try:
            status = mgr.status_dict()
        except Exception:
            return None
        if not status or not status.get("isPlaying"):
            return None
        paused = bool(status.get("isPaused"))
        cfg = self._chatbox_cfg()
        raw = cfg.get("chatbox_template")
        if isinstance(raw, str) and raw.strip():
            return _render_template(raw, status, paused)
        return _format_now_playing(status, paused)
