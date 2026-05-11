"""Structured debug logging helpers for the Govee plugin."""

from __future__ import annotations

import json
import logging
from typing import Any

_LOG = logging.getLogger(__name__)


def log_json(logger: logging.Logger, level: int, msg: str, payload: Any) -> None:
    if not logger.isEnabledFor(level):
        return
    try:
        if isinstance(payload, (dict, list)):
            body = json.dumps(payload, default=str, ensure_ascii=False)[:4000]
        else:
            body = str(payload)[:4000]
    except Exception:
        body = repr(payload)[:4000]
    logger.log(level, "%s %s", msg, body)
