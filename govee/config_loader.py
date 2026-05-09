"""Merge host config, bundled defaults, and optional JSON; hot-reload JSON by mtime."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

_LOG = logging.getLogger(__name__)


def _deep_read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        return data if isinstance(data, dict) else {}
    except Exception as e:
        _LOG.warning("govee: failed to read %s: %s", path, e)
        return {}


def load_merged_config(
    plugin_dir: Path,
    data_dir: Path,
    plugin_config: dict[str, Any],
) -> dict[str, Any]:
    """Merge host config, bundled defaults, and optional JSON; hot-reload JSON by mtime.

    Emergency fallback example::

        emergency_fallback:
          enabled: false
          consecutive_failures: 5
          cooldown_seconds: 300
          source_label: emergency_fallback
          apply:
            group: bedroom
            power_on: true
            brightness: 80
            scene_name: Leisure

    Automations: list of dicts with ``interval_seconds`` (or ``every_seconds``), optional
    ``group`` / ``device_query`` / ``all_devices``, and the same keys as ``control_targets``.

    ``favorites_path``: JSON file (relative to ``data/plugins/govee/``) mapping
    shortcut name → Govee scene name, e.g. ``{"movie_night": "Leisure"}``.
    """
    merged: dict[str, Any] = {
        "api_keys": [],
        "api_key_env": "GOVEE_API_KEY",
        "api_key_env_list": "",
        "discord_webhook": "",
        "discord_webhook_env": "",
        "device_refresh_seconds": 300,
        "state_poll_seconds": 0,
        "command_cooldown_ms": 0,
        "min_command_interval_ms": 30,
        "light_control_min_interval_ms": 45,
        "max_concurrent_requests": 4,
        "default_brightness_min": 1,
        "default_brightness_max": 100,
        "block_scene_changes": False,
        "block_power_off": False,
        "block_brightness_changes": False,
        "block_color_changes": False,
        "blocked_colors": [],
        "blocked_scenes": [],
        "blocked_scene_categories": [],
        "allowed_devices": [],
        "blocked_devices": [],
        "device_groups": {},
        "room_presets": {},
        "room_restrictions": {},
        "blocked_groups": [],
        "device_permissions": {},
        "color_tolerance_default": 12,
        "mqtt_enabled": False,
        "mqtt_host": "mqtt.openapi.govee.com",
        "mqtt_port": 8883,
        "require_confirmation_for_power_off": False,
        "require_confirmation_for_scene": False,
        "admin_override_param": False,
        "emergency_fallback": None,
        "debug": False,
        "automations": [],
        "favorites_path": "",
        "analytics_enabled": True,
        "music_sync": {
            "enabled": False,
            "poll_interval_ms": 120,
            "group": "",
            "device_ids": [],
            "mode": "hue_plus_pulse",
            "hue_period_seconds": 48.0,
            "saturation": 1.0,
            "value": 1.0,
            "brightness_base": 55.0,
            "brightness_swing": 28.0,
            "pulse_hz": 0.85,
            "brightness_min": 8,
            "brightness_max": 100,
            "bypass_color_blocks": True,
        },
    }

    for fname in ("restrictions.json", "colors_extra.json"):
        p = plugin_dir / fname
        if fname == "restrictions.json" and p.exists():
            merged.update(_deep_read_json(p))
        elif fname == "colors_extra.json" and p.exists():
            ex = _deep_read_json(p).get("colors")
            if isinstance(ex, list):
                merged.setdefault("_extra_colors", []).extend(ex)

    data_rest = data_dir / "restrictions.override.json"
    if data_rest.exists():
        merged.update(_deep_read_json(data_rest))

    merged.update({k: v for k, v in (plugin_config or {}).items() if v is not None})
    return merged


class JsonHotReloader:
    """Reload JSON files when their mtime changes."""

    def __init__(self, path: Path, label: str):
        self.path = path
        self.label = label
        self._mtime: float | None = None
        self.data: dict[str, Any] = {}

    def get(self) -> dict[str, Any]:
        if not self.path.exists():
            return self.data
        try:
            m = self.path.stat().st_mtime
        except OSError:
            return self.data
        if self._mtime is not None and m == self._mtime:
            return self.data
        self._mtime = m
        self.data = _deep_read_json(self.path)
        _LOG.debug("govee: hot-reloaded %s from %s", self.label, self.path)
        return self.data
