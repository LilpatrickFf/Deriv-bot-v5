#!/usr/bin/env python3
"""
Deriv Sniper Signal Bot - single-file version for Render deployment.

Activation message: "🚀 DERIV BOT V2 LIVE\nReal Deriv data • Manual trading"

Features:
- Single long-lived Deriv websocket with request/response correlation
- Async concurrent candle fetches per symbol
- Async Telegram sender with simple rate-limiting and retries
- Sniper logic using linear-regression slope for HTF trend + LTF impulse
- Health endpoints and graceful shutdown
- All config via environment variables
"""
import asyncio
import json
import logging
import os
import signal
import time
import uuid
from collections import deque
from datetime import datetime
from typing import Deque, Dict, List, Optional

import aiohttp
import websockets
from aiohttp import web

# Optional: load .env in local dev if python-dotenv is installed
try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass

# --------------------
# Configuration (env)
# --------------------
DERIV_APP_ID = os.getenv("DERIV_APP_ID", "1089")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
SYMBOLS = os.getenv("SYMBOLS", "R_10,R_25,R_50,R_75,R_100").split(",")
M15 = int(os.getenv("M15", "900"))
H1 = int(os.getenv("H1", "3600"))
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "60"))
HEARTBEAT_INTERVAL = int(os.getenv("HEARTBEAT_INTERVAL", "7200"))
MAX_CONCURRENT_FETCH = int(os.getenv("MAX_CONCURRENT_FETCH", "6"))
PORT = int(os.getenv("PORT", "8080"))
TELEGRAM_MAX_PER_MINUTE = int(os.getenv("TELEGRAM_MAX_PER_MINUTE", "20"))

# Safety checks
if not TELEGRAM_TOKEN or not CHAT_ID:
    raise SystemExit("TELEGRAM_TOKEN and CHAT_ID must be set as environment variables")

# --------------------
# Logging
# --------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("sniper_singlefile")

