"""Gemini Live tools for the camera plugin.

Surface:
    * openCamera           — start streaming a webcam into the live video feed
    * closeCamera          — stop streaming and release the device
    * captureCameraSnapshot — single frame (optionally pushed into the feed once)
    * listCameras          — probe device indices that respond
    * getCameraStatus      — current state, frame count, etc.

All commands are routed through `tool_handler.camera_stream`, which the
plugin attaches in its `startup` hook. If the plugin failed to init (e.g.
missing `opencv-python`) the tools all return a clean error payload.
"""

from __future__ import annotations

import logging
from typing import Any

from google.genai import types

from src.tools._base import BaseTool

logger = logging.getLogger(__name__)


_OPEN_DESC = (
    "Open the user's webcam and stream JPEG frames into your realtime video "
    "channel — same channel the screen-vision loop uses, so frames just appear "
    "as part of what you 'see'.\n"
    "**Invocation Condition:** Only call when the user explicitly asks you to "
    "look through their camera (or asks to show you something on camera). NEVER "
    "open the camera unprompted. Pair every open with a `closeCamera` once the "
    "user is done. If `userConfirmed` is required by config, set it to true "
    "only after the user has clearly agreed in this conversation."
)

_CLOSE_DESC = (
    "Stop the webcam stream and release the device.\n"
    "**Invocation Condition:** Call as soon as the user is done showing you "
    "things, or any time the camera should be off (privacy, leaving, switching "
    "to screen-only)."
)

_SNAPSHOT_DESC = (
    "Capture a single still frame from a webcam without starting a continuous "
    "stream. Pass `sendToVision: true` to drop that one frame into your "
    "realtime video feed (otherwise the frame is just acknowledged and "
    "discarded).\n"
    "**Invocation Condition:** Use for one-off 'check what's on camera right "
    "now' moments without keeping the camera open."
)

_LIST_DESC = (
    "Enumerate camera device indices that respond on this machine (probes 0..N). "
    "Use this if the user has multiple cameras and you need to pick one before "
    "calling `openCamera`."
)

_STATUS_DESC = (
    "Return whether the camera is currently streaming, which device index, the "
    "frame interval, total frames sent so far, and the auto-close timer."
)


class CameraTools(BaseTool):
    tool_key = "camera"

    # ---------------- declarations ----------------

    def declarations(self, config=None):
        if config is not None:
            if config.get("plugins", "camera", "enabled", default=True) is False:
                return []
        return [
            types.FunctionDeclaration(
                name="openCamera",
                description=_OPEN_DESC,
                parameters={
                    "type": "OBJECT",
                    "properties": {
                        "deviceIndex": {
                            "type": "INTEGER",
                            "description": "OS camera index. 0 is usually the built-in/default camera.",
                        },
                        "frameIntervalMs": {
                            "type": "INTEGER",
                            "description": "Milliseconds between frames. Lower = smoother but more tokens. Default ~1000.",
                        },
                        "maxSize": {
                            "type": "INTEGER",
                            "description": "Pixel cap on the longer image edge. Frames are downscaled to fit. Default ~1024.",
                        },
                        "mirror": {
                            "type": "BOOLEAN",
                            "description": "Flip horizontally so the user sees themselves un-mirrored on selfie cams. Default true.",
                        },
                        "backend": {
                            "type": "STRING",
                            "description": "OpenCV backend hint: 'auto', 'dshow', 'msmf', 'v4l2', 'avfoundation'. Leave unset normally.",
                        },
                        "userConfirmed": {
                            "type": "BOOLEAN",
                            "description": "Set true only after the user clearly agreed to camera access in this conversation. Required when require_user_confirm is on.",
                        },
                    },
                },
            ),
            types.FunctionDeclaration(
                name="closeCamera",
                description=_CLOSE_DESC,
                parameters={"type": "OBJECT", "properties": {}},
            ),
            types.FunctionDeclaration(
                name="captureCameraSnapshot",
                description=_SNAPSHOT_DESC,
                parameters={
                    "type": "OBJECT",
                    "properties": {
                        "deviceIndex": {"type": "INTEGER"},
                        "sendToVision": {
                            "type": "BOOLEAN",
                            "description": "If true, push the frame into the live video channel exactly once.",
                        },
                    },
                },
            ),
            types.FunctionDeclaration(
                name="listCameras",
                description=_LIST_DESC,
                parameters={
                    "type": "OBJECT",
                    "properties": {
                        "maxIndex": {
                            "type": "INTEGER",
                            "description": "Highest device index to probe (inclusive). Defaults to 8.",
                        },
                    },
                },
            ),
            types.FunctionDeclaration(
                name="getCameraStatus",
                description=_STATUS_DESC,
                parameters={"type": "OBJECT", "properties": {}},
            ),
        ]

    # ---------------- dispatch ----------------

    @property
    def _stream(self):
        return getattr(self.handler, "camera_stream", None)

    def _plugin_cfg(self) -> dict[str, Any]:
        cfg = self.config
        if cfg is None:
            return {}
        try:
            return cfg.get("plugins", "camera", default={}) or {}
        except Exception:
            return {}

    async def handle(self, name, args):
        stream = self._stream
        if stream is None and name in {
            "openCamera",
            "closeCamera",
            "captureCameraSnapshot",
            "listCameras",
            "getCameraStatus",
        }:
            return {
                "result": "error",
                "message": "camera plugin is not running (missing opencv-python or startup failed).",
            }
        args = args or {}

        if name == "openCamera":
            require_confirm = bool(self._plugin_cfg().get("require_user_confirm", True))
            if require_confirm and not bool(args.get("userConfirmed")):
                return {
                    "result": "error",
                    "message": "user confirmation is required to open the camera. Ask the user first, then retry with userConfirmed=true.",
                }
            return await stream.start(
                device_index=args.get("deviceIndex"),
                frame_interval_ms=args.get("frameIntervalMs"),
                max_size=args.get("maxSize"),
                mirror=args.get("mirror"),
                backend=args.get("backend"),
            )

        if name == "closeCamera":
            return await stream.stop()

        if name == "captureCameraSnapshot":
            return await stream.snapshot(
                device_index=args.get("deviceIndex"),
                send_to_vision=bool(args.get("sendToVision")),
            )

        if name == "listCameras":
            return await stream.enumerate(max_index=int(args.get("maxIndex") or 8))

        if name == "getCameraStatus":
            return {"result": "ok", **stream.status()}

        return None
