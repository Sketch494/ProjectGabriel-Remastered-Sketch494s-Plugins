"""Webcam capture loop that mirrors the host's screen-vision pipeline.

The flow is intentionally identical to `src/gemini_live/vision.py`:

    cv2.VideoCapture -> downscale -> JPEG bytes -> live_session._out_queue
    queue item shape: ("video", jpeg_bytes)

The session's `_send_realtime_loop` already handles the "video" branch, so by
sharing the same queue we get realtime ingestion into Gemini Live for free.
Camera reads are blocking, so they run inside `asyncio.to_thread`.

The plugin only ever holds **one** active stream — calling `start` while
already running will reuse the existing loop (and re-apply settings) rather
than racing two captures on the same device.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

try:
    import cv2  # type: ignore[import-not-found]
except Exception:  # pragma: no cover - optional dep
    cv2 = None  # type: ignore[assignment]

try:
    from PIL import Image  # type: ignore[import-not-found]
except Exception:  # pragma: no cover - optional dep
    Image = None  # type: ignore[assignment]

import io

from . import preview_server


_BACKEND_MAP = {
    "auto": 0,
    "any": 0,
    "dshow": 700,   # cv2.CAP_DSHOW
    "msmf": 1400,   # cv2.CAP_MSMF
    "v4l2": 200,    # cv2.CAP_V4L2
    "avfoundation": 1200,  # cv2.CAP_AVFOUNDATION
}


def _resolve_backend(name: str | None) -> int:
    if not name:
        return 0
    return _BACKEND_MAP.get(str(name).lower(), 0)


class CameraStream:
    """Manages a single webcam capture + frame-push loop.

    Public surface used by `CameraTools`:
        * ``start(device_index=None, frame_interval_ms=None, max_size=None,
                  mirror=None, backend=None)`` — launch the capture loop
        * ``stop()`` — cancel the loop and release the device
        * ``snapshot(device_index=None)`` — single frame as JPEG bytes
        * ``enumerate(max_index=8)`` — probe device indices 0..max_index
        * ``status()`` — dict for `getCameraStatus`
    """

    def __init__(self, cfg: dict[str, Any], tool_handler: Any, logger: logging.Logger):
        self._cfg = cfg or {}
        self._handler = tool_handler
        self._log = logger
        self._task: asyncio.Task | None = None
        self._stopping = False
        self._cap = None  # type: ignore[assignment]
        self._device_index: int | None = None
        self._frame_interval_ms: int = int(self._cfg.get("frame_interval_ms", 1000))
        self._max_size: int = int(self._cfg.get("max_size", 1024))
        self._jpeg_quality: int = int(self._cfg.get("jpeg_quality", 75))
        self._mirror: bool = bool(self._cfg.get("mirror", True))
        self._backend: int = _resolve_backend(self._cfg.get("default_backend"))
        self._pause_on_speaking: bool = bool(self._cfg.get("pause_on_speaking", True))
        self._auto_close_seconds: float = float(self._cfg.get("auto_close_seconds", 0))
        self._opened_at: float | None = None
        self._frames_sent = 0
        self._last_frame_size: tuple[int, int] | None = None

    @property
    def is_active(self) -> bool:
        return self._task is not None and not self._task.done()

    def status(self) -> dict[str, Any]:
        return {
            "active": self.is_active,
            "deviceIndex": self._device_index,
            "frameIntervalMs": self._frame_interval_ms,
            "maxSize": self._max_size,
            "mirror": self._mirror,
            "framesSent": self._frames_sent,
            "lastFrameSize": list(self._last_frame_size) if self._last_frame_size else None,
            "openSeconds": (time.time() - self._opened_at) if self._opened_at else 0,
            "autoCloseSeconds": self._auto_close_seconds,
        }

    # ---- lifecycle -------------------------------------------------------

    async def start(
        self,
        device_index: int | None = None,
        frame_interval_ms: int | None = None,
        max_size: int | None = None,
        mirror: bool | None = None,
        backend: str | None = None,
    ) -> dict[str, Any]:
        if cv2 is None:
            return {"result": "error", "message": "opencv-python is not installed (pip install opencv-python)"}
        if Image is None:
            return {"result": "error", "message": "Pillow is not installed (pip install Pillow)"}

        if device_index is None:
            device_index = int(self._cfg.get("default_device", 0))
        if frame_interval_ms is not None:
            self._frame_interval_ms = max(50, int(frame_interval_ms))
        if max_size is not None:
            self._max_size = max(64, int(max_size))
        if mirror is not None:
            self._mirror = bool(mirror)
        if backend is not None:
            self._backend = _resolve_backend(backend)

        if self.is_active and self._device_index == device_index:
            return {"result": "ok", "message": "camera already streaming", **self.status()}

        if self.is_active:
            await self.stop()

        cap = await asyncio.to_thread(self._open_capture, device_index)
        if cap is None:
            return {"result": "error", "message": f"failed to open camera index {device_index}"}
        self._cap = cap
        self._device_index = int(device_index)
        self._opened_at = time.time()
        self._frames_sent = 0
        self._stopping = False
        self._task = asyncio.create_task(self._run_loop(), name=f"camera-stream-{device_index}")
        try:
            preview_server.mark_active(True)
        except Exception:
            pass
        return {"result": "ok", "message": f"camera index {device_index} streaming", **self.status()}

    async def stop(self) -> dict[str, Any]:
        self._stopping = True
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        await asyncio.to_thread(self._release_capture)
        try:
            preview_server.mark_active(False)
            preview_server.clear_frame()
        except Exception:
            pass
        result = {"result": "ok", "message": "camera stopped", **self.status()}
        self._device_index = None
        self._opened_at = None
        return result

    # ---- one-shot --------------------------------------------------------

    async def snapshot(
        self,
        device_index: int | None = None,
        send_to_vision: bool = False,
    ) -> dict[str, Any]:
        if cv2 is None or Image is None:
            return {"result": "error", "message": "opencv-python and Pillow are required for snapshots"}

        if self.is_active and (device_index is None or device_index == self._device_index):
            jpeg = await asyncio.to_thread(self._read_and_encode, self._cap)
        else:
            idx = int(device_index if device_index is not None else self._cfg.get("default_device", 0))
            cap = await asyncio.to_thread(self._open_capture, idx)
            if cap is None:
                return {"result": "error", "message": f"failed to open camera index {idx}"}
            try:
                jpeg = await asyncio.to_thread(self._read_and_encode, cap)
            finally:
                await asyncio.to_thread(_release, cap)

        if jpeg is None:
            return {"result": "error", "message": "failed to capture frame"}

        try:
            preview_server.update_frame(
                jpeg,
                device_index=self._device_index if self.is_active else (device_index if device_index is not None else int(self._cfg.get("default_device", 0))),
                frames_sent=self._frames_sent,
                last_size=self._last_frame_size,
                started_at=self._opened_at or time.time(),
            )
        except Exception:
            pass

        if send_to_vision:
            pushed = self._push_frame(jpeg)
            return {
                "result": "ok",
                "frameBytes": len(jpeg),
                "sentToVision": pushed,
                "lastFrameSize": list(self._last_frame_size) if self._last_frame_size else None,
            }
        return {
            "result": "ok",
            "frameBytes": len(jpeg),
            "sentToVision": False,
            "lastFrameSize": list(self._last_frame_size) if self._last_frame_size else None,
        }

    # ---- enumeration -----------------------------------------------------

    async def enumerate(self, max_index: int = 8) -> dict[str, Any]:
        if cv2 is None:
            return {"result": "error", "message": "opencv-python is not installed"}
        max_index = max(0, min(32, int(max_index)))
        rows = await asyncio.to_thread(self._probe_indices, max_index)
        return {"result": "ok", "cameras": rows}

    # ---- internals -------------------------------------------------------

    def _open_capture(self, device_index: int):
        try:
            cap = cv2.VideoCapture(device_index, self._backend) if self._backend else cv2.VideoCapture(device_index)
            if not cap.isOpened():
                cap.release()
                return None
            return cap
        except Exception as e:  # pragma: no cover - hardware/driver dependent
            self._log.warning(f"camera: open({device_index}) failed: {e}")
            return None

    def _release_capture(self):
        cap = self._cap
        self._cap = None
        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass

    def _probe_indices(self, max_index: int) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for i in range(max_index + 1):
            cap = None
            try:
                cap = cv2.VideoCapture(i, self._backend) if self._backend else cv2.VideoCapture(i)
                if cap.isOpened():
                    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
                    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
                    rows.append({"index": i, "width": w, "height": h})
            except Exception:
                pass
            finally:
                if cap is not None:
                    try:
                        cap.release()
                    except Exception:
                        pass
        return rows

    def _read_and_encode(self, cap) -> bytes | None:
        if cap is None:
            return None
        try:
            ok, frame = cap.read()
        except Exception as e:  # pragma: no cover
            self._log.debug(f"camera read error: {e}")
            return None
        if not ok or frame is None:
            return None

        if self._mirror:
            frame = cv2.flip(frame, 1)

        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        img = Image.fromarray(rgb)
        if img.width > self._max_size or img.height > self._max_size:
            img.thumbnail([self._max_size, self._max_size])
        self._last_frame_size = (img.width, img.height)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=self._jpeg_quality)
        buf.seek(0)
        return buf.read()

    def _push_frame(self, jpeg: bytes) -> bool:
        live = getattr(self._handler, "live_session", None)
        queue = getattr(live, "_out_queue", None) if live is not None else None
        if queue is None:
            return False
        try:
            queue.put_nowait(("video", jpeg))
            return True
        except asyncio.QueueFull:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                queue.put_nowait(("video", jpeg))
                return True
            except asyncio.QueueFull:
                return False

    def _is_paused(self) -> bool:
        if not self._pause_on_speaking:
            return False
        live = getattr(self._handler, "live_session", None)
        if live is None:
            return False
        if getattr(live, "_speaking", False):
            return True
        audio = getattr(self._handler, "audio", None)
        if audio is not None:
            try:
                if audio.is_music_playing():
                    music_gen = getattr(self._handler, "music_gen", None)
                    if not (music_gen and getattr(music_gen, "is_active", False)):
                        return True
            except Exception:
                pass
        return False

    async def _run_loop(self):
        log = self._log
        try:
            while not self._stopping:
                if self._auto_close_seconds and self._opened_at:
                    if (time.time() - self._opened_at) > self._auto_close_seconds:
                        log.info("camera: auto-close timeout reached")
                        break
                if self._is_paused():
                    await asyncio.sleep(self._frame_interval_ms / 1000.0)
                    continue
                jpeg = await asyncio.to_thread(self._read_and_encode, self._cap)
                if jpeg is not None:
                    if self._push_frame(jpeg):
                        self._frames_sent += 1
                    try:
                        preview_server.update_frame(
                            jpeg,
                            device_index=self._device_index,
                            frames_sent=self._frames_sent,
                            last_size=self._last_frame_size,
                            started_at=self._opened_at,
                        )
                    except Exception:
                        pass
                await asyncio.sleep(self._frame_interval_ms / 1000.0)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.error(f"camera loop error: {e}", exc_info=True)
        finally:
            await asyncio.to_thread(self._release_capture)


def _release(cap) -> None:
    try:
        cap.release()
    except Exception:
        pass
