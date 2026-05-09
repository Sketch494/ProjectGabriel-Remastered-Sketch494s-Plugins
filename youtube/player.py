"""ffmpeg → PyAudio streaming player for a single YouTube stream URL.

Mirrors the Suno player layout:
    * subprocess `ffmpeg -i <stream_url> -f s16le ...` decodes to PCM
    * a worker thread reads PCM in ~100ms chunks and writes to a PyAudio
      output stream
    * volume + a fade multiplier are applied per-chunk (numpy)
    * `pause()` flips a stop-write flag (we keep ffmpeg going so resume is
      instant rather than seeking back through HTTP)
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pyaudio

logger = logging.getLogger(__name__)


YT_SR = 48000
YT_CH = 2
YT_BYTES_PER_SAMPLE = 2  # int16


def _resolve_ffmpeg() -> Optional[str]:
    """Find an ffmpeg executable. Prefer imageio_ffmpeg's bundled binary."""
    try:
        import imageio_ffmpeg  # type: ignore
        path = imageio_ffmpeg.get_ffmpeg_exe()
        if path and os.path.exists(path):
            return path
    except Exception:
        pass
    sys_path = shutil.which("ffmpeg")
    if sys_path:
        return sys_path
    return None


@dataclass
class YouTubeTrack:
    video_id: str
    title: str
    uploader: str
    duration: float
    stream_url: str
    webpage_url: str
    thumbnail: str = ""


@dataclass
class _PlayerState:
    track: YouTubeTrack
    play_start: Optional[float] = None
    pause_started_at: Optional[float] = None
    paused_total: float = 0.0
    output_bytes: int = 0
    written_bytes: int = 0
    finished: bool = False
    error: Optional[str] = None


class YouTubePlayer:
    """Plays one YouTube stream URL via ffmpeg -> PyAudio."""

    def __init__(self, track: YouTubeTrack, audio_mgr, volume: int = 80):
        self.state = _PlayerState(track=track)
        self._audio = audio_mgr
        self._volume = max(0, min(200, int(volume))) / 100.0
        self._pya = audio_mgr.pya
        self._stream = None
        self._proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_flag = threading.Event()
        self._pause_flag = threading.Event()
        self._fade_volume = 1.0
        self._on_finished = None  # type: ignore[assignment]

    def set_on_finished(self, cb) -> None:
        self._on_finished = cb

    @property
    def volume(self) -> float:
        return self._volume

    def set_volume(self, volume_pct: int) -> None:
        self._volume = max(0, min(200, int(volume_pct))) / 100.0

    def set_fade(self, fade: float) -> None:
        self._fade_volume = max(0.0, min(1.0, float(fade)))

    @property
    def is_paused(self) -> bool:
        return self._pause_flag.is_set()

    def pause(self) -> None:
        if not self._pause_flag.is_set():
            self._pause_flag.set()
            self.state.pause_started_at = time.time()

    def resume(self) -> None:
        if self._pause_flag.is_set():
            if self.state.pause_started_at is not None:
                self.state.paused_total += time.time() - self.state.pause_started_at
                self.state.pause_started_at = None
            self._pause_flag.clear()

    def position_seconds(self) -> float:
        if self.state.play_start is None:
            return 0.0
        elapsed = time.time() - self.state.play_start - self.state.paused_total
        if self.state.pause_started_at is not None:
            elapsed -= time.time() - self.state.pause_started_at
        return max(0.0, elapsed)

    def start(self) -> bool:
        ffmpeg = _resolve_ffmpeg()
        if not ffmpeg:
            self.state.error = "ffmpeg not found (install imageio-ffmpeg or system ffmpeg)"
            logger.error(self.state.error)
            return False

        out_dev = getattr(self._audio, "output_device", None)
        try:
            self._stream = self._pya.open(
                format=pyaudio.paInt16,
                channels=YT_CH,
                rate=YT_SR,
                output=True,
                output_device_index=out_dev,
            )
        except Exception as e:
            self.state.error = f"pyaudio open failed: {e}"
            logger.error(self.state.error)
            return False

        cmd = [
            ffmpeg, "-loglevel", "error", "-nostdin",
            "-probesize", "32", "-analyzeduration", "0",
            "-fflags", "+nobuffer+discardcorrupt",
            "-flags", "low_delay",
            "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
            "-i", self.state.track.stream_url,
            "-vn",
            "-f", "s16le", "-ar", str(YT_SR), "-ac", str(YT_CH),
            "pipe:1",
        ]
        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                bufsize=0,
            )
        except Exception as e:
            self.state.error = f"ffmpeg spawn failed: {e}"
            logger.error(self.state.error)
            try:
                self._stream.close()
            except Exception:
                pass
            self._stream = None
            return False

        self._thread = threading.Thread(target=self._pump_loop, daemon=True, name=f"yt-{self.state.track.video_id}")
        self._thread.start()
        self.state.play_start = time.time()
        logger.info(f"YouTube playback started: {self.state.track.video_id} {self.state.track.title!r}")
        return True

    def _pump_loop(self):
        chunk_size = YT_SR * YT_CH * YT_BYTES_PER_SAMPLE // 10  # ~100ms
        try:
            while not self._stop_flag.is_set():
                data = self._proc.stdout.read(chunk_size) if self._proc and self._proc.stdout else b""
                if not data:
                    break
                self.state.output_bytes += len(data)

                # Pause: hold off writing without dropping the stream
                while self._pause_flag.is_set() and not self._stop_flag.is_set():
                    time.sleep(0.05)
                if self._stop_flag.is_set():
                    break

                samples = np.frombuffer(data, dtype=np.int16)
                vol = self._volume * self._fade_volume
                if vol != 1.0:
                    scaled = (samples.astype(np.float32) * vol).clip(-32768, 32767).astype(np.int16)
                    data = scaled.tobytes()
                try:
                    self._stream.write(data)
                except Exception:
                    break
                self.state.written_bytes += len(data)
        except Exception as e:
            logger.error(f"YouTube pump loop error: {e}")
            self.state.error = str(e)
        finally:
            self.state.finished = True
            try:
                if self._stream:
                    self._stream.stop_stream()
                    self._stream.close()
            except Exception:
                pass
            self._stream = None
            try:
                if self._proc and self._proc.poll() is None:
                    self._proc.terminate()
            except Exception:
                pass
            logger.info(f"YouTube playback ended: {self.state.track.video_id}")
            cb = self._on_finished
            if cb is not None:
                try:
                    cb(self)
                except Exception as e:
                    logger.debug(f"yt on_finished callback error: {e}")

    def stop(self):
        if self._stop_flag.is_set():
            return
        self._stop_flag.set()
        self._pause_flag.clear()
        try:
            if self._proc and self._proc.poll() is None:
                self._proc.terminate()
        except Exception:
            pass