# --------------------
# Deriv client
# --------------------
class DerivClient:
    """Async single long-lived websocket client for Deriv (binaryws)."""

    def __init__(self, app_id: str, reconnect_backoff_base: float = 1.0):
        self.app_id = app_id
        self.ws_url = f"wss://ws.binaryws.com/websockets/v3?app_id={app_id}"
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._recv_task: Optional[asyncio.Task] = None
        self._futures: Dict[str, asyncio.Future] = {}
        self._lock = asyncio.Lock()
        self._closing = False
        self._reconnect_base = reconnect_backoff_base
        self._connected_event = asyncio.Event()

    async def connect(self):
        backoff = self._reconnect_base
        while not self._closing:
            try:
                logger.info("Connecting to Deriv: %s", self.ws_url)
                self._ws = await websockets.connect(self.ws_url, ping_interval=30, ping_timeout=10)
                self._recv_task = asyncio.create_task(self._reader())
                self._connected_event.set()
                logger.info("Connected to Deriv websocket")
                return
            except Exception as exc:
                logger.warning("Deriv connect failed: %s — retrying in %.1fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    async def _ensure_connected(self):
        if self._ws is None or self._ws.closed:
            await self.connect()

    async def close(self):
        self._closing = True
        if self._recv_task:
            self._recv_task.cancel()
            try:
                await self._recv_task
            except Exception:
                pass
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass

    async def _reader(self):
        assert self._ws is not None
        try:
            async for message in self._ws:
                try:
                    data = json.loads(message)
                except Exception:
                    logger.exception("Invalid JSON from Deriv")
                    continue
                req_id = None
                if isinstance(data, dict):
                    echo = data.get("echo_req") or {}
                    req_id = echo.get("req_id") or data.get("req_id")
                if req_id and req_id in self._futures:
                    fut = self._futures.pop(req_id)
                    if not fut.done():
                        fut.set_result(data)
                else:
                    logger.debug("Unsolicited/unknown message: %s", data)
        except asyncio.CancelledError:
            logger.info("Deriv reader task cancelled")
        except Exception:
            logger.exception("Error in Deriv reader; closing connection and cancelling outstanding requests")
        finally:
            # Cancel outstanding futures
            for k, fut in list(self._futures.items()):
                if not fut.done():
                    fut.set_exception(ConnectionError("Deriv connection lost"))
                self._futures.pop(k, None)
            self._connected_event.clear()
            try:
                if self._ws and not self._ws.closed:
                    await self._ws.close()
            except Exception:
                pass
            self._ws = None

    async def _request(self, payload: dict, timeout: float = 15.0) -> dict:
        async with self._lock:
            await self._ensure_connected()
            if self._ws is None:
                raise ConnectionError("Failed to connect to Deriv")

            req_id = uuid.uuid4().hex
            payload = dict(payload)
            payload.setdefault("echo_req", {})
            payload["echo_req"]["req_id"] = req_id

            fut = asyncio.get_event_loop().create_future()
            self._futures[req_id] = fut

            try:
                await self._ws.send(json.dumps(payload))
            except Exception:
                self._futures.pop(req_id, None)
                raise

        try:
            resp = await asyncio.wait_for(fut, timeout=timeout)
            return resp
        except Exception:
            self._futures.pop(req_id, None)
            raise

    async def fetch_candles(self, symbol: str, granularity: int, count: int = 100, timeout: float = 20.0) -> list:
        payload = {
            "ticks_history": symbol,
            "style": "candles",
            "granularity": granularity,
            "count": count,
            "end": "latest",
        }
        retry = 0
        backoff = self._reconnect_base
        while True:
            try:
                resp = await self._request(payload, timeout=timeout)
                if "error" in resp:
                    logger.warning("Deriv error for %s: %s", symbol, resp["error"])
                    return []
                candles = resp.get("history", {}).get("candles", [])
                return candles or []
            except (ConnectionError, websockets.exceptions.ConnectionClosed):
                logger.info("Connection lost, reconnecting before fetching %s", symbol)
                await self.connect()
            except asyncio.TimeoutError:
                retry += 1
                logger.warning("Timeout fetching candles for %s (try %d), backing off %.1fs", symbol, retry, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
                if retry >= 3:
                    return []
            except Exception:
                logger.exception("Unexpected error fetching candles for %s", symbol)
                return []

# --------------------
# Telegram sender
# --------------------
class TelegramSender:
    """
    Async Telegram sender with sliding-window rate limit.
    """

    def __init__(self, bot_token: str, chat_id: str, max_per_minute: int = 20):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self._session = aiohttp.ClientSession()
        self._timestamps: Deque[float] = deque()
        self._max_per_minute = max_per_minute
        self._lock = asyncio.Lock()

    async def close(self):
        await self._session.close()

    async def _enforce_rate_limit(self):
        async with self._lock:
            now = time.monotonic()
            window = 60.0
            while self._timestamps and now - self._timestamps[0] > window:
                self._timestamps.popleft()
            if len(self._timestamps) >= self._max_per_minute:
                earliest = self._timestamps[0]
                wait = window - (now - earliest) + 0.1
                logger.info("Telegram rate limit reached; sleeping %.2fs", wait)
                await asyncio.sleep(wait)
            self._timestamps.append(time.monotonic())

    async def send(self, text: str, parse_mode: str = "Markdown", tries: int = 3) -> bool:
        await self._enforce_rate_limit()
        payload = {"chat_id": self.chat_id, "text": text, "parse_mode": parse_mode}
        backoff = 1.0
        for attempt in range(1, tries + 1):
            try:
                async with self._session.post(self.api_url, data=payload, timeout=10) as resp:
                    txt = await resp.text()
                    if resp.status == 200:
                        return True
                    else:
                        logger.warning("Telegram returned %s: %s", resp.status, txt)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Telegram send failed (attempt %d)", attempt)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
        return False

# --------------------
# Sniper logic
# --------------------
def _linear_regression_slope(values: List[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    x_mean = (n - 1) / 2.0
    y_mean = sum(values) / n
    num = 0.0
    den = 0.0
    for i, v in enumerate(values):
        xi = i
        num += (xi - x_mean) * (v - y_mean)
        den += (xi - x_mean) ** 2
    if den == 0:
        return 0.0
    return num / den

def sniper_logic(m15: List[dict], h1: List[dict]) -> Optional[str]:
    if len(m15) < 10 or len(h1) < 10:
        return None
    try:
        h_recent = h1[-6:]
        h_highs = [float(c["high"]) for c in h_recent]
        slope = _linear_regression_slope(h_highs)
        trend = "BUY" if slope > 0 else "SELL"

        last = m15[-1]
        open_p = float(last["open"])
        close_p = float(last["close"])
        high_p = float(last["high"])
        low_p = float(last["low"])

        body = close_p - open_p
        abs_body = abs(body)
        wick = high_p - low_p
        if wick <= 0:
            return None
        if abs_body / wick < 0.4:
            return None

        direction = "BUY" if body > 0 else "SELL"
        if direction != trend:
            return None

        if abs_body < max(0.0001 * close_p, 0.00001):
            return None

        return direction
    except Exception:
        return None

# --------------------
# Main app
# --------------------
deriv = DerivClient(app_id=DERIV_APP_ID)
telegram = TelegramSender(bot_token=TELEGRAM_TOKEN, chat_id=CHAT_ID, max_per_minute=TELEGRAM_MAX_PER_MINUTE)
should_exit = False

async def scan_once(symbols: List[str], semaphore: asyncio.Semaphore) -> bool:
    tasks = []
    async def handle_symbol(sym: str):
        async with semaphore:
            m15_task = asyncio.create_task(deriv.fetch_candles(sym, M15, 120))
            h1_task = asyncio.create_task(deriv.fetch_candles(sym, H1, 120))
            m15 = await m15_task
            h1 = await h1_task
            return sym, m15, h1

    for s in symbols:
        tasks.append(asyncio.create_task(handle_symbol(s)))

    for fut in asyncio.as_completed(tasks):
        try:
            sym, m15, h1 = await fut
        except asyncio.CancelledError:
            continue
        except Exception:
            logger.exception("Error fetching symbol data")
            continue

        sig = sniper_logic(m15, h1)
        if sig:
            entry = None
            try:
                entry = float(m15[-1]["close"])
            except Exception:
                entry = None
            msg = (
                f"🎯 *SNIPER SIGNAL*\n"
                f"Symbol: `{sym}`\n"
                f"Direction: *{sig}*\n"
                f"Entry: `{entry}`\n"
                f"TFs: H1 ➝ M15\n"
                f"RR: 1:5 (manual)\n"
                f"Time: {datetime.utcnow().isoformat()}Z"
            )
            await telegram.send(msg)
            for t in tasks:
                if not t.done():
                    t.cancel()
            return True
    return False

async def heartbeat_task():
    while not should_exit:
        await asyncio.sleep(HEARTBEAT_INTERVAL)
        await telegram.send(f"📡 HEARTBEAT\nStatus: ACTIVE\nTime: {datetime.utcnow().isoformat()}Z")

async def scanner_loop():
    sem = asyncio.Semaphore(MAX_CONCURRENT_FETCH)
    # Activation message is the requested text
    await telegram.send("🚀 DERIV BOT V2 LIVE\nReal Deriv data • Manual trading")
    logger.info("Starting scanner loop for symbols: %s", ",".join(SYMBOLS))
    while not should_exit:
        try:
            found = await scan_once(SYMBOLS, sem)
            if not found:
                logger.debug("No signal in this scan")
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Error during scan loop")
        await asyncio.sleep(SCAN_INTERVAL)

# HTTP handlers
async def handle_root(request):
    return web.Response(text="Sniper bot running")

async def handle_ping(request):
    return web.Response(text="alive")

async def start_background_tasks(app):
    app["scanner"] = asyncio.create_task(scanner_loop())
    app["heartbeat"] = asyncio.create_task(heartbeat_task())

async def cleanup_background_tasks(app):
    app["scanner"].cancel()
    app["heartbeat"].cancel()
    await telegram.close()
    await deriv.close()

def _setup_signal_handlers(loop):
    def _stop():
        nonlocal should_exit
        logger.info("Received stop signal, shutting down")
        globals()["should_exit"] = True

    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _stop)
    except NotImplementedError:
        signal.signal(signal.SIGINT, lambda *_: _stop())
        signal.signal(signal.SIGTERM, lambda *_: _stop())

def main():
    loop = asyncio.get_event_loop()
    _setup_signal_handlers(loop)
    app = web.Application()
    app.add_routes([web.get("/", handle_root), web.get("/ping", handle_ping)])
    app.on_startup.append(lambda app: start_background_tasks(app))
    app.on_cleanup.append(lambda app: cleanup_background_tasks(app))

    logger.info("Attempting initial connection to Deriv...")
    loop.run_until_complete(deriv.connect())

    logger.info("Starting web server on port %d", PORT)
    web.run_app(app, host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    main()