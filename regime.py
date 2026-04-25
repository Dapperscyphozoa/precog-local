"""
regime.py — Market regime detection.

Classifies a coin's current state as one of:
  TREND_UP    — strong upward directional move
  TREND_DOWN  — strong downward directional move
  CHOP        — range-bound, no directional edge
  BREAKOUT    — explosive volatility, undefined direction

Used by risk engine to gate signals: chop = lower size or skip,
trend = full size in trend direction, breakout = skip until resolved.

Pure function. No I/O.
"""

import math


def classify(candles, ema_fast=9, ema_slow=21, atr_period=14):
    """Returns regime string + numeric score."""
    if not candles or len(candles) < max(ema_slow, atr_period) + 5:
        return ('UNKNOWN', 0)

    closes = [c['c'] for c in candles]

    # EMA stack
    def _ema(values, period):
        if len(values) < period: return float('nan')
        sma = sum(values[:period]) / period
        k = 2 / (period + 1)
        v = sma
        for x in values[period:]:
            v = x * k + v * (1 - k)
        return v

    f = _ema(closes, ema_fast)
    s = _ema(closes, ema_slow)
    last = closes[-1]

    # ATR-based volatility
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]['h'], candles[i]['l'], candles[i-1]['c']
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    a = sum(trs[-atr_period:]) / atr_period if len(trs) >= atr_period else 0
    atr_pct = (a / last) if last > 0 else 0

    # Range over last 30 bars relative to ATR
    window = candles[-30:] if len(candles) >= 30 else candles
    rng = max(c['h'] for c in window) - min(c['l'] for c in window)
    rng_atr = rng / a if a > 0 else 0

    # Slope of EMAs
    closes_recent = closes[-20:]
    if len(closes_recent) >= 20:
        slope = (closes_recent[-1] - closes_recent[0]) / closes_recent[0]
    else:
        slope = 0

    # ── Classify ──
    if math.isnan(f) or math.isnan(s):
        return ('UNKNOWN', 0)

    # Breakout: very high recent volatility
    if atr_pct > 0.05:  # >5% ATR
        return ('BREAKOUT', 100)

    # Trend: EMAs stacked + price aligned + slope significant
    if last > f > s and slope > 0.02:
        return ('TREND_UP', min(100, int(slope * 1000)))
    if last < f < s and slope < -0.02:
        return ('TREND_DOWN', min(100, int(-slope * 1000)))

    # Chop: range tight relative to ATR, no clean EMA stack
    if rng_atr < 4:
        return ('CHOP', int(100 - rng_atr * 25))

    return ('NEUTRAL', 50)


def btc_correlation_ok(signal_side, btc_regime):
    """True if signal direction aligns with BTC regime.

    BUY blocked when BTC is TREND_DOWN.
    SELL blocked when BTC is TREND_UP.
    All sides allowed in CHOP/NEUTRAL/UNKNOWN.
    """
    if btc_regime == 'TREND_DOWN' and signal_side == 'BUY':
        return False
    if btc_regime == 'TREND_UP' and signal_side == 'SELL':
        return False
    return True
