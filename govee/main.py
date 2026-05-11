"""Govee Home OpenAPI integration for Project Gabriel."""

from __future__ import annotations

import logging

from src.plugins import Plugin, PluginContext

from .controller import GoveeController
from .tools import GoveeTools

logger = logging.getLogger(__name__)


class GoveePlugin(Plugin):
    name = "govee"
    version = "1.0.0"
    description = "Advanced Govee smart-device control via official OpenAPI (lights, scenes, groups, Discord webhooks, safety rules)."
    author = "Sketch494"

    def setup(self, ctx: PluginContext):
        ctx.register_tool(GoveeTools)

        def _prompt() -> str:
            gh = getattr(ctx.tool_handler, "govee", None) if ctx.tool_handler else None
            if gh is None:
                return ""
            try:
                return gh.prompt_context()
            except Exception as e:
                ctx.logger.debug("govee prompt contributor failed: %s", e)
                return ""

        ctx.register_prompt_contributor("govee_devices", _prompt)
        ctx.subscribe("startup", lambda *_a, **_k: self._startup(ctx))

    async def _startup(self, ctx: PluginContext):
        cfg = ctx.plugin_config() or {}
        ctrl = GoveeController(ctx.logger, ctx.plugin_dir, ctx.data_dir(), cfg if isinstance(cfg, dict) else {})
        try:
            await ctrl.start()
        except Exception as e:
            ctx.logger.error("Govee controller failed to start: %s", e, exc_info=True)
            return
        if ctx.tool_handler is None:
            ctx.logger.warning("govee: tool_handler missing, tools will stay inert")
            return
        ctx.tool_handler.govee = ctrl
        if getattr(ctrl, "reactive", None):
            ctrl.reactive.bind_ctx(ctx)
        ctx.logger.info("Govee plugin online (%s devices cached)", len(ctrl.store.devices))

    async def teardown(self, ctx: PluginContext):
        gh = getattr(ctx.tool_handler, "govee", None) if ctx.tool_handler else None
        if gh is not None:
            try:
                await gh.stop()
            except Exception as e:
                ctx.logger.warning("govee teardown: %s", e)
            try:
                ctx.tool_handler.govee = None
            except Exception:
                pass
        ctx.unregister_prompt_contributor("govee_devices")
        ctx.logger.info("govee plugin shut down")


plugin = GoveePlugin()
