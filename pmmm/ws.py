"""Authenticated WebSocket client with reconnect and subscription-format fallback."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Callable

import websockets

from .auth import Signer
from .util import camelize

log = logging.getLogger(__name__)

SUB_ENUM_INT = {
    "/v1/ws/private": {"SUBSCRIPTION_TYPE_ORDER": 1, "SUBSCRIPTION_TYPE_POSITION": 3,
                       "SUBSCRIPTION_TYPE_ACCOUNT_BALANCE": 4},
    "/v1/ws/markets": {"SUBSCRIPTION_TYPE_MARKET_DATA": 1, "SUBSCRIPTION_TYPE_MARKET_DATA_LITE": 2,
                       "SUBSCRIPTION_TYPE_TRADE": 3},
}


class WsClient:
    def __init__(self, base: str, path: str, signer: Signer, subs: list[tuple[str, list[str]]],
                 on_message: Callable[[dict], None], on_state: Callable[[bool], None] | None = None,
                 name: str = "ws"):
        self.url = base.rstrip("/") + path
        self.path = path
        self.signer = signer
        self.subs = subs  # [(subscription_type, market_slugs)]
        self.on_message = on_message
        self.on_state = on_state or (lambda up: None)
        self.name = name
        self.connected = False
        self.last_msg = 0.0
        self._ws = None
        self._fallback_sent: set[str] = set()

    def _sub_msg(self, i: int, typ: str, slugs: list[str], snake: bool) -> dict:
        rid = f"{self.name}-{i}"
        if snake:
            body = {"request_id": rid, "subscription_type": SUB_ENUM_INT[self.path][typ]}
            if slugs:
                body["market_slugs"] = slugs
        else:
            body = {"requestId": rid, "subscriptionType": typ}
            if slugs:
                body["marketSlugs"] = slugs
        return {"subscribe": body}

    async def run(self, stop: asyncio.Event) -> None:
        backoff = 1.0
        while not stop.is_set():
            try:
                headers = self.signer.headers("GET", self.path)
                async with websockets.connect(self.url, additional_headers=headers, ping_interval=5,
                                              ping_timeout=5, open_timeout=10, close_timeout=3,
                                              max_size=2 ** 23) as ws:
                    self._ws = ws
                    self.connected = True
                    self._fallback_sent.clear()
                    backoff = 1.0
                    log.info("%s connected", self.name)
                    self.on_state(True)
                    for i, (typ, slugs) in enumerate(self.subs):
                        await ws.send(json.dumps(self._sub_msg(i, typ, slugs, snake=False)))
                    stop_task = asyncio.ensure_future(stop.wait())
                    try:
                        while True:
                            recv = asyncio.ensure_future(ws.recv())
                            done, _ = await asyncio.wait({recv, stop_task}, return_when=asyncio.FIRST_COMPLETED)
                            if stop_task in done:
                                recv.cancel()
                                return
                            raw = recv.result()
                            self.last_msg = time.time()
                            try:
                                msg = camelize(json.loads(raw))
                            except ValueError:
                                log.warning("%s non-JSON message: %r", self.name, raw[:200])
                                continue
                            if "heartbeat" in msg:
                                continue
                            if msg.get("error"):
                                await self._handle_error(ws, msg)
                                continue
                            try:
                                self.on_message(msg)
                            except Exception:
                                log.exception("%s handler error", self.name)
                    finally:
                        stop_task.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("%s disconnected: %s: %s", self.name, type(e).__name__, e)
            finally:
                if self.connected:
                    self.connected = False
                    self.on_state(False)
                self._ws = None
            if stop.is_set():
                return
            try:
                await asyncio.wait_for(stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 30.0)

    async def _handle_error(self, ws, msg: dict) -> None:
        rid = str(msg.get("requestId", ""))
        log.error("%s subscription error %s: %s", self.name, rid, msg.get("error"))
        # Docs show two request styles (camelCase+string enum, snake_case+int). Try the other once.
        if rid.startswith(self.name + "-") and rid not in self._fallback_sent:
            self._fallback_sent.add(rid)
            try:
                i = int(rid.rsplit("-", 1)[1])
                typ, slugs = self.subs[i]
                await ws.send(json.dumps(self._sub_msg(i, typ, slugs, snake=True)))
                log.info("%s retried subscription %s with snake_case format", self.name, rid)
            except Exception:
                log.exception("%s fallback subscribe failed", self.name)
