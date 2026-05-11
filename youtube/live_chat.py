"""YouTube live / replay chat ingestion via ``chat-downloader`` (optional).

Runs in a daemon thread and pushes normalized messages into a queue consumed
by :class:`YouTubeManager`'s asyncio pump task.
"""

from __future__ import annotations

import queue
import threading
from typing import Any


def chat_downloader_available() -> bool:
    try:
        import chat_downloader  # noqa: F401
        return True
    except ImportError:
        return False


def _normalize_message(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Map chat-downloader payloads to ``author`` / ``text``."""
    if not isinstance(raw, dict):
        return None
    author_obj = raw.get("author")
    if isinstance(author_obj, dict):
        name = (
            author_obj.get("name")
            or author_obj.get("display_name")
            or author_obj.get("id")
            or "?"
        )
    elif author_obj is not None:
        name = str(author_obj)
    else:
        name = "?"
    text = raw.get("message") or raw.get("message_text") or raw.get("text") or ""
    text = str(text).strip()
    if not text:
        return None
    return {
        "author": str(name),
        "text": text,
        "timeText": str(raw.get("time_text") or raw.get("time_in_seconds") or ""),
    }


class YouTubeLiveChatWorker:
    """Streams chat messages from a watch URL into ``drain()``."""

    def __init__(self, watch_url: str, log: Any):
        self._url = watch_url
        self._log = log
        self._q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=2000)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="youtube-live-chat", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def drain(self, max_messages: int = 400) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for _ in range(max_messages):
            try:
                out.append(self._q.get_nowait())
            except queue.Empty:
                break
        return out

    def _run(self) -> None:
        try:
            from chat_downloader import ChatDownloader

            cd = ChatDownloader()
            for raw in cd.get_chat(self._url):
                if self._stop.is_set():
                    break
                norm = _normalize_message(raw if isinstance(raw, dict) else {})
                if norm is None:
                    continue
                try:
                    self._q.put_nowait(norm)
                except queue.Full:
                    try:
                        self._q.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        self._q.put_nowait(norm)
                    except queue.Full:
                        pass
        except Exception as e:
            self._log.warning(f"YouTube live chat reader stopped: {e}")
            try:
                self._q.put_nowait({"author": "system", "text": f"[chat stream error: {e}]", "timeText": ""})
            except queue.Full:
                pass
