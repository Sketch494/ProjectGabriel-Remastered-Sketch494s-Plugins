"""Orchestrates Govee API, device store, restrictions, webhooks, and background tasks."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

from .ai_parser import build_color_db, norm_scene_query, resolve_color
from .command_handler import CommandPipeline
from .config_loader import JsonHotReloader, load_merged_config
from .device_store import DeviceStore, summarize_from_state
from .govee_api import GoveeAPI, GoveeAPIError
from .mqtt_sync import GoveeMqttBridge
from .music_sync import music_sync_loop
from .restrictions import RestrictionsEngine
from . import webhook as discord_webhook

_LOG = logging.getLogger(__name__)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        return data if isinstance(data, dict) else {}
    except Exception as e:
        _LOG.warning("govee: json read %s failed: %s", path, e)
        return {}


def _collect_keys(cfg: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    for k in cfg.get("api_keys") or []:
        if str(k).strip():
            keys.append(str(k).strip())
    env_single = os.environ.get(str(cfg.get("api_key_env") or "GOVEE_API_KEY") or "")
    if env_single:
        keys.append(env_single.strip())
    raw_list = os.environ.get(str(cfg.get("api_key_env_list") or "") or "")
    if raw_list:
        keys.extend([x.strip() for x in raw_list.split(",") if x.strip()])
    # de-dupe preserving order
    seen: set[str] = set()
    out: list[str] = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def _webhook_url(cfg: dict[str, Any]) -> str:
    url = str(cfg.get("discord_webhook") or "").strip()
    if not url:
        envn = str(cfg.get("discord_webhook_env") or "").strip()
        if envn:
            url = os.environ.get(envn, "") or ""
    return url.strip()


def _favorites_file_path(data_dir: Path, cfg: dict[str, Any]) -> Path | None:
    fp = str(cfg.get("favorites_path") or "").strip()
    if not fp:
        return None
    p = Path(fp)
    if not p.is_absolute():
        p = data_dir / p
    return p


class GoveeController:
    def __init__(self, logger: logging.Logger, plugin_dir: Path, data_dir: Path, host_cfg: dict[str, Any]):
        self.log = logger
        self.plugin_dir = plugin_dir
        self.data_dir = data_dir
        self._host_cfg = host_cfg
        self.cfg = load_merged_config(plugin_dir, data_dir, host_cfg)
        self.colors_raw = _read_json(plugin_dir / "colors.json")
        self.scenes_raw = _read_json(plugin_dir / "scenes.json")
        extras = list(self.cfg.get("_extra_colors") or [])
        self.color_db = build_color_db(self.colors_raw, extras)
        self.restrictions = RestrictionsEngine(self.cfg, self.color_db, self.scenes_raw)
        self._rest_hot = JsonHotReloader(plugin_dir / "restrictions.json", "restrictions")
        self._colors_hot = JsonHotReloader(plugin_dir / "colors.json", "colors")
        self._scenes_hot = JsonHotReloader(plugin_dir / "scenes.json", "scenes")
        cache_path = data_dir / "devices_cache.json"
        self.store = DeviceStore(cache_path)
        self.store.load_cache()
        self.api: GoveeAPI | None = None
        keys = _collect_keys(self.cfg)
        if keys:
            try:
                lc = self.cfg.get("light_control_min_interval_ms")
                self.api = GoveeAPI(
                    keys,
                    min_interval_ms=float(self.cfg.get("min_command_interval_ms") or 30),
                    light_control_min_interval_ms=float(lc) if lc is not None else None,
                    debug=bool(self.cfg.get("debug")),
                )
            except ValueError:
                self.api = None
        self._favorites_map = self._load_favorites_map()
        self.pipeline = CommandPipeline(float(self.cfg.get("command_cooldown_ms") or 0))
        self._mqtt: GoveeMqttBridge | None = None
        self._bg_tasks: list[asyncio.Task] = []
        self._running = False
        self._activity_path = data_dir / "activity.jsonl"
        self._analytics_path = data_dir / "analytics.json"
        self._control_fail_streak = 0
        self._last_emergency_mono = 0.0
        self._emergency_lock = asyncio.Lock()
        self._automation_next: dict[int, float] = {}

    def reload_cfg(self, host_cfg: dict[str, Any] | None = None) -> None:
        if host_cfg is not None:
            self._host_cfg = host_cfg
        self.cfg = load_merged_config(self.plugin_dir, self.data_dir, self._host_cfg)
        merged_colors = self._colors_hot.get() or self.colors_raw
        merged_scenes = self._scenes_hot.get() or self.scenes_raw
        rest_patch = self._rest_hot.get()
        if rest_patch:
            self.cfg = {**self.cfg, **rest_patch}
        extras = list(self.cfg.get("_extra_colors") or [])
        self.color_db = build_color_db(merged_colors, extras)
        self.restrictions.reload_cfg(self.cfg)
        self.scenes_raw = merged_scenes
        self.restrictions.scene_db = merged_scenes
        self._favorites_map = self._load_favorites_map()
        if self.api is not None:
            lc = self.cfg.get("light_control_min_interval_ms")
            self.api.set_rate_limits(
                float(self.cfg.get("min_command_interval_ms") or 30),
                float(lc) if lc is not None else None,
            )

    def _ensure_api(self) -> GoveeAPI:
        if self.api is None:
            keys = _collect_keys(self.cfg)
            if not keys:
                raise ValueError("No Govee API keys configured (plugins.govee.api_keys or GOVEE_API_KEY).")
            lc = self.cfg.get("light_control_min_interval_ms")
            self.api = GoveeAPI(
                keys,
                min_interval_ms=float(self.cfg.get("min_command_interval_ms") or 30),
                light_control_min_interval_ms=float(lc) if lc is not None else None,
                debug=bool(self.cfg.get("debug")),
            )
        return self.api

    def _load_favorites_map(self) -> dict[str, str]:
        path = _favorites_file_path(self.data_dir, self.cfg)
        if not path or not path.exists():
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return {}
            return {str(k).strip().lower(): str(v).strip() for k, v in data.items() if k and v}
        except Exception as e:
            self.log.debug("govee favorites read failed: %s", e)
            return {}

    def _resolve_favorite_scene(self, scene_name: str) -> str:
        if not scene_name:
            return scene_name
        return self._favorites_map.get(scene_name.strip().lower(), scene_name)

    async def start(self, *, tool_handler_getter: Any | None = None) -> None:
        self._running = True
        self.reload_cfg(self._host_cfg)
        try:
            api = self._ensure_api()
            rows = await api.list_devices()
            if rows:
                self.store.ingest_api_list(rows)
                self.store.save_cache()
        except Exception as e:
            self.log.warning("govee: initial device fetch failed (using cache if any): %s", e)

        if bool(self.cfg.get("mqtt_enabled")) and self.api:
            self._mqtt = GoveeMqttBridge(
                self.api.current_key,
                host=str(self.cfg.get("mqtt_host") or "mqtt.openapi.govee.com"),
                port=int(self.cfg.get("mqtt_port") or 8883),
                on_event=self._on_mqtt_event,
            )
            self._mqtt.start()

        interval = float(self.cfg.get("device_refresh_seconds") or 0)
        if interval > 0:
            self._bg_tasks.append(asyncio.create_task(self._refresh_loop(interval)))

        st = float(self.cfg.get("state_poll_seconds") or 0)
        if st > 0:
            self._bg_tasks.append(asyncio.create_task(self._state_poll_loop(st)))

        if self.cfg.get("automations"):
            self._bg_tasks.append(asyncio.create_task(self._automations_loop()))

        ms = self.cfg.get("music_sync")
        if isinstance(ms, dict) and ms.get("enabled") and tool_handler_getter is not None:
            self._bg_tasks.append(
                asyncio.create_task(
                    music_sync_loop(
                        controller=self,
                        handler_getter=tool_handler_getter,
                        logger=self.log,
                    )
                )
            )

    def _on_mqtt_event(self, payload: dict[str, Any]) -> None:
        did = str(payload.get("device") or "")
        if did and did in self.store.devices:
            self.store.update_summary(did, {"mqtt_event": True, "last_event_ts": datetime.utcnow().isoformat()})

    async def _refresh_loop(self, interval: float) -> None:
        while self._running:
            await asyncio.sleep(interval)
            try:
                await self.refresh_devices()
            except Exception as e:
                self.log.debug("govee refresh loop: %s", e)

    async def _state_poll_loop(self, interval: float) -> None:
        conc = max(1, int(self.cfg.get("max_concurrent_requests") or 4))
        sem = asyncio.Semaphore(conc)

        async def _one_poll(dev_id: str, rec: dict[str, Any]) -> None:
            sku = rec.get("sku")
            if not sku:
                return
            async with sem:
                try:
                    st = await self._ensure_api().get_state(str(sku), dev_id)
                    summ = summarize_from_state(rec, st)
                    self.store.update_summary(dev_id, summ)
                except Exception as e:
                    self.log.debug("govee state poll %s: %s", dev_id, e)

        while self._running:
            await asyncio.sleep(interval)
            try:
                pairs = list(self.store.devices.items())
                await asyncio.gather(*[_one_poll(did, rec) for did, rec in pairs])
            except Exception as e:
                self.log.debug("govee state poll: %s", e)

    async def _automations_loop(self) -> None:
        while self._running:
            await asyncio.sleep(1.0)
            try:
                self.reload_cfg(self._host_cfg)
                items = self.cfg.get("automations") or []
                if not isinstance(items, list) or not items:
                    continue
                now = time.monotonic()
                for i, item in enumerate(items):
                    if not isinstance(item, dict):
                        continue
                    every = float(item.get("interval_seconds") or item.get("every_seconds") or 0)
                    if every <= 0:
                        continue
                    nxt = self._automation_next.get(i, 0.0)
                    if now < nxt:
                        continue
                    self._automation_next[i] = now + every
                    devices, err = self._match_devices(
                        item.get("device_query"),
                        item.get("group"),
                        bool(item.get("all_devices")),
                    )
                    if err:
                        self.log.debug("govee automation %s skipped: %s", i, err)
                        continue
                    await self.control_targets(
                        devices,
                        power_on=item.get("power_on"),
                        brightness=item.get("brightness"),
                        color_name=item.get("color_name"),
                        hex_color=item.get("hex_color"),
                        rgb_string=item.get("rgb_string"),
                        kelvin=item.get("kelvin"),
                        scene_name=item.get("scene_name"),
                        source=str(item.get("source") or "automation"),
                        admin_override=bool(item.get("admin_override")),
                        user_confirmed_risk=bool(item.get("user_confirmed_risk")),
                        skip_emergency=True,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.log.debug("govee automations: %s", e)

    async def stop(self) -> None:
        self._running = False
        for t in self._bg_tasks:
            t.cancel()
        if self._bg_tasks:
            await asyncio.gather(*self._bg_tasks, return_exceptions=True)
        self._bg_tasks.clear()
        if self._mqtt:
            self._mqtt.stop()
            self._mqtt = None
        if self.api:
            await self.api.aclose()
            self.api = None

    async def refresh_devices(self) -> dict[str, Any]:
        api = self._ensure_api()
        rows = await api.list_devices()
        self.store.ingest_api_list(rows)
        self.store.save_cache()
        return {"result": "ok", "count": len(rows)}

    def _resolve_group(self, group_name: str | None) -> tuple[list[dict[str, Any]], str | None]:
        if not group_name:
            return [], None
        groups = self.cfg.get("device_groups") or {}
        if not isinstance(groups, dict):
            return [], "device_groups is not a dict"
        ids = groups.get(group_name) or groups.get(group_name.lower())
        if not isinstance(ids, list):
            return [], f"unknown group '{group_name}'"
        out: list[dict[str, Any]] = []
        for i in ids:
            dev_id = str(i)
            rec = self.store.devices.get(dev_id)
            if rec:
                out.append(rec)
        return out, None

    def _match_devices(self, query: str | None, group: str | None, select_all: bool) -> tuple[list[dict[str, Any]], str | None]:
        if group:
            bad_g, err = self.restrictions.is_group_blocked(group)
            if bad_g:
                return [], err
            devices, err2 = self._resolve_group(group)
            if err2:
                return [], err2
            if not devices:
                return [], f"group '{group}' resolved to zero known devices"
            return devices, None
        if select_all:
            return list(self.store.devices.values()), None
        devices = self.store.find_devices(query, None, False)
        if not devices:
            return [], "no devices matched query"
        return devices, None

    def _global_perms(self, device_id: str) -> dict[str, Any]:
        per = deepcopy(self.restrictions.merge_device_permissions(device_id))
        return per

    def _clamp_brightness(self, v: int | float, device_id: str) -> int:
        per = self._global_perms(device_id)
        lo = int(self.cfg.get("default_brightness_min") or 1)
        hi = int(self.cfg.get("default_brightness_max") or 100)
        if per.get("brightness_min") is not None:
            lo = max(lo, int(per["brightness_min"]))
        if per.get("brightness_max") is not None:
            hi = min(hi, int(per["brightness_max"]))
        iv = int(round(float(v)))
        return max(lo, min(hi, iv))

    def _find_capability_option(
        self, device: dict[str, Any], scene_name: str
    ) -> tuple[str | None, str | None, Any]:
        qn = norm_scene_query(scene_name)
        for cap in device.get("capabilities") or []:
            if not isinstance(cap, dict):
                continue
            ctype = str(cap.get("type") or "")
            inst = str(cap.get("instance") or "")
            if ctype not in (
                "devices.capabilities.dynamic_scene",
                "devices.capabilities.mode",
                "devices.capabilities.diy_color_setting",
            ):
                continue
            params = cap.get("parameters") or {}
            options = params.get("options") or []
            if not isinstance(options, list):
                continue
            for opt in options:
                if not isinstance(opt, dict):
                    continue
                name = str(opt.get("name") or "").lower()
                if qn == name or qn in name or name in qn:
                    return ctype, inst, opt.get("value")
        return None, None, None

    async def _resolve_scene(
        self, device: dict[str, Any], scene_name: str
    ) -> tuple[str | None, str | None, Any, str | None]:
        ct, inst, val = self._find_capability_option(device, scene_name)
        if ct and inst and val is not None:
            return ct, inst, val, None
        sku = str(device.get("sku") or "")
        dev_id = str(device.get("device") or "")
        api = self._ensure_api()
        try:
            pl = await api.get_light_scenes(sku, dev_id)
            for cap in pl.get("capabilities") or []:
                if not isinstance(cap, dict):
                    continue
                if cap.get("type") != "devices.capabilities.dynamic_scene":
                    continue
                inst = str(cap.get("instance") or "")
                params = cap.get("parameters") or {}
                for opt in params.get("options") or []:
                    if not isinstance(opt, dict):
                        continue
                    name = str(opt.get("name") or "").lower()
                    if norm_scene_query(scene_name) in name or name in norm_scene_query(scene_name):
                        return "devices.capabilities.dynamic_scene", inst, opt.get("value"), None
        except Exception:
            pass
        try:
            pl = await api.get_diy_scenes(sku, dev_id)
            for cap in pl.get("capabilities") or []:
                if not isinstance(cap, dict):
                    continue
                if cap.get("type") != "devices.capabilities.diy_color_setting":
                    continue
                inst = str(cap.get("instance") or "diyScene")
                params = cap.get("parameters") or {}
                for opt in params.get("options") or []:
                    if not isinstance(opt, dict):
                        continue
                    name = str(opt.get("name") or "").lower()
                    if norm_scene_query(scene_name) in name or name in norm_scene_query(scene_name):
                        return "devices.capabilities.diy_color_setting", inst, opt.get("value"), None
        except Exception:
            pass
        return None, None, None, "scene not found on device"

    async def _try_emergency_fallback(self) -> None:
        ef = self.cfg.get("emergency_fallback")
        if not isinstance(ef, dict) or not ef.get("enabled"):
            return
        need = int(ef.get("consecutive_failures") or 5)
        if self._control_fail_streak < need:
            return
        cool = float(ef.get("cooldown_seconds") or 300)
        now = time.monotonic()
        if now - self._last_emergency_mono < cool:
            return
        apply = ef.get("apply")
        if not isinstance(apply, dict):
            apply = {
                k: v
                for k, v in ef.items()
                if k
                not in (
                    "enabled",
                    "consecutive_failures",
                    "cooldown_seconds",
                    "source_label",
                    "apply",
                )
            }
        async with self._emergency_lock:
            self._last_emergency_mono = time.monotonic()
            self._control_fail_streak = 0
            devices, err = self._match_devices(
                apply.get("device_query"),
                apply.get("group"),
                bool(apply.get("all_devices")),
            )
            if err or not devices:
                self.log.warning("govee emergency_fallback skipped: %s", err or "no targets")
                return
            self.log.warning("govee emergency_fallback applying to %s device(s)", len(devices))
            await self.control_targets(
                devices,
                power_on=apply.get("power_on"),
                brightness=apply.get("brightness"),
                color_name=apply.get("color_name"),
                hex_color=apply.get("hex_color"),
                rgb_string=apply.get("rgb_string"),
                kelvin=apply.get("kelvin"),
                scene_name=apply.get("scene_name"),
                source=str(ef.get("source_label") or "emergency_fallback"),
                admin_override=True,
                user_confirmed_risk=True,
                skip_emergency=True,
            )

    async def _pull_state(self, device: dict[str, Any]) -> dict[str, Any]:
        sku = str(device.get("sku") or "")
        dev_id = str(device.get("device") or "")
        try:
            st = await self._ensure_api().get_state(sku, dev_id)
            summ = summarize_from_state(device, st)
            self.store.update_summary(dev_id, summ)
            return summ
        except Exception as e:
            self.log.debug("govee state: %s", e)
            return dict(device.get("summary") or {})

    async def apply_music_sync_frame(
        self,
        devices: list[dict[str, Any]],
        *,
        rgb_int: int,
        brightness: int,
        bypass_color_blocks: bool,
    ) -> None:
        """Direct light updates for reactive music sync (not full control_targets / webhook)."""
        api = self._ensure_api()
        for dev in devices:
            sku = str(dev.get("sku") or "")
            dev_id = str(dev.get("device") or "")
            if not sku or not dev_id:
                continue
            name = str(dev.get("deviceName") or dev_id)
            ok, _err = self.restrictions.device_allowed(dev_id, name)
            if not ok:
                continue
            per = self._global_perms(dev_id)
            try:
                if not bypass_color_blocks:
                    if bool(self.cfg.get("block_color_changes")) and not per.get("allow_color"):
                        continue
                    bc, _why = self.restrictions.check_color_blocked(int(rgb_int))
                    if bc:
                        continue
                if bool(self.cfg.get("block_brightness_changes")) and not per.get("allow_brightness"):
                    continue
                b = self._clamp_brightness(brightness, dev_id)
                if self.store.capability_supports(dev, "devices.capabilities.range", "brightness"):
                    await api.control(sku, dev_id, "devices.capabilities.range", "brightness", b)
                if self.store.capability_supports(dev, "devices.capabilities.color_setting", "colorRgb"):
                    await api.control(
                        sku,
                        dev_id,
                        "devices.capabilities.color_setting",
                        "colorRgb",
                        int(rgb_int),
                    )
                summ = dict(dev.get("summary") or {})
                summ.update({"color_rgb_int": rgb_int, "brightness": b})
                self.store.update_summary(dev_id, summ)
            except Exception as e:
                self.log.debug("govee music sync %s: %s", dev_id, e)

    async def control_targets(
        self,
        devices: list[dict[str, Any]],
        *,
        power_on: bool | None = None,
        brightness: int | None = None,
        color_name: str | None = None,
        hex_color: str | None = None,
        rgb_string: str | None = None,
        kelvin: int | None = None,
        scene_name: str | None = None,
        source: str = "AI",
        admin_override: bool = False,
        user_confirmed_risk: bool = False,
        skip_emergency: bool = False,
    ) -> dict[str, Any]:
        self.reload_cfg(self._host_cfg)
        results: list[dict[str, Any]] = []

        rgb_resolved: int | None = None
        if color_name or hex_color or rgb_string:
            rgb_resolved, errc = resolve_color(
                color_name=color_name,
                hex_color=hex_color,
                rgb_string=rgb_string,
                color_db=self.color_db,
            )
            if errc:
                return {"result": "error", "message": errc}
            bc, why = self.restrictions.check_color_blocked(rgb_resolved)
            if bc:
                return {"result": "error", "message": f"blocked color: {why}"}

        effective_scene: str | None = None
        if scene_name:
            effective_scene = self._resolve_favorite_scene(scene_name)
            bs, why = self.restrictions.check_scene_blocked(effective_scene, admin_override=admin_override)
            if bs:
                return {"result": "error", "message": f"blocked scene: {why}"}

        async def _one(dev: dict[str, Any]) -> dict[str, Any]:
            sku = str(dev.get("sku") or "")
            dev_id = str(dev.get("device") or "")
            name = str(dev.get("deviceName") or dev_id)
            okd, errmsg = self.restrictions.device_allowed(dev_id, name)
            if not okd:
                return {"device": dev_id, "ok": False, "error": errmsg}
            per = self._global_perms(dev_id)
            prev = dict(dev.get("summary") or {})

            if power_on is False:
                if bool(self.cfg.get("block_power_off")) and not admin_override:
                    if not per.get("allow_power_off"):
                        return {"device": dev_id, "ok": False, "error": "power off blocked"}
                if bool(self.cfg.get("require_confirmation_for_power_off")):
                    if not user_confirmed_risk and not per.get("skip_power_confirm"):
                        return {"device": dev_id, "ok": False, "error": "power off needs confirmation"}

            api = self._ensure_api()
            try:
                if power_on is True and self.store.capability_supports(dev, "devices.capabilities.on_off", "powerSwitch"):
                    await api.control(sku, dev_id, "devices.capabilities.on_off", "powerSwitch", 1)
                if power_on is False and self.store.capability_supports(dev, "devices.capabilities.on_off", "powerSwitch"):
                    await api.control(sku, dev_id, "devices.capabilities.on_off", "powerSwitch", 0)

                if brightness is not None:
                    if bool(self.cfg.get("block_brightness_changes")) and not admin_override:
                        if not per.get("allow_brightness"):
                            return {"device": dev_id, "ok": False, "error": "brightness changes blocked"}
                    if self.store.capability_supports(dev, "devices.capabilities.range", "brightness"):
                        b = self._clamp_brightness(brightness, dev_id)
                        await api.control(sku, dev_id, "devices.capabilities.range", "brightness", b)

                if rgb_resolved is not None:
                    if bool(self.cfg.get("block_color_changes")) and not admin_override:
                        if not per.get("allow_color"):
                            return {"device": dev_id, "ok": False, "error": "color changes blocked"}
                    if self.store.capability_supports(dev, "devices.capabilities.color_setting", "colorRgb"):
                        await api.control(sku, dev_id, "devices.capabilities.color_setting", "colorRgb", int(rgb_resolved))

                if kelvin is not None:
                    if bool(self.cfg.get("block_color_changes")) and not admin_override and not per.get("allow_color"):
                        return {"device": dev_id, "ok": False, "error": "color temperature blocked"}
                    if self.store.capability_supports(dev, "devices.capabilities.color_setting", "colorTemperatureK"):
                        await api.control(
                            sku,
                            dev_id,
                            "devices.capabilities.color_setting",
                            "colorTemperatureK",
                            int(kelvin),
                        )

                if effective_scene:
                    if bool(self.cfg.get("block_scene_changes")) and not admin_override and not per.get("allow_scene"):
                        return {"device": dev_id, "ok": False, "error": "scene changes blocked by configuration"}
                    if (
                        bool(self.cfg.get("require_confirmation_for_scene"))
                        and not user_confirmed_risk
                        and not admin_override
                        and not per.get("skip_scene_confirm")
                    ):
                        return {"device": dev_id, "ok": False, "error": "scene change requires userConfirmedRisk=true"}
                    ct, inst, val, serr = await self._resolve_scene(dev, effective_scene)
                    if serr or not ct:
                        return {"device": dev_id, "ok": False, "error": serr or "scene unsupported"}
                    await api.control(sku, dev_id, ct, inst, val)

                new_s = await self._pull_state(dev)
                url = _webhook_url(self.cfg)
                if url:
                    action_parts = []
                    if power_on is not None:
                        action_parts.append("power")
                    if brightness is not None:
                        action_parts.append("brightness")
                    if rgb_resolved is not None or kelvin is not None:
                        action_parts.append("color")
                    if effective_scene:
                        action_parts.append("scene")
                    await discord_webhook.post_device_change(
                        url,
                        device_name=name,
                        action=", ".join(action_parts) or "update",
                        previous=prev,
                        new=new_s,
                        source=source,
                        scene_name=effective_scene or scene_name,
                    )
                self._log_activity(
                    dev_id,
                    action="control",
                    details={
                        "power": power_on,
                        "brightness": brightness,
                        "scene_requested": scene_name,
                        "scene_effective": effective_scene,
                    },
                )
                return {"device": dev_id, "ok": True, "summary": new_s}
            except GoveeAPIError as e:
                self._control_fail_streak += 1
                if not skip_emergency:
                    await self._try_emergency_fallback()
                return {"device": dev_id, "ok": False, "error": str(e)}
            except Exception as e:
                return {"device": dev_id, "ok": False, "error": str(e)}

        for dev in devices:
            results.append(await _one(dev))

        if any(r.get("ok") for r in results):
            self._control_fail_streak = 0

        if self.cfg.get("analytics_enabled"):
            self._bump_analytics("controls", len(results))
        ok = all(r.get("ok") for r in results)
        return {"result": "ok" if ok else "partial", "details": results}

    def _log_activity(self, device_id: str, *, action: str, details: dict[str, Any]) -> None:
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            with open(self._activity_path, "a", encoding="utf-8") as f:
                f.write(
                    json.dumps(
                        {"ts": datetime.utcnow().isoformat(), "device": device_id, "action": action, "details": details},
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        except Exception:
            pass

    def _bump_analytics(self, key: str, n: int = 1) -> None:
        try:
            cur: dict[str, Any] = {}
            if self._analytics_path.exists():
                with open(self._analytics_path, "r", encoding="utf-8") as f:
                    cur = json.load(f) or {}
            cur[key] = int(cur.get(key) or 0) + n
            with open(self._analytics_path, "w", encoding="utf-8") as f:
                json.dump(cur, f, indent=2)
        except Exception:
            pass

    def prompt_context(self) -> str:
        self.reload_cfg(self._host_cfg)
        lines = [
            "### Govee smart devices (live summary)",
            f"Cached devices: {len(self.store.devices)}. Use `listGoveeDevices` before ambiguous commands.",
            f"HTTP pacing: all requests ≥{self.cfg.get('min_command_interval_ms')}ms; "
            f"POST /device/control (light changes) ≥{self.cfg.get('light_control_min_interval_ms')}ms.",
            "Scene favorites: set `favorites_path` JSON map (shortcut name → Govee scene name).",
            f"Music→lights sync: {'on' if (self.cfg.get('music_sync') or {}).get('enabled') else 'off'} — "
            f"configure `plugins.govee.music_sync`.",
            "Restriction flags:",
            f"  block_scene_changes={self.cfg.get('block_scene_changes')}, block_power_off={self.cfg.get('block_power_off')}, "
            f"block_brightness={self.cfg.get('block_brightness_changes')}, block_color={self.cfg.get('block_color_changes')}",
        ]
        for did, dev in list(self.store.devices.items())[:40]:
            s = dev.get("summary") or {}
            caps = []
            if self.store.capability_supports(dev, "devices.capabilities.color_setting", "colorRgb"):
                caps.append("RGB")
            if self.store.capability_supports(dev, "devices.capabilities.dynamic_scene", "lightScene"):
                caps.append("dynamic_scene")
            lines.append(
                f"- {dev.get('deviceName')} ({did}): online={s.get('online')} power={s.get('power')} "
                f"brightness={s.get('brightness')} scene={s.get('scene_effect')} supports=[{','.join(caps)}]"
            )
        if len(self.store.devices) > 40:
            lines.append(f"... plus {len(self.store.devices) - 40} more (ask for a refresh or search).")
        return "\n".join(lines)

    async def apply_room_preset(self, preset_name: str, *, source: str = "AI") -> dict[str, Any]:
        presets = self.cfg.get("room_presets") or {}
        if preset_name not in presets:
            return {"result": "error", "message": f"unknown preset '{preset_name}'"}
        spec = presets[preset_name] or {}
        group = spec.get("group")
        query = spec.get("device_query")
        devices, err = self._match_devices(query, group, bool(spec.get("all_devices")))
        if err:
            return {"result": "error", "message": err}
        return await self.control_targets(
            devices,
            power_on=spec.get("power_on"),
            brightness=spec.get("brightness"),
            color_name=spec.get("color_name"),
            hex_color=spec.get("hex_color"),
            kelvin=spec.get("kelvin"),
            scene_name=spec.get("scene_name"),
            source=source,
            admin_override=bool(spec.get("admin_override")),
            user_confirmed_risk=bool(spec.get("user_confirmed_risk")),
        )
