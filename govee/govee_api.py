"""Async client for Govee OpenAPI v1 (https://openapi.api.govee.com)."""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

import httpx

_LOG = logging.getLogger(__name__)

BASE_URL = "https://openapi.api.govee.com"


class GoveeAPIError(Exception):
    def __init__(self, message: str, code: int | None = None, raw: Any = None):
        super().__init__(message)
        self.code = code
        self.raw = raw


class GoveeAPI:
    def __init__(
        self,
        api_keys: list[str],
        *,
        min_interval_ms: float = 35,
        light_control_min_interval_ms: float | None = None,
        max_retries: int = 3,
        debug: bool = False,
    ):
        keys = [k.strip() for k in api_keys if k and str(k).strip()]
        if not keys:
            raise ValueError("At least one Govee API key is required")
        self._keys = keys
        self._key_index = 0
        self._min_interval = max(0.0, float(min_interval_ms)) / 1000.0
        lc = light_control_min_interval_ms
        if lc is None:
            lc_ms = float(min_interval_ms)
        else:
            lc_ms = float(lc)
        self._light_interval = max(0.0, lc_ms) / 1000.0
        self._last_request_mono: float = 0.0
        self._last_control_mono: float = 0.0
        self._lock = asyncio.Lock()
        self._max_retries = max(1, max_retries)
        self._debug = debug
        self._client: httpx.AsyncClient | None = None

    def set_rate_limits(self, min_interval_ms: float, light_control_min_interval_ms: float | None = None) -> None:
        self._min_interval = max(0.0, float(min_interval_ms)) / 1000.0
        if light_control_min_interval_ms is None:
            lc_ms = float(min_interval_ms)
        else:
            lc_ms = float(light_control_min_interval_ms)
        self._light_interval = max(0.0, lc_ms) / 1000.0

    @property
    def current_key(self) -> str:
        return self._keys[self._key_index % len(self._keys)]

    def rotate_key(self) -> None:
        self._key_index = (self._key_index + 1) % len(self._keys)

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(base_url=BASE_URL, timeout=30.0)
        return self._client

    async def aclose(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def _throttle(self) -> None:
        if self._min_interval <= 0:
            return
        loop = asyncio.get_event_loop()
        now = loop.time()
        wait = self._min_interval - (now - self._last_request_mono)
        if wait > 0:
            await asyncio.sleep(wait)

    async def _throttle_light_change(self) -> None:
        """Extra spacing between POST /device/control calls (lights stay responsive but 429-safe)."""
        if self._light_interval <= 0:
            return
        loop = asyncio.get_event_loop()
        now = loop.time()
        wait = self._light_interval - (now - self._last_control_mono)
        if wait > 0:
            await asyncio.sleep(wait)

    async def _request_once(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        is_light_control: bool = False,
    ) -> dict[str, Any]:
        await self._throttle()
        if is_light_control:
            await self._throttle_light_change()
        client = await self._ensure_client()
        headers = {
            "Govee-API-Key": self.current_key,
            "Content-Type": "application/json",
        }
        if method.upper() == "GET":
            r = await client.get(path, headers=headers)
        else:
            r = await client.request(method.upper(), path, headers=headers, json=json_body)
        loop = asyncio.get_event_loop()
        now = loop.time()
        self._last_request_mono = now
        if is_light_control:
            self._last_control_mono = now

        try:
            data: dict[str, Any] = r.json() if r.content else {}
        except Exception:
            data = {}
        if r.status_code == 429:
            raise GoveeAPIError("rate limited (429)", 429, data)
        if r.status_code >= 400:
            raise GoveeAPIError(
                data.get("message") or r.text or f"HTTP {r.status_code}",
                r.status_code,
                data,
            )
        code = data.get("code")
        if code is not None and int(code) != 200:
            msg = data.get("message") or str(data)
            raise GoveeAPIError(msg, int(code), data)
        if self._debug:
            _LOG.debug("govee api %s %s -> %s", method, path, str(data)[:800])
        return data if isinstance(data, dict) else {}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        is_light_control: bool = False,
    ) -> dict[str, Any]:
        last_err: Exception | None = None
        async with self._lock:
            for attempt in range(self._max_retries):
                try:
                    return await self._request_once(
                        method, path, json_body=json_body, is_light_control=is_light_control
                    )
                except GoveeAPIError as e:
                    last_err = e
                    if e.code == 429:
                        self.rotate_key()
                        await asyncio.sleep(0.35 * (attempt + 1))
                        continue
                    raise
                except httpx.HTTPError as e:
                    last_err = e
                    self.rotate_key()
                    await asyncio.sleep(0.35 * (attempt + 1))
        if last_err:
            raise GoveeAPIError(str(last_err), None, None) from last_err
        raise GoveeAPIError("request failed", None, None)

    async def list_devices(self) -> list[dict[str, Any]]:
        data = await self._request("GET", "/router/api/v1/user/devices")
        devs = data.get("data")
        return devs if isinstance(devs, list) else []

    async def control(
        self,
        sku: str,
        device: str,
        cap_type: str,
        instance: str,
        value: Any,
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        rid = request_id or str(uuid.uuid4())
        body = {
            "requestId": rid,
            "payload": {
                "sku": sku,
                "device": device,
                "capability": {
                    "type": cap_type,
                    "instance": instance,
                    "value": value,
                },
            },
        }
        return await self._request(
            "POST", "/router/api/v1/device/control", json_body=body, is_light_control=True
        )

    async def get_state(self, sku: str, device: str) -> dict[str, Any]:
        body = {
            "requestId": str(uuid.uuid4()),
            "payload": {"sku": sku, "device": device},
        }
        data = await self._request("POST", "/router/api/v1/device/state", json_body=body)
        pl = data.get("payload")
        return pl if isinstance(pl, dict) else {}

    async def get_light_scenes(self, sku: str, device: str) -> dict[str, Any]:
        body = {
            "requestId": str(uuid.uuid4()),
            "payload": {"sku": sku, "device": device},
        }
        data = await self._request("POST", "/router/api/v1/device/scenes", json_body=body)
        pl = data.get("payload")
        return pl if isinstance(pl, dict) else {}

    async def get_diy_scenes(self, sku: str, device: str) -> dict[str, Any]:
        body = {
            "requestId": str(uuid.uuid4()),
            "payload": {"sku": sku, "device": device},
        }
        data = await self._request("POST", "/router/api/v1/device/diy-scenes", json_body=body)
        pl = data.get("payload")
        return pl if isinstance(pl, dict) else {}
