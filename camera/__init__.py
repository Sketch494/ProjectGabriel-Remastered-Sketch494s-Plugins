"""Camera plugin — let the AI open a webcam and stream frames into Gemini Live.

Frames are JPEG-encoded and pushed onto the same `live_session._out_queue` the
screen-capture vision loop uses, so the AI sees them through its existing
realtime video channel without any session-side changes.

Wiring:
  * `setup()` registers `CameraTools` and subscribes to startup/shutdown so the
    `CameraStream` manager is created once `tool_handler` exists.
  * The manager is attached as `tool_handler.camera_stream` so the tools can
    dispatch to it and the teardown hook can stop the loop cleanly.

Permissions:
  * Plugin is disabled by default in `plugin.yml` (the user must explicitly
    enable it).
  * Per-tool toggles live in `config/tools.yml` under `plugin_tools.camera`.
  * Runtime safety: if `plugins.camera.require_user_confirm` is true (default),
    `openCamera` rejects calls without `userConfirmed=true`.
"""

from __future__ import annotations

import logging

from src.plugins import Plugin, PluginContext

from .camera import CameraStream
from .tools import CameraTools

logger = logging.getLogger(__name__)


class CameraPlugin(Plugin):
    name = "camera"
    version = "1.0.0"
    description = (
        "Webcam streaming into the Gemini Live video channel. "
        "Provides openCamera / closeCamera / snapshot / list tools."
    )
    author = "Sketch494"

    def setup(self, ctx: PluginContext):
        ctx.register_tool(CameraTools)
        ctx.subscribe("startup", lambda: self._on_startup(ctx))
        ctx.subscribe("shutdown", lambda: self._on_shutdown(ctx))
        ctx.register_prompt_contributor("camera", lambda: self._prompt_blurb(ctx))

    def _on_startup(self, ctx: PluginContext):
        if ctx.tool_handler is None:
            ctx.logger.warning("camera startup: tool_handler missing, cannot init")
            return
        try:
            cfg = ctx.plugin_config() or {}
            stream = CameraStream(cfg, ctx.tool_handler, ctx.logger)
            ctx.tool_handler.camera_stream = stream
            ctx.logger.info("camera plugin ready")
        except Exception as e:
            ctx.logger.error(f"failed to init CameraStream: {e}", exc_info=True)
            return

        preview_cfg = (cfg.get("preview") or {}) if isinstance(cfg, dict) else {}
        if preview_cfg.get("enabled", True):
            from . import preview_server
            try:
                app_name = "Gabriel"
                if ctx.config is not None:
                    app_name = str(ctx.config.get("app_name", default="Gabriel") or "Gabriel")
                preview_server.start_server(
                    port=int(preview_cfg.get("port", 8768)),
                    fps=int(preview_cfg.get("fps", 15)),
                    app_name=app_name,
                )
            except Exception as e:
                ctx.logger.warning(f"camera preview server failed to start: {e}")

    async def _on_shutdown(self, ctx: PluginContext):
        stream = getattr(ctx.tool_handler, "camera_stream", None) if ctx.tool_handler else None
        if stream is not None:
            try:
                await stream.stop()
            except Exception as e:
                ctx.logger.error(f"camera shutdown stop() failed: {e}")
            try:
                ctx.tool_handler.camera_stream = None
            except Exception:
                pass
        ctx.logger.info("camera plugin shut down")

    async def teardown(self, ctx: PluginContext):
        await self._on_shutdown(ctx)

    @staticmethod
    def _prompt_blurb(ctx: PluginContext) -> str | None:
        cfg = ctx.plugin_config() or {}
        require_confirm = bool(cfg.get("require_user_confirm", True))
        line = (
            "Webcam control: you can call `openCamera` to start streaming the user's "
            "webcam into your video feed, and `closeCamera` to stop. Always close it "
            "as soon as it isn't needed, and never open it without the user asking."
        )
        if require_confirm:
            line += (
                " Pass `userConfirmed: true` only after the user has clearly agreed "
                "in this conversation."
            )
        return line


plugin = CameraPlugin
