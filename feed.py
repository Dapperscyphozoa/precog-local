"""
feed.py — Hyperliquid WebSocket candle ingestion.

Architecture rule: WebSocket is the ONLY market data source. No REST polling.

HL WS protocol:
  - URL: wss://api.hyperliquid.xyz/ws
  - Subscribe: {"method":"subscribe","subscription":{"type":"candle","coin":"BTC","interval":"1m"}}
  - Receive: {"channel":"candle","data":{"t":<open_ms>,"T":<close_ms>,"s":"BTC","i":"1m","o":"...","c":"...","h":"...","l":"...","v":"...","n":<trades>}}

Single connection, multiplexes 78 subscriptions. Auto-reconnect on disconnect.
On startup, also seeds historical bars via REST so we don't wait 4 hours for 4h candles.
"""

import asyncio
import json
import time
import logging
from collections import defaultdict, deque

try:
    import websockets
except ImportError:
    raise SystemExit("Install: pip install websockets")

try:
    import urllib.request
except ImportError:
    raise SystemExit("urllib.request not available")

log = logging.getLogger("feed")

HL_WS_URL = "wss://api.hyperliquid.xyz/ws"
HL_REST_URL = "https://api.hyperliquid.xyz/info"


class CandleFeed:
    """Live candle stream. Updates _live_candles in-place as bars form/close."""

    def __init__(self, universe, interval="1m", on_candle_close=None):
        self.universe = list(universe)
        self.interval = interval
        self.on_candle_close = on_candle_close  # callable(coin, candle_dict)

        # {coin: deque[candle_dict]}  — candle_dict = {t, o, h, l, c, v, closed}
        self._candles = defaultdict(lambda: deque(maxlen=600))
        self._connected = False
        self._last_msg_ts = 0
        self.stats = {
            'msgs_received': 0,
            'candles_closed': 0,
            'reconnects': 0,
            'errors': 0,
            'forward_fills': 0,
        }

    def get_recent(self, coin, n=100):
        """Return last n candles for coin as list of dicts (oldest first)."""
        c = list(self._candles.get(coin, []))
        return c[-n:] if len(c) >= n else c

    def is_healthy(self):
        """True if we've received a message in the last 30s and connection alive."""
        return self._connected and (time.time() - self._last_msg_ts) < 30

    def status(self):
        return {
            'connected': self._connected,
            'last_msg_age_sec': round(time.time() - self._last_msg_ts, 1) if self._last_msg_ts else None,
            'coins_tracked': len(self._candles),
            'universe_size': len(self.universe),
            **self.stats,
        }

    # ─────────────────────────────────────────────────────────
    # SEED — pull initial bars via REST (one-time per coin)
    # ─────────────────────────────────────────────────────────
    def seed_history(self, n_bars=200):
        """One-time REST fetch to populate buffers before live stream takes over.
        Without this we'd have empty buffers until the first 1m candle closes.
        """
        log.info(f"Seeding {len(self.universe)} coins with {n_bars} bars each (one-time)…")
        end_ms = int(time.time() * 1000)
        # 1m bars: end - n_bars * 60s
        start_ms = end_ms - n_bars * 60 * 1000
        ok = 0
        for coin in self.universe:
            try:
                body = json.dumps({
                    "type": "candleSnapshot",
                    "req": {"coin": coin, "interval": self.interval,
                            "startTime": start_ms, "endTime": end_ms}
                }).encode()
                req = urllib.request.Request(HL_REST_URL, data=body,
                    headers={'Content-Type': 'application/json'})
                with urllib.request.urlopen(req, timeout=8) as r:
                    data = json.loads(r.read())
                for bar in data:
                    self._candles[coin].append({
                        't': int(bar['t']),
                        'o': float(bar['o']),
                        'h': float(bar['h']),
                        'l': float(bar['l']),
                        'c': float(bar['c']),
                        'v': float(bar['v']),
                        'closed': True,
                    })
                ok += 1
                # Be polite to REST during seed
                time.sleep(1.5)
            except Exception as e:
                log.warning(f"seed err {coin}: {str(e)[:80]}")
        log.info(f"Seed complete: {ok}/{len(self.universe)} coins loaded")
        return ok

    # ─────────────────────────────────────────────────────────
    # LIVE STREAM
    # ─────────────────────────────────────────────────────────
    async def run(self):
        """Connect, subscribe, process. Auto-reconnect on disconnect."""
        while True:
            try:
                async with websockets.connect(HL_WS_URL, ping_interval=20, ping_timeout=10) as ws:
                    self._connected = True
                    log.info(f"WS connected to {HL_WS_URL}")
                    # Subscribe to all coins in batches (HL accepts one sub per message)
                    for coin in self.universe:
                        sub = {"method": "subscribe",
                               "subscription": {"type": "candle", "coin": coin,
                                                "interval": self.interval}}
                        await ws.send(json.dumps(sub))
                        await asyncio.sleep(0.05)  # don't slam the WS handshake
                    log.info(f"Subscribed to {len(self.universe)} coins ({self.interval})")
                    # Receive loop
                    async for raw in ws:
                        self._last_msg_ts = time.time()
                        self.stats['msgs_received'] += 1
                        try:
                            msg = json.loads(raw)
                        except Exception:
                            continue
                        if msg.get('channel') != 'candle':
                            continue
                        d = msg.get('data') or {}
                        coin = d.get('s')
                        if not coin:
                            continue
                        # Build candle record
                        try:
                            new_candle = {
                                't': int(d['t']),
                                'o': float(d['o']),
                                'h': float(d['h']),
                                'l': float(d['l']),
                                'c': float(d['c']),
                                'v': float(d['v']),
                                'closed': False,  # in-progress
                            }
                        except (KeyError, TypeError, ValueError):
                            continue
                        buf = self._candles[coin]
                        # 2026-04-25: forward-fill gap detection (spec §3.1).
                        # If new bar's t is more than 1 minute after last known
                        # bar, synthesize the missing minutes by carrying close
                        # forward. Volume = 0 on filled bars to mark them.
                        if buf and buf[-1]['t'] + 60_000 < new_candle['t']:
                            last_close = buf[-1]['c']
                            gap_t = buf[-1]['t'] + 60_000
                            while gap_t < new_candle['t']:
                                buf[-1]['closed'] = True
                                buf.append({
                                    't': gap_t, 'o': last_close, 'h': last_close,
                                    'l': last_close, 'c': last_close, 'v': 0.0,
                                    'closed': True, 'filled': True,
                                })
                                self.stats['candles_closed'] += 1
                                self.stats['forward_fills'] += 1
                                gap_t += 60_000
                        # Last candle in buffer — does it match this t?
                        if buf and buf[-1]['t'] == new_candle['t']:
                            # Update in place (still forming)
                            buf[-1] = new_candle
                        else:
                            # Previous candle closes when a new t arrives
                            if buf:
                                buf[-1]['closed'] = True
                                self.stats['candles_closed'] += 1
                                if self.on_candle_close:
                                    try:
                                        self.on_candle_close(coin, buf[-1])
                                    except Exception as e:
                                        log.warning(f"on_candle_close err {coin}: {e}")
                            buf.append(new_candle)
            except (websockets.ConnectionClosed, OSError, asyncio.TimeoutError) as e:
                self._connected = False
                self.stats['reconnects'] += 1
                self.stats['errors'] += 1
                log.warning(f"WS disconnected: {e} — reconnecting in 3s")
                await asyncio.sleep(3)
            except Exception as e:
                self._connected = False
                self.stats['errors'] += 1
                log.error(f"WS unexpected: {e}")
                await asyncio.sleep(5)
