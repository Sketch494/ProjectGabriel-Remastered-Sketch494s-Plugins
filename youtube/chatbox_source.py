"""YouTube chatbox now-playing source.

Implements the chatbox source protocol from the plugin API:
  * `is_active() -> bool`     — True while a YouTube track is playing.
  * `render() -> str | None`  — formatted "Now Playing" text, ≤144 chars to
                                fit VRChat's chatbox cap.

The format mirrors the Suno banner so a viewer who has both plugins running
gets a consistent look. We piggy-back on the host's idle-banner divider
config (`vrchat.idle_chatbox.divider`) when we need a horizontal rule.
"""

from __future__ import annotations

from typing import Any


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


class YouTubeChatboxSource:
    """Adapts a YouTubeManager into a chatbox source the host iterates over."""

    def __init__(self, manager_getter, config):
        self._get = manager_getter
        self._config = config

    def is_active(self) -> bool:
        mgr = self._get()
        return bool(mgr and getattr(mgr, "is_playing", False))

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
        return _format_now_playing(status, paused=bool(status.get("isPaused")))
