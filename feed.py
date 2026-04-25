"""
feed.py — Hyperliquid WebSocket candle ingestion.

Architecture rule: WebSocket is the ONLY market data source. No REST polling.

HL WS protocol:
  - URL: wss://api.hyperliquid.xyz/ws
  - Subscribe: {"method":"subscribe","subscription":{"type":"candle","coin":"BTC","interval":"1m"}}
  - Receive: {"channel":"candle","data":{"t":<open_ms>,"T":<close_ms>,"s":"BTC","i":"1m","o":"...","c":"...","h":"...","l":"...","v":"...","n":<trades>}}

Multi-connection design: HL drops sockets when subscribed to too many channels
on one connection. We split the universe across N parallel connections
(default 4 → ~20 subs per socket).

On startup, seeds 1m bars (live-feed catch-up) AND 15m bars (so signal
evaluation can run immediately without waiting hours of 15m candles to form).
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

import urllib.request

log = logging.getLogger("feed")

HL_WS_URL = "wss://api.hyperliquid.xyz/ws"
HL_REST_URL = "https://api.hyperliquid.xyz/info"

# How many parallel WS connections to split universe across.
# HL closes connections that subscribe to too many channels at once.
WS_NUM_CONNECTIONS = 4
SUB_DELAY_SEC = 1.0   # was 0.3 — try slower to see if HL is rate-limiting subs


class CandleFeed:
    """Live candle stream. Updates _candles in-place as bars form/close."""

    def __init__(self, universe, interval="1m", on_candle_close=None):
        self.universe = list(universe)
        self.interval = interval
        self.on_candle_close = on_candle_close

        # 1m live stream
        self._candles = defaultdict(lambda: deque(maxlen=600))
        # Seeded 15m bars (so signal eval has data on day 1)
        self._seeded_15m = defaultdict(list)

        self._connected_count = 0
        self._last_msg_ts = 0
        self.stats = {
            'msgs_received': 0,
            'candles_closed': 0,
            'reconnects': 0,
            'errors': 0,
            'forward_fills': 0,
        }

    def get_recent(self, coin, n=100):
        """Return last n live 1m candles for coin."""
        c = list(self._candles.get(coin, []))
        return c[-n:] if len(c) >= n else c

    def get_seeded_15m(self, coin):
        """Return seeded 15m bars (oldest→newest)."""
        return list(self._seeded_15m.get(coin, []))

    def is_healthy(self):
        return (self._connected_count > 0 and
                (time.time() - self._last_msg_ts) < 30)

    def status(self):
        return {
            'connected_count': self._connected_count,
            'connection_target': WS_NUM_CONNECTIONS,
            'last_msg_age_sec': round(time.time() - self._last_msg_ts, 1) if self._last_msg_ts else None,
            'coins_tracked_1m': len(self._candles),
            'coins_tracked_15m_seeded': len(self._seeded_15m),
            'universe_size': len(self.universe),
            **self.stats,
        }

    # ─────────────────────────────────────────────────────────
    # SEED — REST fetch to populate buffers BEFORE WS catches up
    # ─────────────────────────────────────────────────────────
    def _rest_candles(self, coin, interval, n_bars):
        end_ms = int(time.time() * 1000)
        sec_per_bar = {'1m': 60, '15m': 900, '1h': 3600, '4h': 14400}.get(interval, 60)
        start_ms = end_ms - n_bars * sec_per_bar * 1000
        body = json.dumps({
            "type": "candleSnapshot",
            "req": {"coin": coin, "interval": interval,
                    "startTime": start_ms, "endTime": end_ms}
        }).encode()
        req = urllib.request.Request(HL_REST_URL, data=body,
            headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read())

    def seed_history(self, n_1m=200, n_15m=200):
        """Two-pass seed: 1m for live continuity + 15m for instant signal eligibility.

        With n_15m=200, signal engine has 200 15m bars available immediately.
        That's ~50 hours of history — plenty for ext_lookback=70 and pivots.
        """
        log.info(f"Seeding {len(self.universe)} coins (1m×{n_1m}, 15m×{n_15m}) via REST…")
        ok_1m = ok_15m = 0
        for coin in self.universe:
            # 1m
            try:
                data = self._rest_candles(coin, '1m', n_1m)
                for bar in data:
                    self._candles[coin].append({
                        't': int(bar['t']), 'o': float(bar['o']), 'h': float(bar['h']),
                        'l': float(bar['l']), 'c': float(bar['c']), 'v': float(bar['v']),
                        'closed': True,
                    })
                ok_1m += 1
            except Exception as e:
                log.warning(f"seed 1m err {coin}: {str(e)[:80]}")
            time.sleep(1.5)
            # 15m
            try:
                data = self._rest_candles(coin, '15m', n_15m)
                self._seeded_15m[coin] = [{
                    't': int(bar['t']), 'o': float(bar['o']), 'h': float(bar['h']),
                    'l': float(bar['l']), 'c': float(bar['c']), 'v': float(bar['v']),
                    'closed': True,
                } for bar in data]
                ok_15m += 1
            except Exception as e:
                log.warning(f"seed 15m err {coin}: {str(e)[:80]}")
            time.sleep(1.5)
        log.info(f"Seed complete: 1m={ok_1m}/{len(self.universe)}  15m={ok_15m}/{len(self.universe)}")
        return ok_1m, ok_15m

    # ─────────────────────────────────────────────────────────
    # LIVE STREAM — multi-connection split
    # ─────────────────────────────────────────────────────────
    async def _run_one_connection(self, conn_idx, coins):
        """Run one WS connection responsible for a subset of coins."""
        while True:
            try:
                async with websockets.connect(
                    HL_WS_URL, ping_interval=20, ping_timeout=20,
                    close_timeout=5, max_size=2**20,
                ) as ws:
                    self._connected_count += 1
                    log.info(f"WS-{conn_idx} connected ({len(coins)} coins)")
                    # Subscribe to assigned coins
                    for coin in coins:
                        sub = {"method": "subscribe",
                               "subscription": {"type": "candle", "coin": coin,
                                                "interval": self.interval}}
                        await ws.send(json.dumps(sub))
                        await asyncio.sleep(SUB_DELAY_SEC)
                    log.info(f"WS-{conn_idx} subscribed to {len(coins)} coins")
                    # Receive loop
                    async for raw in ws:
                        self._last_msg_ts = time.time()
                        self.stats['msgs_received'] += 1
                        try:
                            msg = json.loads(raw)
                        except Exception:
                            continue
                        ch = msg.get('channel')
                        if ch != 'candle':
                            # Log non-candle replies (subscriptionResponse, error, etc)
                            if ch in ('error', 'subscriptionResponse'):
                                log.info(f"WS-{conn_idx} {ch}: {str(msg)[:200]}")
                            continue
                        d = msg.get('data') or {}
                        coin = d.get('s')
                        if not coin:
                            continue
                        try:
                            new_candle = {
                                't': int(d['t']),
                                'o': float(d['o']),
                                'h': float(d['h']),
                                'l': float(d['l']),
                                'c': float(d['c']),
                                'v': float(d['v']),
                                'closed': False,
                            }
                        except (KeyError, TypeError, ValueError):
                            continue
                        buf = self._candles[coin]
                        # Forward-fill gap detection
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
                        if buf and buf[-1]['t'] == new_candle['t']:
                            buf[-1] = new_candle
                        else:
                            if buf:
                                buf[-1]['closed'] = True
                                self.stats['candles_closed'] += 1
                                if self.on_candle_close:
                                    try: self.on_candle_close(coin, buf[-1])
                                    except Exception as e:
                                        log.warning(f"on_candle_close err {coin}: {e}")
                            buf.append(new_candle)
            except websockets.ConnectionClosed as e:
                self._connected_count = max(0, self._connected_count - 1)
                self.stats['reconnects'] += 1
                self.stats['errors'] += 1
                code = getattr(e, 'code', None) or getattr(getattr(e, 'rcvd', None), 'code', None)
                reason = getattr(e, 'reason', None) or getattr(getattr(e, 'rcvd', None), 'reason', None)
                log.warning(f"WS-{conn_idx} closed: code={code} reason={reason!r} — reconnecting in 5s")
                await asyncio.sleep(5)
            except (OSError, asyncio.TimeoutError) as e:
                self._connected_count = max(0, self._connected_count - 1)
                self.stats['reconnects'] += 1
                self.stats['errors'] += 1
                log.warning(f"WS-{conn_idx} disconnected: {type(e).__name__}: {e} — reconnecting in 5s")
                await asyncio.sleep(5)
            except Exception as e:
                self._connected_count = max(0, self._connected_count - 1)
                self.stats['errors'] += 1
                log.error(f"WS-{conn_idx} unexpected: {type(e).__name__}: {e}")
                await asyncio.sleep(5)

    async def run(self):
        """Split universe across WS_NUM_CONNECTIONS parallel sockets."""
        chunks = [[] for _ in range(WS_NUM_CONNECTIONS)]
        for i, coin in enumerate(self.universe):
            chunks[i % WS_NUM_CONNECTIONS].append(coin)
        log.info(f"Splitting {len(self.universe)} coins across {WS_NUM_CONNECTIONS} WS connections "
                 f"(~{len(self.universe)//WS_NUM_CONNECTIONS} each)")
        await asyncio.gather(*[
            self._run_one_connection(idx, coins)
            for idx, coins in enumerate(chunks) if coins
        ])
