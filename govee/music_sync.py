"""Map host music playback time to Govee RGB + brightness (local library + Suno/external)."""

from __future__ import annotations

import asyncio
import colorsys
import logging
import math
from collections.abc import Callable
from typing import Any


def _hsv_to_rgb_int(h: float, s: float, v: float) -> int:
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, max(0.0, min(1.0, s)), max(0.0, min(1.0, v)))
    return (int(r * 255) << 16) | (int(g * 255) << 8) | int(b * 255)


def playback_phase_seconds(handler: Any | None) -> float | None:
    """Return a monotonic seconds value tied to the current track, or None if idle."""
    if handler is None:
        return None
    audio = getattr(handler, "audio", None)
    if audio is None or not audio.is_music_playing():
        return None
    prog = audio.get_music_progress()
    if prog is not None:
        return float(prog.get("position") or 0.0)
    suno = getattr(handler, "suno", None)
    if suno is not None and hasattr(suno, "get_progress"):
        try:
            sp = suno.get_progress()
            if sp and sp.get("position") is not None:
                return float(sp["position"])
        except Exception:
            pass
    return audio.get_music_sync_position()


async def music_sync_loop(
    *,
    controller: Any,
    handler_getter: Callable[[], Any | None],
    logger: logging.Logger,
) -> None:
    cfg_root: dict[str, Any] = controller.cfg if hasattr(controller, "cfg") else {}
    ms = cfg_root.get("music_sync") or {}
    if not isinstance(ms, dict):
        return

    poll_ms = float(ms.get("poll_interval_ms") or 120)
    poll_s = max(0.05, poll_ms / 1000.0)
    mode = str(ms.get("mode") or "hue_plus_pulse").lower()
    hue_period = max(5.0, float(ms.get("hue_period_seconds") or 48.0))
    saturation = float(ms.get("saturation") or 1.0)
    value = float(ms.get("value") or 1.0)

    bri_base = float(ms.get("brightness_base") or 55)
    bri_swing = float(ms.get("brightness_swing") or 28)
    pulse_hz = float(ms.get("pulse_hz") or 0.85)

    bri_lo = int(ms.get("brightness_min") or 8)
    bri_hi = int(ms.get("brightness_max") or 100)
    bypass = bool(ms.get("bypass_color_blocks", True))

    group = ms.get("group")
    raw_ids = ms.get("device_ids") or []
    device_ids = [str(x) for x in raw_ids] if isinstance(raw_ids, list) else []

    last_rgb: int | None = None
    last_bri: int | None = None

    while getattr(controller, "_running", False):
        await asyncio.sleep(poll_s)
        try:
            controller.reload_cfg(getattr(controller, "_host_cfg", None))
            ms = controller.cfg.get("music_sync") or {}
            if not isinstance(ms, dict) or not ms.get("enabled"):
                continue

            handler = handler_getter()
            t = playback_phase_seconds(handler)
            if t is None:
                last_rgb = None
                last_bri = None
                continue

            devices: list[dict[str, Any]] = []
            if device_ids:
                for did in device_ids:
                    rec = controller.store.devices.get(did)
                    if rec:
                        devices.append(rec)
            elif group:
                devs, err = controller._resolve_group(str(group))
                if err:
                    continue
                devices = devs
            if not devices:
                continue

            hue = (t / hue_period) % 1.0
            rgb = _hsv_to_rgb_int(hue, saturation, value)

            use_pulse = mode in ("hue_plus_pulse", "both", "pulse", "pulse_brightness")
            if use_pulse:
                brighten = bri_base + bri_swing * math.sin(2 * math.pi * pulse_hz * t)
            else:
                brighten = bri_base
            brighten = max(float(bri_lo), min(float(bri_hi), brighten))
            bri_i = int(round(brighten))

            if last_rgb == rgb and last_bri == bri_i:
                continue
            last_rgb, last_bri = rgb, bri_i

            await controller.apply_music_sync_frame(
                devices,
                rgb_int=rgb,
                brightness=bri_i,
                bypass_color_blocks=bypass,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug("govee music_sync: %s", e)
