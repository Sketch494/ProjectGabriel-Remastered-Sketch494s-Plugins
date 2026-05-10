"""Shim TikTokLive's HTTP layer for newer httpx (0.28+) where ``AsyncClient(..., proxies=...)`` was removed."""

from __future__ import annotations

import importlib
import inspect
import logging
from typing import Any

_LOG = logging.getLogger(__name__)
_applied = False


def _async_client_kwargs(self: Any) -> dict[str, Any]:
    """Build only kwargs the installed ``httpx.AsyncClient`` accepts."""
    import httpx

    params = inspect.signature(httpx.AsyncClient.__init__).parameters
    kw: dict[str, Any] = {}
    if "trust_env" in params:
        kw["trust_env"] = self.trust_env
    if "cookies" in params:
        kw["cookies"] = self.cookies
    if "verify" in params:
        kw["verify"] = self.ssl_context
    proxies = getattr(self, "proxies", None)
    if proxies:
        if "proxies" in params:
            kw["proxies"] = proxies
        elif "proxy" in params:
            for v in proxies.values() if isinstance(proxies, dict) else []:
                if v:
                    kw["proxy"] = v
                    break
    return kw


def apply_tiktoklive_httpx_compat() -> None:
    """Replace TikTokHTTPClient methods that hard-code removed httpx kwargs."""
    global _applied
    if _applied:
        return
    try:
        mod = importlib.import_module("TikTokLive.client.httpx")
    except Exception as e:
        _LOG.debug("tiktok_chat httpx_compat: skip (TikTokLive not importable): %s", e)
        return

    import httpx

    from TikTokLive.types import SignatureRateLimitReached

    TikTokHTTPClient = mod.TikTokHTTPClient

    async def __httpx_get_bytes(self, url: str, params: dict | None = None, sign_api: bool = False) -> bytes:
        url = self.update_url(url, params or dict())
        async with httpx.AsyncClient(**_async_client_kwargs(self)) as client:
            response: httpx.Response = await client.get(url, headers=self.headers, timeout=self.timeout)
        if sign_api:
            if response.status_code == 429:
                raise SignatureRateLimitReached(
                    response.headers.get("RateLimit-Reset"),
                    response.headers.get("X-RateLimit-Reset"),
                    "You have hit the rate limit for starting connections. Try again in %s seconds. "
                    "Catch this error & access its attributes (retry_after, reset_time) for data on when you can request next.",
                )
            self.__set_tt_cookies(cookies=response.headers.get("X-Set-TT-Cookie"))
        return response.read()

    async def __httpx_post_json(self, url: str, params: dict, json: dict | None = None) -> dict:
        url = self.update_url(url, params or dict())
        async with httpx.AsyncClient(**_async_client_kwargs(self)) as client:
            response: httpx.Response = await client.post(
                url=url,
                data=json,
                headers=self.headers,
                timeout=self.timeout,
            )
        return response.json()

    async def post_json_to_url(self, url: str, headers: dict, json: dict | None = None) -> dict:
        async with httpx.AsyncClient(**_async_client_kwargs(self)) as client:
            response: httpx.Response = await client.post(
                url=url,
                data=json,
                headers=headers,
                timeout=self.timeout,
            )
        return response.json()

    TikTokHTTPClient.__httpx_get_bytes = __httpx_get_bytes  # type: ignore[method-assign]
    TikTokHTTPClient.__httpx_post_json = __httpx_post_json  # type: ignore[method-assign]
    TikTokHTTPClient.post_json_to_url = post_json_to_url  # type: ignore[method-assign]
    _applied = True
    _LOG.debug("tiktok_chat: patched TikTokLive.client.httpx for httpx %s", getattr(httpx, "__version__", "?"))
