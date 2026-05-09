"""Color names, hex parsing, and device query resolution."""

from __future__ import annotations

import re
from typing import Any

_HEX = re.compile(r"^#?([0-9a-fA-F]{6}|[0-9a-fA-F]{3})$")


def hex_to_rgb_int(h: str) -> int | None:
    s = h.strip()
    m = _HEX.match(s)
    if not m:
        return None
    g = m.group(1)
    if len(g) == 3:
        r, gr, b = int(g[0] + g[0], 16), int(g[1] + g[1], 16), int(g[2] + g[2], 16)
    else:
        r, gr, b = int(g[0:2], 16), int(g[2:4], 16), int(g[4:6], 16)
    return (r << 16) | (gr << 8) | b


def parse_rgb_tuple(text: str) -> int | None:
    m = re.match(
        r"^\s*(\d{1,3})\s*[, ]\s*(\d{1,3})\s*[, ]\s*(\d{1,3})\s*$",
        text.strip(),
    )
    if not m:
        return None
    r, g, b = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if max(r, g, b) > 255:
        return None
    return (r << 16) | (g << 8) | b


def resolve_color(
    *,
    color_name: str | None,
    hex_color: str | None,
    rgb_string: str | None,
    color_db: dict[str, dict[str, Any]],
) -> tuple[int | None, str | None]:
    if hex_color:
        v = hex_to_rgb_int(hex_color)
        return v, None if v is not None else "invalid hex color"
    if rgb_string:
        v = parse_rgb_tuple(rgb_string)
        return v, None if v is not None else "invalid rgb triplet"
    if color_name:
        key = color_name.strip().lower()
        row = color_db.get(key)
        if row and row.get("rgb_int") is not None:
            return int(row["rgb_int"]), None
        if row and row.get("hex"):
            v = hex_to_rgb_int(str(row["hex"]))
            if v is not None:
                return v, None
        v = hex_to_rgb_int(color_name)
        if v is not None:
            return v, None
        v = parse_rgb_tuple(color_name)
        if v is not None:
            return v, None
        return None, f"unknown color name '{color_name}'"
    return None, "no color provided"


def build_color_db(plugin_path_colors: dict[str, Any], extras: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    db: dict[str, dict[str, Any]] = {}
    raw = plugin_path_colors.get("colors") if isinstance(plugin_path_colors, dict) else None
    if isinstance(raw, list):
        for row in raw:
            if not isinstance(row, dict):
                continue
            names = row.get("names") or [row.get("name")]
            if not names:
                continue
            rgb_int = row.get("rgb_int")
            if rgb_int is None and row.get("hex"):
                rgb_int = hex_to_rgb_int(str(row["hex"]))
            if rgb_int is None:
                continue
            entry = {**row, "rgb_int": int(rgb_int)}
            for nm in names:
                if nm:
                    db[str(nm).lower()] = entry
    for row in extras or []:
        if not isinstance(row, dict):
            continue
        for nm in row.get("names") or []:
            if nm and row.get("rgb_int") is not None:
                db[str(nm).lower()] = row
    return db


def norm_scene_query(q: str) -> str:
    return " ".join(q.strip().lower().split())
