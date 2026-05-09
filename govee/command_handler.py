"""Non-blocking command queue with optional cooldown between sends."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

_LOG = logging.getLogger(__name__)


class CommandPipeline:
    def __init__(self, cooldown_ms: float = 0):
        self._cooldown_s = max(0.0, float(cooldown_ms)) / 1000.0
        self._last = 0.0
        self._sem = asyncio.Semaphore(32)
        self._tasks: set[asyncio.Task] = set()

    def _touch_cooldown(self) -> None:
        now = time.monotonic()
        self._last = now

    async def _wait_cooldown(self) -> None:
        if self._cooldown_s <= 0:
            return
        now = time.monotonic()
        wait = self._cooldown_s - (now - self._last)
        if wait > 0:
            await asyncio.sleep(wait)

    def fire(
        self,
        coro_factory: Callable[[], Awaitable[Any]],
        *,
        label: str = "",
    ) -> asyncio.Task:
        async def _run():
            async with self._sem:
                await self._wait_cooldown()
                try:
                    await coro_factory()
                except Exception as e:
                    _LOG.error("govee pipeline %s failed: %s", label or "task", e)
                finally:
                    self._touch_cooldown()

        task = asyncio.create_task(_run())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def drain(self) -> None:
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
