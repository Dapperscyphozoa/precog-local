"""
buffer.py — Multi-timeframe candle aggregation.

Takes a stream of 1m candles and produces aligned 15m/1h/4h candles.
Time-aligned to UTC clock boundaries (no drift).

15m candle starts at minute % 15 == 0.
1h candle starts at minute == 0.
4h candle starts at hour % 4 == 0.
"""

import time
from collections import defaultdict, deque


# ms per timeframe
TF_MS = {
    '1m':   60 * 1000,
    '15m':  15 * 60 * 1000,
    '1h':   60 * 60 * 1000,
    '4h':   4 * 60 * 60 * 1000,
}


def floor_to_tf(ts_ms, tf):
    """Round timestamp down to the start of its tf bucket (UTC-aligned)."""
    bucket = TF_MS[tf]
    return (ts_ms // bucket) * bucket


def aggregate(candles_1m, target_tf):
    """Aggregate a list of 1m candles (oldest→newest) into target_tf candles.

    Returns list of dicts {t, o, h, l, c, v, closed}.
    Only emits candles with at least 1 source bar. The last candle may be
    'open' (closed=False) if the latest 1m bar isn't the bucket-end.
    """
    if not candles_1m or target_tf == '1m':
        return list(candles_1m)
    bucket = TF_MS[target_tf]
    out = []
    cur = None
    for c in candles_1m:
        bucket_t = floor_to_tf(c['t'], target_tf)
        if cur is None or cur['t'] != bucket_t:
            if cur is not None:
                out.append(cur)
            cur = {
                't': bucket_t,
                'o': c['o'],
                'h': c['h'],
                'l': c['l'],
                'c': c['c'],
                'v': c['v'],
                'closed': False,
            }
        else:
            cur['h'] = max(cur['h'], c['h'])
            cur['l'] = min(cur['l'], c['l'])
            cur['c'] = c['c']
            cur['v'] += c['v']
    if cur is not None:
        # Mark closed if the bucket END time has passed
        if cur['t'] + bucket <= int(time.time() * 1000):
            cur['closed'] = True
        out.append(cur)
    return out


class TimeframeBuffer:
    """Wraps a CandleFeed and exposes get(coin, tf) → list of candles.

    Keeps a cache of computed timeframes per coin to avoid recomputing on
    every signal evaluation. Cache invalidates when a new 1m bar arrives.
    """

    def __init__(self, feed, max_per_tf=500):
        self.feed = feed
        self.max_per_tf = max_per_tf
        # Cache: {coin: {tf: (last_1m_t, [candles])}}
        self._cache = defaultdict(dict)

    def get(self, coin, tf, n=200):
        """Get last n candles for coin at timeframe tf.

        For 15m: combines seeded historical bars (from REST seed at startup)
        with live-aggregated bars from the 1m feed. This means the signal
        engine has full history immediately on day 1 instead of waiting
        12+ hours for live 1m bars to roll up into 50+ 15m bars.
        """
        candles_1m = self.feed.get_recent(coin, n=self.max_per_tf)
        if tf == '1m':
            return candles_1m[-n:]

        # Live-aggregated from 1m
        live_agg = aggregate(candles_1m, tf) if candles_1m else []

        # For 15m: also pull seeded bars from feed and merge
        if tf == '15m':
            seeded = self.feed.get_seeded_15m(coin)
            if seeded:
                # Merge: use seeded for everything before earliest live bar,
                # use live for everything from there forward.
                if live_agg:
                    live_start_t = live_agg[0]['t']
                    older = [c for c in seeded if c['t'] < live_start_t]
                    merged = older + live_agg
                else:
                    merged = list(seeded)
                merged = merged[-self.max_per_tf:]
                # Cache for return
                last_t = candles_1m[-1]['t'] if candles_1m else (seeded[-1]['t'] if seeded else 0)
                self._cache[coin][tf] = (last_t, merged)
                return merged[-n:]

        # 1h/4h: pure live aggregation (we don't seed these directly)
        if not candles_1m:
            return []
        last_t = candles_1m[-1]['t']
        cached = self._cache[coin].get(tf)
        if cached and cached[0] == last_t:
            return cached[1][-n:]
        agg = live_agg[-self.max_per_tf:]
        self._cache[coin][tf] = (last_t, agg)
        return agg[-n:]

    def status(self):
        return {
            'coins_cached': len(self._cache),
            'tfs_per_coin': {c: list(tfs.keys()) for c, tfs in self._cache.items()},
        }
