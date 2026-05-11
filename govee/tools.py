"""Gemini tool definitions for Govee Home OpenAPI."""

from __future__ import annotations

import asyncio
from google.genai import types

from src.tools._base import BaseTool


class GoveeTools(BaseTool):
    tool_key = "govee"

    def _ctrl(self):
        return getattr(self.handler, "govee", None)

    def declarations(self, config=None):
        return [
            types.FunctionDeclaration(
                name="listGoveeDevices",
                description=(
                    "List Govee devices cached for this session (names, IDs, capabilities sketch, last known state). "
                    "Call `refresh=true` after the user links new hardware. Use before ambiguous commands like "
                    "\"turn off the bedroom\"."
                ),
                parameters={
                    "type": "OBJECT",
                    "properties": {
                        "refresh": {
                            "type": "BOOLEAN",
                            "description": "If true, pulls fresh device metadata from Govee before responding.",
                        },
                    },
                },
            ),
            types.FunctionDeclaration(
                name="getGoveeDeviceState",
                description="Fetch live state for one device (power, brightness, RGB, kelvin, scenes) from Govee servers.",
                parameters={
                    "type": "OBJECT",
                    "properties": {
                        "deviceQuery": {
                            "type": "STRING",
                            "description": "Substring of the device name in Govee Home, or the full device ID/MAC string.",
                        },
                    },
                    "required": ["deviceQuery"],
                },
            ),
            types.FunctionDeclaration(
                name="controlGoveeDevice",
                description=(
                    "Control one or more Govee devices with fast, combined updates (power, brightness, RGB, "
                    "white-kelvin, scenes). Pass `deviceQuery` and/or `group`. Natural-language requests map here: "
                    "\"dim desk lamp\" → brightness; \"set movie mode\" → scene_name or preset; \"warm white\" → "
                    "color_name or kelvin. Respect restriction flags—surface errors to the user if blocked. "
                    "Set `userConfirmedRisk=true` when the user explicitly confirmed powering off or applying a "
                    "restricted scene if the host requires it."
                ),
                parameters={
                    "type": "OBJECT",
                    "properties": {
                        "deviceQuery": {
                            "type": "STRING",
                            "description": "Matches device name substring or ID. Ignored if `group` is set.",
                        },
                        "group": {
                            "type": "STRING",
                            "description": "Named group from `device_groups` in config (e.g. bedroom, desk).",
                        },
                        "selectAllDevices": {
                            "type": "BOOLEAN",
                            "description": "If true, applies to every cached device (dangerous—require explicit user intent).",
                        },
                        "powerOn": {
                            "type": "BOOLEAN",
                            "description": "True turns on, false turns off, omit to leave unchanged.",
                        },
                        "brightness": {
                            "type": "INTEGER",
                            "description": "1-100 percent for lights that support brightness.",
                        },
                        "colorName": {
                            "type": "STRING",
                            "description": "Named color from the plugin palette (e.g. warm white, ice cyan, neon pink).",
                        },
                        "hexColor": {
                            "type": "STRING",
                            "description": "RRGGBB or #RRGGBB.",
                        },
                        "rgbString": {
                            "type": "STRING",
                            "description": "Comma or space separated R,G,B (0-255).",
                        },
                        "kelvin": {
                            "type": "INTEGER",
                            "description": "Correlated color temperature when supported (Kelvin).",
                        },
                        "sceneName": {
                            "type": "STRING",
                            "description": "Scene/effect name as shown in Govee (static or dynamic).",
                        },
                        "adminOverride": {
                            "type": "BOOLEAN",
                            "description": "Operator-only bypass for blocked scenes when `admin_override_param` is enabled in config.",
                        },
                        "userConfirmedRisk": {
                            "type": "BOOLEAN",
                            "description": "Set true after the human explicitly confirms destructive changes.",
                        },
                        "source": {
                            "type": "STRING",
                            "description": "Audit label for Discord webhooks (default AI).",
                        },
                    },
                },
            ),
            types.FunctionDeclaration(
                name="controlGoveeDevicesBulk",
                description=(
                    "Queue multiple independent Govee commands without waiting for each to finish sequentially from "
                    "the model's perspective—each action is scheduled on the host command pipeline for rapid lighting "
                    "sync (e.g. chase, simple flash patterns)."
                ),
                parameters={
                    "type": "OBJECT",
                    "properties": {
                        "actions": {
                            "type": "ARRAY",
                            "description": "Each entry mirrors `controlGoveeDevice` fields.",
                            "items": {
                                "type": "OBJECT",
                                "properties": {
                                    "deviceQuery": {"type": "STRING"},
                                    "group": {"type": "STRING"},
                                    "selectAllDevices": {"type": "BOOLEAN"},
                                    "powerOn": {"type": "BOOLEAN"},
                                    "brightness": {"type": "INTEGER"},
                                    "colorName": {"type": "STRING"},
                                    "hexColor": {"type": "STRING"},
                                    "rgbString": {"type": "STRING"},
                                    "kelvin": {"type": "INTEGER"},
                                    "sceneName": {"type": "STRING"},
                                    "delayMsBefore": {"type": "INTEGER"},
                                    "adminOverride": {"type": "BOOLEAN"},
                                    "userConfirmedRisk": {"type": "BOOLEAN"},
                                },
                            },
                        },
                    },
                    "required": ["actions"],
                },
            ),
            types.FunctionDeclaration(
                name="applyGoveeRoomPreset",
                description="Apply a predefined room preset from `room_presets` in plugin configuration (movie mode, bedtime, etc.).",
                parameters={
                    "type": "OBJECT",
                    "properties": {
                        "presetName": {"type": "STRING", "description": "Key inside `room_presets`."},
                    },
                    "required": ["presetName"],
                },
            ),
        ]

    async def handle(self, name, args):
        ctrl = self._ctrl()
        if ctrl is None:
            return {"result": "error", "message": "Govee plugin is not running (missing API keys or startup failed)."}
        args = args or {}

        if name == "listGoveeDevices":
            if args.get("refresh"):
                await ctrl.refresh_devices()
            devices = []
            for d in ctrl.store.devices.values():
                s = d.get("summary") or {}
                devices.append({
                    "device": d.get("device"),
                    "sku": d.get("sku"),
                    "name": d.get("deviceName"),
                    "type": d.get("type"),
                    "supportsRgb": ctrl.store.capability_supports(d, "devices.capabilities.color_setting", "colorRgb"),
                    "supportsScene": ctrl.store.capability_supports(d, "devices.capabilities.dynamic_scene", "lightScene"),
                    "summary": s,
                })
            rl = ctrl.cfg.get("reactive_lighting") or {}
            rl_targets = rl.get("targets") if isinstance(rl, dict) else {}
            if not isinstance(rl_targets, dict):
                rl_targets = {}
            return {"result": "ok", "devices": devices, "restrictionSummary": {
                "block_scene_changes": ctrl.cfg.get("block_scene_changes"),
                "block_power_off": ctrl.cfg.get("block_power_off"),
                "block_brightness_changes": ctrl.cfg.get("block_brightness_changes"),
                "block_color_changes": ctrl.cfg.get("block_color_changes"),
                "rateLimitsMs": {
                    "allRequests": ctrl.cfg.get("min_command_interval_ms"),
                    "lightControl": ctrl.cfg.get("light_control_min_interval_ms"),
                },
                "favoritesConfigured": bool(str(ctrl.cfg.get("favorites_path") or "").strip()),
                "favoriteLabels": sorted(ctrl._favorites_map.keys()) if ctrl._favorites_map else [],
                "automationsCount": len(ctrl.cfg.get("automations") or []),
                "emergencyFallbackEnabled": bool(
                    isinstance(ctrl.cfg.get("emergency_fallback"), dict)
                    and ctrl.cfg["emergency_fallback"].get("enabled")
                ),
                "reactiveLighting": {
                    "enabled": bool(isinstance(rl, dict) and rl.get("enabled")),
                    "group": rl_targets.get("group"),
                    "deviceIdsCount": len(rl_targets.get("device_ids") or []),
                },
            }}

        if name == "getGoveeDeviceState":
            q = args.get("deviceQuery") or ""
            found, err = ctrl._match_devices(q, None, False)
            if err or len(found) != 1:
                return {"result": "error", "message": err or "query must match exactly one device"}
            dev = found[0]
            st = await ctrl._pull_state(dev)
            return {"result": "ok", "device": dev.get("device"), "state": st}

        if name == "controlGoveeDevice":
            devices, err = ctrl._match_devices(
                args.get("deviceQuery"),
                args.get("group"),
                bool(args.get("selectAllDevices")),
            )
            if err:
                return {"result": "error", "message": err}
            power = args.get("powerOn")
            if power is not None:
                power = bool(power)
            return await ctrl.control_targets(
                devices,
                power_on=power,
                brightness=args.get("brightness"),
                color_name=args.get("colorName"),
                hex_color=args.get("hexColor"),
                rgb_string=args.get("rgbString"),
                kelvin=args.get("kelvin"),
                scene_name=args.get("sceneName"),
                source=str(args.get("source") or "AI"),
                admin_override=bool(args.get("adminOverride")),
                user_confirmed_risk=bool(args.get("userConfirmedRisk")),
            )

        if name == "controlGoveeDevicesBulk":
            actions = args.get("actions") or []
            if not isinstance(actions, list):
                return {"result": "error", "message": "actions must be a list"}

            async def _delayed(i: int, a: dict):
                delay = int(a.get("delayMsBefore") or 0)
                if delay > 0:
                    await asyncio.sleep(delay / 1000.0)
                devices, err = ctrl._match_devices(
                    a.get("deviceQuery"),
                    a.get("group"),
                    bool(a.get("selectAllDevices")),
                )
                if err:
                    return {"index": i, "result": "error", "message": err}
                p = a.get("powerOn")
                if p is not None:
                    p = bool(p)
                return {
                    "index": i,
                    **await ctrl.control_targets(
                        devices,
                        power_on=p,
                        brightness=a.get("brightness"),
                        color_name=a.get("colorName"),
                        hex_color=a.get("hexColor"),
                        rgb_string=a.get("rgbString"),
                        kelvin=a.get("kelvin"),
                        scene_name=a.get("sceneName"),
                        source=str(a.get("source") or "AI-bulk"),
                        admin_override=bool(a.get("adminOverride")),
                        user_confirmed_risk=bool(a.get("userConfirmedRisk")),
                    ),
                }

            tasks = [_delayed(i, a) for i, a in enumerate(actions) if isinstance(a, dict)]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            out = []
            for r in results:
                if isinstance(r, Exception):
                    out.append({"result": "error", "message": str(r)})
                else:
                    out.append(r)
            return {"result": "ok", "details": out}

        if name == "applyGoveeRoomPreset":
            return await ctrl.apply_room_preset(str(args.get("presetName") or ""))

        return None
