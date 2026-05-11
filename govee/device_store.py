"""In-memory device catalog + persisted cache."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

_LOG = logging.getLogger(__name__)


def _cap_state_map(payload_caps: list[dict[str, Any]]) -> dict[tuple[str, str], Any]:
    out: dict[tuple[str, str], Any] = {}
    for c in payload_caps or []:
        if not isinstance(c, dict):
            continue
        t = c.get("type")
        inst = c.get("instance")
        st = c.get("state") or {}
        if t and inst:
            out[(str(t), str(inst))] = st.get("value")
    return out


def summarize_from_state(device_meta: dict[str, Any], state_payload: dict[str, Any]) -> dict[str, Any]:
    caps = state_payload.get("capabilities") if isinstance(state_payload, dict) else None
    vals = _cap_state_map(caps if isinstance(caps, list) else [])

    online = vals.get(("devices.capabilities.online", "online"))
    power = vals.get(("devices.capabilities.on_off", "powerSwitch"))
    brightness = vals.get(("devices.capabilities.range", "brightness"))
    color_rgb = vals.get(("devices.capabilities.color_setting", "colorRgb"))
    ct = vals.get(("devices.capabilities.color_setting", "colorTemperatureK"))
    scene_light = vals.get(("devices.capabilities.dynamic_scene", "lightScene"))
    scene_diy = vals.get(("devices.capabilities.diy_color_setting", "diyScene"))
    mode_scene = None
    for k, v in vals.items():
        if k[0] == "devices.capabilities.mode" and k[1].endswith("Scene"):
            mode_scene = v
            break

    return {
        "online": bool(online) if online is not None else None,
        "power": power,
        "brightness": brightness,
        "color_rgb_int": color_rgb,
        "color_temperature_k": ct,
        "scene_effect": scene_light or scene_diy or mode_scene,
    }


class DeviceStore:
    def __init__(self, cache_path: Path | None = None):
        self.devices: dict[str, dict[str, Any]] = {}
        self._by_name: dict[str, str] = {}
        self.cache_path = cache_path

    def load_cache(self) -> None:
        if not self.cache_path or not self.cache_path.exists():
            return
        try:
            with open(self.cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            _LOG.warning("govee: devices cache load failed: %s", e)
            return
        if isinstance(data, list):
            self.ingest_api_list(data)
        elif isinstance(data, dict) and isinstance(data.get("devices"), list):
            self.ingest_api_list(data["devices"])

    def save_cache(self) -> None:
        if not self.cache_path:
            return
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.cache_path, "w", encoding="utf-8") as f:
                json.dump(
                    {"devices": list(self.devices.values())},
                    f,
                    indent=2,
                    ensure_ascii=False,
                )
        except Exception as e:
            _LOG.warning("govee: devices cache save failed: %s", e)

    def ingest_api_list(self, rows: list[dict[str, Any]]) -> None:
        self.devices.clear()
        self._by_name.clear()
        for row in rows:
            if not isinstance(row, dict):
                continue
            dev_id = str(row.get("device") or "")
            if not dev_id:
                continue
            name = str(row.get("deviceName") or row.get("device_name") or dev_id)
            rec = {
                "device": dev_id,
                "sku": str(row.get("sku") or ""),
                "deviceName": name,
                "type": str(row.get("type") or ""),
                "capabilities": row.get("capabilities") or [],
                "summary": {},
            }
            self.devices[dev_id] = rec
            self._by_name[name.lower()] = dev_id

    def update_summary(self, device_id: str, summary: dict[str, Any]) -> None:
        rec = self.devices.get(device_id)
        if not rec:
            return
        rec["summary"] = {**rec.get("summary", {}), **summary}

    def find_devices(self, query: str | None, group: str | None, all_devices: bool) -> list[dict[str, Any]]:
        if all_devices or (not query and not group):
            return list(self.devices.values())
        if group:
            # group resolved in controller
            pass
        q = (query or "").strip().lower()
        if not q:
            return list(self.devices.values())
        out: list[dict[str, Any]] = []
        for d in self.devices.values():
            did = str(d.get("device") or "")
            name = str(d.get("deviceName") or "").lower()
            sku = str(d.get("sku") or "").lower()
            if q == did.lower() or q in name or q in sku or q in did.lower().replace(":", ""):
                out.append(d)
        return out

    def capability_supports(self, device: dict[str, Any], cap_type: str, instance: str) -> bool:
        for c in device.get("capabilities") or []:
            if not isinstance(c, dict):
                continue
            if c.get("type") == cap_type and c.get("instance") == instance:
                return True
        return False
