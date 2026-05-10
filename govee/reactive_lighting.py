"""Reactive Govee lighting: thinking brightness pulse, Discord color flash, mood colors."""

from __future__ import annotations

import asyncio
from typing import Any


def _rgb_tuple_to_int(rgb: Any) -> int | None:
    if not isinstance(rgb, (list, tuple)) or len(rgb) < 3:
        return None
    try:
        r = max(0, min(255, int(rgb[0])))
        g = max(0, min(255, int(rgb[1])))
        b = max(0, min(255, int(rgb[2])))
        return (r << 16) | (g << 8) | b
    except (TypeError, ValueError):
        return None


class ReactiveLighting:
    """Handles ai_thinking_start, discord_notification, emotion_animation events."""

    def __init__(self, controller: Any, cfg: dict[str, Any]):
        self.ctrl = controller
        self.cfg = cfg if isinstance(cfg, dict) else {}
        self.log = controller.log
        self._lock = asyncio.Lock()
        self._armed = True
        self._bound = False
        self._last_mood_rgb: int | None = None
        self._last_mood_brightness: int | None = None

    def is_armed(self) -> bool:
        if not self._armed:
            return False
        if not self.cfg:
            return False
        return bool(self.cfg.get("enabled", False))

    def disable(self) -> None:
        self._armed = False

    def _refresh_cfg(self) -> None:
        self.ctrl.reload_cfg(getattr(self.ctrl, "_host_cfg", None))
        rc = self.ctrl.cfg.get("reactive_lighting")
        self.cfg = rc if isinstance(rc, dict) else {}

    def _section(self, key: str) -> dict[str, Any]:
        s = self.cfg.get(key)
        return s if isinstance(s, dict) else {}

    def _bypass(self) -> bool:
        return bool(self.cfg.get("bypass_safety_blocks", True))

    def _resolve_devices(self) -> list[dict[str, Any]]:
        t = self.cfg.get("targets") or {}
        if not isinstance(t, dict):
            t = {}
        group = t.get("group")
        raw_ids = t.get("device_ids") or []
        ids = [str(x) for x in raw_ids] if isinstance(raw_ids, list) else []
        devices: list[dict[str, Any]] = []
        if ids:
            for did in ids:
                rec = self.ctrl.store.devices.get(did)
                if rec:
                    devices.append(rec)
        elif group:
            devs, err = self.ctrl._resolve_group(str(group))
            if err:
                self.log.debug("govee reactive targets: %s", err)
                return []
            devices = devs
        else:
            self.log.debug("govee reactive: set reactive_lighting.targets.group or device_ids")
        return devices

    def _snapshot_uniform(self, devices: list[dict[str, Any]]) -> tuple[int, int]:
        rgb_out = 0xFFFFFF
        bri_out = 80
        for dev in devices:
            summ = dict(dev.get("summary") or {})
            rgb = summ.get("color_rgb_int")
            bri = summ.get("brightness")
            try:
                if rgb is not None:
                    rgb_out = int(rgb)
            except (TypeError, ValueError):
                pass
            try:
                if bri is not None:
                    bri_out = int(bri)
            except (TypeError, ValueError):
                pass
            break
        ml = self._section("mood_lighting")
        if rgb_out == 0xFFFFFF and self._last_mood_rgb is not None:
            rgb_out = self._last_mood_rgb
        if bri_out == 80 and self._last_mood_brightness is not None:
            bri_out = self._last_mood_brightness
        d_rgb = _rgb_tuple_to_int(ml.get("default_rgb") if isinstance(ml, dict) else None)
        if d_rgb is not None and rgb_out == 0xFFFFFF:
            rgb_out = d_rgb
        return rgb_out, bri_out

    async def _apply_all(self, devices: list[dict[str, Any]], rgb_int: int, brightness: int) -> None:
        did0 = str(devices[0].get("device") or "") if devices else ""
        b = self.ctrl._clamp_brightness(brightness, did0) if did0 else int(brightness)
        await self.ctrl.apply_direct_rgb_frame(
            devices,
            rgb_int=int(rgb_int),
            brightness=b,
            bypass_color_blocks=self._bypass(),
        )

    async def on_ai_thinking_start(self) -> None:
        self._refresh_cfg()
        if not self.is_armed():
            return
        tp = self._section("thinking_pulse")
        if not tp.get("enabled", True):
            return
        dur = max(0.2, float(tp.get("duration_seconds") or 3))
        peak = int(tp.get("brightness_percent") or 100)
        async with self._lock:
            devices = self._resolve_devices()
            if not devices:
                return
            rgb0, bri0 = self._snapshot_uniform(devices)
            did0 = str(devices[0].get("device") or "")
            peak_use = self.ctrl._clamp_brightness(peak, did0)
            try:
                await self._apply_all(devices, rgb0, peak_use)
                await asyncio.sleep(dur)
            finally:
                await self._apply_all(devices, rgb0, bri0)

    async def on_discord_notification(self, **_kwargs: Any) -> None:
        self._refresh_cfg()
        if not self.is_armed():
            return
        dp = self._section("discord_pulse")
        if not dp.get("enabled", True):
            return
        dur = max(0.2, float(dp.get("duration_seconds") or 3))
        rgb_list = dp.get("rgb") or [88, 101, 242]
        discord_rgb = _rgb_tuple_to_int(rgb_list)
        if discord_rgb is None:
            discord_rgb = 0x5865F2
        async with self._lock:
            devices = self._resolve_devices()
            if not devices:
                return
            rgb0, bri0 = self._snapshot_uniform(devices)
            did0 = str(devices[0].get("device") or "")
            bri_keep = self.ctrl._clamp_brightness(bri0, did0)
            try:
                await self._apply_all(devices, discord_rgb, bri_keep)
                await asyncio.sleep(dur)
            finally:
                await self._apply_all(devices, rgb0, bri0)

    def _mood_entry(self, mood_key: str) -> dict[str, Any] | None:
        ml = self._section("mood_lighting")
        moods = ml.get("moods") or {}
        if not isinstance(moods, dict):
            return None
        return moods.get(mood_key) or moods.get(mood_key.lower())

    def _resolve_mood_key(self, animation: str) -> str | None:
        ml = self._section("mood_lighting")
        anim_map = ml.get("animation_moods") or {}
        an = (animation or "").strip()
        if not an:
            return None
        if isinstance(anim_map, dict):
            hit = anim_map.get(an) or anim_map.get(an.lower())
            if hit:
                return str(hit).strip().lower()
        al = an.lower().replace("_", "-")
        moods_cfg = ml.get("moods") or {}
        if isinstance(moods_cfg, dict):
            for mk in moods_cfg.keys():
                mkl = str(mk).lower()
                if mkl == al or mkl in al or al in mkl:
                    return mkl
        return None

    async def on_emotion_animation(self, *, animation: str, duration: Any = None) -> None:
        self._refresh_cfg()
        if not self.is_armed():
            return
        ml = self._section("mood_lighting")
        if not ml.get("enabled", True):
            return
        mood_key = self._resolve_mood_key(animation)
        if not mood_key:
            mood_key = "neutral"
        entry = self._mood_entry(mood_key) or self._mood_entry("neutral")
        if not isinstance(entry, dict):
            entry = {}
        rgb_list = entry.get("rgb") or ml.get("default_rgb") or [255, 255, 255]
        rgb_int = _rgb_tuple_to_int(rgb_list)
        if rgb_int is None:
            rgb_int = 0xFFFFFF
        base_bri = int(ml.get("base_brightness") or 80)
        ref_dur = float(ml.get("duration_brightness_reference_seconds") or 3.0)
        scale = float(entry.get("brightness_scale") or 1.0)
        dur_val: float | None = None
        if duration is not None:
            try:
                dur_val = float(duration)
            except (TypeError, ValueError):
                dur_val = None
        if dur_val is None:
            dur_val = ref_dur
        intensity = max(0.35, min(1.65, dur_val / max(0.5, ref_dur)))
        bri = int(round(base_bri * scale * intensity))
        async with self._lock:
            devices = self._resolve_devices()
            if not devices:
                return
            did0 = str(devices[0].get("device") or "")
            bri_c = self.ctrl._clamp_brightness(bri, did0)
            await self._apply_all(devices, rgb_int, bri_c)
        self._last_mood_rgb = rgb_int
        self._last_mood_brightness = bri_c

    def bind_ctx(self, ctx: Any) -> None:
        if self._bound:
            return
        self._bound = True

        def _gh():
            return getattr(ctx.tool_handler, "govee", None)

        def thinking_cb(*_a, **_k):
            gh = _gh()
            r = getattr(gh, "reactive", None) if gh else None
            if not r or not r.is_armed():
                return
            tp = r._section("thinking_pulse")
            if not tp.get("enabled", True):
                return
            return r.on_ai_thinking_start()

        def discord_cb(*_a, **kw):
            gh = _gh()
            r = getattr(gh, "reactive", None) if gh else None
            if not r or not r.is_armed():
                return
            dp = r._section("discord_pulse")
            if not dp.get("enabled", True):
                return
            return r.on_discord_notification(**kw)

        def emotion_cb(*_a, **kw):
            gh = _gh()
            r = getattr(gh, "reactive", None) if gh else None
            if not r or not r.is_armed():
                return
            ml = r._section("mood_lighting")
            if not ml.get("enabled", True):
                return
            anim = kw.get("animation")
            if not anim:
                return
            return r.on_emotion_animation(animation=str(anim), duration=kw.get("duration"))

        ctx.subscribe("ai_thinking_start", thinking_cb)
        ctx.subscribe("discord_notification", discord_cb)
        ctx.subscribe("emotion_animation", emotion_cb)
        self.log.info("govee reactive_lighting hooks registered (thinking / discord / emotion)")
