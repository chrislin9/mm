"""Thin async REST client for Polymarket US.

Order creation is NEVER retried automatically: a timeout or 5xx leaves the order's fate
unknown, which is surfaced as `Ambiguous` so the engine can treat it as live and clean up.
"""
from __future__ import annotations

import asyncio
import logging
import time
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from .auth import Signer

log = logging.getLogger(__name__)


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message


class AuthError(ApiError):
    pass


class BadRequest(ApiError):
    pass


class NotFound(ApiError):
    pass


class RateLimited(ApiError):
    pass


class ServerError(ApiError):
    pass


class Ambiguous(Exception):
    """Transport failure: we cannot know whether the request took effect."""


class TokenBucket:
    """Rate limiter; priority callers (cancels) may use a reserved slice of the bucket."""

    def __init__(self, rate: float, capacity: float | None = None, reserve: float = 2.0):
        self.rate = rate
        self.capacity = capacity or max(2.0, rate)
        self.reserve = min(reserve, self.capacity - 1)
        self.tokens = self.capacity
        self.ts = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, priority: bool = False) -> None:
        need = 1.0 if priority else 1.0 + self.reserve
        while True:
            async with self._lock:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.ts) * self.rate)
                self.ts = now
                if self.tokens >= need:
                    self.tokens -= 1.0
                    return
                wait = (need - self.tokens) / self.rate
            await asyncio.sleep(wait)


class PolymarketClient:
    def __init__(self, signer: Signer | None, api_base: str, gateway_base: str,
                 rps: float = 8.0, timeout: float = 6.0):
        self.signer = signer
        self.api_base = api_base.rstrip("/")
        self.gateway_base = gateway_base.rstrip("/")
        # Small bursts: Cloudflare in front of both hosts blocks bursts well below the
        # documented per-second limit (observed: ~8 simultaneous requests -> 429 Access denied).
        self.limiter = TokenBucket(rps, capacity=4, reserve=1.5)
        self.gw_limiter = TokenBucket(1.0, capacity=2, reserve=0)
        self.http = httpx.AsyncClient(timeout=timeout, headers={"User-Agent": "pmmm/0.1"})
        self.clock_skew_s: float | None = None  # server - local

    async def close(self) -> None:
        await self.http.aclose()

    async def _request(self, method: str, base: str, path: str, *, params=None, json=None,
                       auth: bool = True, priority: bool = False, retries: int = 0) -> Any:
        attempt = 0
        while True:
            await (self.limiter if auth else self.gw_limiter).acquire(priority)
            headers = {}
            if auth:
                if self.signer is None:
                    raise AuthError(0, "no API credentials configured")
                # Sign the path only (no query string).
                headers = self.signer.headers(method, path)
            try:
                r = await self.http.request(method, base + path, params=params, json=json,
                                            headers=headers)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                if attempt < retries:
                    attempt += 1
                    await asyncio.sleep(0.5 * 2 ** attempt)
                    continue
                raise Ambiguous(f"{method} {path}: {type(e).__name__}: {e}") from e
            self._note_date(r)
            if r.status_code < 300:
                if not r.content:
                    return {}
                try:
                    data = r.json()
                except ValueError:
                    return {}
                if isinstance(data, dict):
                    # Gateway responses are CDN-cached (max-age=30); expose the cache age so
                    # callers never mistake a 30s-old book for a live one.
                    try:
                        data["_age_s"] = float(r.headers.get("age", 0) or 0)
                    except ValueError:
                        data["_age_s"] = 30.0
                return data
            msg = r.text[:500]
            if r.status_code == 429 or r.status_code >= 500:
                if attempt < retries:
                    attempt += 1
                    await asyncio.sleep(1.0 * 2 ** attempt)
                    continue
                if r.status_code == 429:
                    raise RateLimited(429, msg)
                raise ServerError(r.status_code, msg)
            if r.status_code in (401, 403):
                raise AuthError(r.status_code, msg)
            if r.status_code == 404:
                raise NotFound(404, msg)
            raise BadRequest(r.status_code, msg)

    def _note_date(self, r: httpx.Response) -> None:
        d = r.headers.get("date")
        if not d:
            return
        try:
            server = parsedate_to_datetime(d).timestamp()
            self.clock_skew_s = server - time.time()
        except Exception:
            pass

    # ---------------- public market data (gateway, no auth) ----------------
    async def get_event(self, slug: str) -> dict:
        return (await self._request("GET", self.gateway_base, f"/v1/events/slug/{slug}",
                                    auth=False, retries=2)).get("event", {})

    async def get_market(self, slug: str) -> dict:
        return (await self._request("GET", self.gateway_base, f"/v1/market/slug/{slug}",
                                    auth=False, retries=2)).get("market", {})

    async def get_book(self, slug: str) -> dict:
        """Book snapshot; `_age_s` is how old the (CDN-cached) snapshot already is."""
        resp = await self._request("GET", self.gateway_base, f"/v1/markets/{slug}/book",
                                   auth=False, retries=1)
        md = dict(resp.get("marketData") or {})
        md["_age_s"] = resp.get("_age_s", 30.0)
        return md

    # ---------------- authenticated ----------------
    async def open_orders(self) -> list[dict]:
        return (await self._request("GET", self.api_base, "/v1/orders/open", retries=1)).get("orders", []) or []

    async def get_order(self, order_id: str) -> dict:
        return (await self._request("GET", self.api_base, f"/v1/order/{order_id}", retries=1)).get("order", {})

    async def positions(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        cursor = None
        for _ in range(50):
            params = {"limit": 100}
            if cursor:
                params["cursor"] = cursor
            resp = await self._request("GET", self.api_base, "/v1/portfolio/positions",
                                       params=params, retries=1)
            out.update(resp.get("positions") or {})
            cursor = resp.get("nextCursor")
            if resp.get("eof", True) or not cursor:
                return out
        raise ServerError(0, "positions pagination did not terminate")

    async def balances(self) -> dict:
        resp = await self._request("GET", self.api_base, "/v1/account/balances", retries=1)
        for b in resp.get("balances") or []:
            if (b.get("currency") or "USD") == "USD":
                return b
        raise ServerError(0, f"no USD balance in response: {resp}")

    async def activities(self, types: list[str], cursor: str | None = None, limit: int = 100) -> dict:
        params: dict[str, Any] = {"limit": limit, "types": types, "sortOrder": "SORT_ORDER_DESCENDING"}
        if cursor:
            params["cursor"] = cursor
        return await self._request("GET", self.api_base, "/v1/portfolio/activities",
                                   params=params, retries=1)

    async def create_order(self, body: dict) -> dict:
        return await self._request("POST", self.api_base, "/v1/orders", json=body)

    async def preview_order(self, body: dict) -> dict:
        return await self._request("POST", self.api_base, "/v1/order/preview", json={"request": body})

    async def cancel_order(self, order_id: str, market_slug: str) -> dict:
        return await self._request("POST", self.api_base, f"/v1/order/{order_id}/cancel",
                                   json={"marketSlug": market_slug}, priority=True)

    async def cancel_all(self, slugs: list[str]) -> dict:
        return await self._request("POST", self.api_base, "/v1/orders/open/cancel",
                                   json={"slugs": slugs}, priority=True)
