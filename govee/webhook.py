"""Discord webhook notifications for Govee state changes."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx

_LOG = logging.getLogger(__name__)


def _rgb_embed_color(rgb_int: int | None) -> int:
    if rgb_int is None:
        return 0x5865F2
    r = (int(rgb_int) >> 16) & 0xFF
    g = (int(rgb_int) >> 8) & 0xFF
    b = int(rgb_int) & 0xFF
    return (r << 16) | (g << 8) | b


def _fmt_rgb(rgb_int: int | None) -> str:
    if rgb_int is None:
        return "—"
    n = int(rgb_int)
    r, g, b = (n >> 16) & 0xFF, (n >> 8) & 0xFF, n & 0xFF
    return f"#{r:02x}{g:02x}{b:02x} ({r},{g},{b})"


async def post_device_change(
    webhook_url: str,
    *,
    device_name: str,
    action: str,
    previous: dict[str, Any],
    new: dict[str, Any],
    source: str,
    scene_name: str | None = None,
) -> None:
    if not webhook_url:
        return
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    prev_rgb = previous.get("color_rgb_int")
    new_rgb = new.get("color_rgb_int")
    embed = {
        "title": device_name,
        "description": f"**{action}**",
        "color": _rgb_embed_color(new_rgb if new_rgb is not None else prev_rgb),
        "fields": [
            {"name": "Previous", "value": f"power={previous.get('power')} bright={previous.get('brightness')} color={_fmt_rgb(prev_rgb)} scene={previous.get('scene_effect')}", "inline": False},
            {"name": "Now", "value": f"power={new.get('power')} bright={new.get('brightness')} color={_fmt_rgb(new_rgb)} scene={new.get('scene_effect')}", "inline": False},
            {"name": "Brightness", "value": str(new.get("brightness") if new.get("brightness") is not None else previous.get("brightness") or "—"), "inline": True},
            {"name": "Color", "value": _fmt_rgb(new_rgb if new_rgb is not None else prev_rgb), "inline": True},
            {"name": "Scene / effect", "value": scene_name or str(new.get("scene_effect") or previous.get("scene_effect") or "—")[:256], "inline": True},
            {"name": "Source", "value": source[:128], "inline": True},
            {"name": "Time", "value": ts, "inline": True},
        ],
    }
    payload = {"embeds": [embed]}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(webhook_url, json=payload)
            if r.status_code >= 400:
                _LOG.warning("govee discord webhook failed: %s %s", r.status_code, r.text[:200])
    except Exception as e:
        _LOG.warning("govee discord webhook error: %s", e)
