"""Safety rules, allowlists, blocked colors/scenes."""

from __future__ import annotations

import re
from typing import Any

_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{6})$")


def _parse_hex(h: str) -> tuple[int, int, int] | None:
    m = _HEX_RE.match(h.strip())
    if not m:
        return None
    v = int(m.group(1), 16)
    return (v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF


def _rgb_int_to_tuple(n: int) -> tuple[int, int, int]:
    n = int(n)
    return (n >> 16) & 0xFF, (n >> 8) & 0xFF, n & 0xFF


def _dist_sq(a: tuple[int, int, int], b: tuple[int, int, int]) -> int:
    return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2


class RestrictionsEngine:
    def __init__(self, cfg: dict[str, Any], color_db: dict[str, dict[str, Any]], scene_db: dict[str, Any]):
        self.cfg = cfg
        self.color_db = color_db
        self.scene_db = scene_db

    def reload_cfg(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg

    def device_allowed(self, device_id: str, device_name: str) -> tuple[bool, str | None]:
        allowed = self.cfg.get("allowed_devices") or []
        blocked = self.cfg.get("blocked_devices") or []
        if blocked:
            bl = {str(x).lower() for x in blocked}
            if device_id.lower() in bl or device_name.lower() in bl:
                return False, "device is blocked by configuration"
        if allowed:
            al = {str(x).lower() for x in allowed}
            ok = device_id.lower() in al or device_name.lower() in al
            if not ok:
                return False, "device is not in allowed_devices list"
        return True, None

    def is_group_blocked(self, group_name: str) -> tuple[bool, str | None]:
        blocked = {str(x).lower() for x in (self.cfg.get("blocked_groups") or [])}
        if group_name.strip().lower() in blocked:
            return True, f"group '{group_name}' is blocked"
        rr = self.cfg.get("room_restrictions") or {}
        if isinstance(rr, dict):
            rule = rr.get(group_name)
            if rule == "block" or rule is False:
                return True, f"group '{group_name}' is restricted by room_restrictions"
        return False, None

    def merge_device_permissions(self, device_id: str) -> dict[str, Any]:
        per = self.cfg.get("device_permissions") or {}
        return per.get(device_id) or per.get(device_id.upper()) or {}

    def check_color_blocked(self, rgb_int: int) -> tuple[bool, str | None]:
        t = _rgb_int_to_tuple(rgb_int)
        blocks = self.cfg.get("blocked_colors") or []
        tol_def = int(self.cfg.get("color_tolerance_default") or 0)
        if not isinstance(blocks, list):
            return False, None
        for rule in blocks:
            if not isinstance(rule, dict):
                if isinstance(rule, str):
                    key = rule.lower()
                    if key in self.color_db:
                        hx = self.color_db[key].get("hex")
                        if hx:
                            tt = _parse_hex(str(hx))
                            if tt and (tol_def <= 0 and t == tt):
                                return True, f"color matches blocked name '{rule}'"
                            if tt and tol_def > 0 and _dist_sq(t, tt) <= tol_def * tol_def * 3:
                                return True, f"color matches blocked name '{rule}' (tolerance)"
                continue
            kind = str(rule.get("type") or rule.get("kind") or "").lower()
            tol = int(rule.get("tolerance", tol_def) or 0)
            if kind in ("hex", ""):
                hx = rule.get("hex") or rule.get("value")
                if hx:
                    tt = _parse_hex(str(hx))
                    if tt:
                        if tol <= 0 and t == tt:
                            return True, f"blocked hex {hx}"
                        if tol > 0 and _dist_sq(t, tt) <= tol * tol * 3:
                            return True, f"blocked hex {hx} (within tolerance)"
            if kind == "rgb" or "r" in rule:
                try:
                    r, g, b = int(rule["r"]), int(rule["g"]), int(rule["b"])
                    tt = (r, g, b)
                    if tol <= 0 and t == tt:
                        return True, "blocked RGB"
                    if tol > 0 and _dist_sq(t, tt) <= tol * tol * 3:
                        return True, "blocked RGB (within tolerance)"
                except Exception:
                    pass
            if kind == "name" or rule.get("name"):
                nm = str(rule.get("name", "")).lower().strip()
                entry = self.color_db.get(nm)
                if entry:
                    hx = entry.get("hex")
                    if hx:
                        tt = _parse_hex(str(hx))
                        if tt:
                            thr = tol if tol > 0 else 0
                            if thr <= 0 and t == tt:
                                return True, f"blocked color name '{nm}'"
                            if thr > 0 and _dist_sq(t, tt) <= thr * thr * 3:
                                return True, f"blocked color name '{nm}' (within tolerance)"
        return False, None

    def check_scene_blocked(self, scene_name: str, *, admin_override: bool) -> tuple[bool, str | None]:
        if admin_override and bool(self.cfg.get("admin_override_param")):
            return False, None
        name_l = scene_name.strip().lower()
        blocked_names = {str(x).lower() for x in (self.cfg.get("blocked_scenes") or [])}
        if name_l in blocked_names:
            return True, f"scene '{scene_name}' is blocked"
        blocked_cats = [str(x).lower() for x in (self.cfg.get("blocked_scene_categories") or [])]
        cats = self.scene_db.get("categories") or {}
        if isinstance(cats, dict) and blocked_cats:
            for cat_key, members in cats.items():
                if cat_key.lower() not in blocked_cats:
                    continue
                members_l = {str(m).lower() for m in (members or [])}
                if name_l in members_l:
                    return True, f"scene category '{cat_key}' is blocked"
        return False, None
