"""
strategy.py — PreCog confluence signal engine.

Reuses the validated PreCog logic that produced ~80% backtested WR
on 60-day backtests across 25 HL coins:

  - Structural pivot gate (THE KEY EDGE — never disable)
      block buy if last 3 pivot lows descending
      block sell if last 3 pivot highs ascending
  - RSI extremes (75/25 strict mode)
  - Wick extension (0.2 minimum body-to-range)
  - HTF trend bias (1h, 4h)
  - EMA crossover (9/21)
  - Volume confirmation
  - Extension lookback (70 bars) — checks how stretched price is

Output: dict per signal or None.
  {
    'symbol', 'side' ('BUY'|'SELL'),
    'entry', 'sl', 'tp', 'rr',
    'confidence' (0-100),
    'reasons': [list of strings],
    'tf': '15m'
  }

Pure function — no state, no I/O, no randomness.
"""

import math


# ─── INDICATORS ──────────────────────────────────────────────────────────

def ema(values, period):
    """Standard EMA. Returns list same length as input, NaN for first period-1."""
    if len(values) < period:
        return [float('nan')] * len(values)
    out = [float('nan')] * (period - 1)
    sma = sum(values[:period]) / period
    out.append(sma)
    k = 2 / (period + 1)
    prev = sma
    for v in values[period:]:
        cur = v * k + prev * (1 - k)
        out.append(cur)
        prev = cur
    return out


def rsi(closes, period=14):
    """Wilder's RSI. Returns list with NaN for first period bars."""
    if len(closes) < period + 1:
        return [float('nan')] * len(closes)
    gains = [max(0, closes[i] - closes[i-1]) for i in range(1, len(closes))]
    losses = [max(0, closes[i-1] - closes[i]) for i in range(1, len(closes))]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    out = [float('nan')] * period
    rs = avg_gain / avg_loss if avg_loss > 0 else (100 if avg_gain > 0 else 0)
    out.append(100 - 100 / (1 + rs))
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            out.append(100)
        else:
            rs = avg_gain / avg_loss
            out.append(100 - 100 / (1 + rs))
    return out


def atr(candles, period=14):
    """Average True Range. Last value only."""
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        h = candles[i]['h']; l = candles[i]['l']; pc = candles[i-1]['c']
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < period:
        return None
    # Wilder's smoothing
    a = sum(trs[:period]) / period
    for tr in trs[period:]:
        a = (a * (period - 1) + tr) / period
    return a


# ─── PIVOTS (the edge) ───────────────────────────────────────────────────

def find_pivot_highs(candles, left=5, right=5):
    """A pivot high at index i exists if candle[i].h > all candles in [i-left:i+right+1]."""
    out = []
    for i in range(left, len(candles) - right):
        h = candles[i]['h']
        if all(candles[j]['h'] < h for j in range(i - left, i)) and \
           all(candles[j]['h'] < h for j in range(i + 1, i + right + 1)):
            out.append((i, h))
    return out


def find_pivot_lows(candles, left=5, right=5):
    out = []
    for i in range(left, len(candles) - right):
        l = candles[i]['l']
        if all(candles[j]['l'] > l for j in range(i - left, i)) and \
           all(candles[j]['l'] > l for j in range(i + 1, i + right + 1)):
            out.append((i, l))
    return out


def structural_block_buy(pivot_lows, n=3):
    """Block long if last n pivot lows are strictly descending (downtrend structure)."""
    if len(pivot_lows) < n:
        return False
    last_n = [p[1] for p in pivot_lows[-n:]]
    return all(last_n[i] > last_n[i+1] for i in range(n-1))


def structural_block_sell(pivot_highs, n=3):
    """Block short if last n pivot highs are strictly ascending (uptrend structure)."""
    if len(pivot_highs) < n:
        return False
    last_n = [p[1] for p in pivot_highs[-n:]]
    return all(last_n[i] < last_n[i+1] for i in range(n-1))


# ─── HTF TREND ───────────────────────────────────────────────────────────

def htf_trend(candles, ema_fast=9, ema_slow=21):
    """Returns 'BULL', 'BEAR', or 'NEUTRAL' from EMA stack."""
    if len(candles) < ema_slow + 1:
        return 'NEUTRAL'
    closes = [c['c'] for c in candles]
    f = ema(closes, ema_fast)[-1]
    s = ema(closes, ema_slow)[-1]
    last = closes[-1]
    if math.isnan(f) or math.isnan(s):
        return 'NEUTRAL'
    if last > f > s:
        return 'BULL'
    if last < f < s:
        return 'BEAR'
    return 'NEUTRAL'


# ─── EXTENSION CHECK ─────────────────────────────────────────────────────

def is_overextended(candles, lookback=70, side=None):
    """True if price is at extreme of recent range (signal exhaustion)."""
    if len(candles) < lookback:
        return False
    window = candles[-lookback:]
    hi = max(c['h'] for c in window)
    lo = min(c['l'] for c in window)
    cur = candles[-1]['c']
    rng = hi - lo
    if rng <= 0:
        return False
    pct = (cur - lo) / rng
    if side == 'BUY' and pct > 0.85:
        return True   # buying near the high — exhaust risk
    if side == 'SELL' and pct < 0.15:
        return True   # selling near the low — exhaust risk
    return False


# ─── WICK CHECK ──────────────────────────────────────────────────────────

def has_rejection_wick(candle, side, min_ratio=0.2):
    """For BUY: lower wick ≥ min_ratio of full range (rejection of lows)."""
    rng = candle['h'] - candle['l']
    if rng <= 0:
        return False
    body_top = max(candle['o'], candle['c'])
    body_bot = min(candle['o'], candle['c'])
    upper_wick = candle['h'] - body_top
    lower_wick = body_bot - candle['l']
    if side == 'BUY':
        return (lower_wick / rng) >= min_ratio
    if side == 'SELL':
        return (upper_wick / rng) >= min_ratio
    return False


# ─── VOLUME CHECK ────────────────────────────────────────────────────────

def volume_ok(candles, n=20, min_pct=0.7):
    """Last bar volume must be ≥ min_pct × N-bar avg."""
    if len(candles) < n + 1:
        return True  # not enough data, don't block
    avg = sum(c['v'] for c in candles[-n-1:-1]) / n
    if avg <= 0:
        return True
    return (candles[-1]['v'] / avg) >= min_pct


# ─── MAIN SIGNAL ─────────────────────────────────────────────────────────

def evaluate_signal(coin, candles_15m, candles_1h, candles_4h, params):
    """Return signal dict or None.

    candles_*: list of {t, o, h, l, c, v, closed} oldest→newest
    params: dict with strategy config
    """
    if not candles_15m or len(candles_15m) < params.get('ext_lookback', 70):
        return None

    closes_15 = [c['c'] for c in candles_15m]
    last = candles_15m[-1]
    cur_price = last['c']

    rsi_vals = rsi(closes_15, params.get('rsi_period', 14))
    if math.isnan(rsi_vals[-1]):
        return None
    cur_rsi = rsi_vals[-1]

    # Pivots for structural gate (THE EDGE)
    pl = params.get('pivot_lookback_left', 5)
    pr = params.get('pivot_lookback_right', 5)
    pivot_highs = find_pivot_highs(candles_15m, pl, pr)
    pivot_lows = find_pivot_lows(candles_15m, pl, pr)

    htf_1h = htf_trend(candles_1h or [], params.get('ema_fast', 9), params.get('ema_slow', 21))
    htf_4h = htf_trend(candles_4h or [], params.get('ema_fast', 9), params.get('ema_slow', 21))

    side = None
    reasons = []

    # ── BUY signal evaluation ──
    if cur_rsi <= params.get('rsi_long_max', 25):
        if structural_block_buy(pivot_lows, params.get('structural_pivot_n', 3)):
            return None  # downtrend structure — never long
        if is_overextended(candles_15m, params.get('ext_lookback', 70), side='BUY'):
            return None  # too extended
        if not has_rejection_wick(last, 'BUY', params.get('wick_min_ratio', 0.2)):
            return None  # no rejection
        if not volume_ok(candles_15m, n=20, min_pct=params.get('min_volume_pct_of_avg', 0.7)):
            return None  # weak volume
        side = 'BUY'
        reasons.append(f"rsi={cur_rsi:.1f}≤{params.get('rsi_long_max')}")
        reasons.append(f"struct=ok({len(pivot_lows)}pls)")
        reasons.append(f"wick=ok")
        reasons.append(f"vol=ok")
        reasons.append(f"htf1h={htf_1h}")

    # ── SELL signal evaluation ──
    elif cur_rsi >= params.get('rsi_short_min', 75):
        if structural_block_sell(pivot_highs, params.get('structural_pivot_n', 3)):
            return None
        if is_overextended(candles_15m, params.get('ext_lookback', 70), side='SELL'):
            return None
        if not has_rejection_wick(last, 'SELL', params.get('wick_min_ratio', 0.2)):
            return None
        if not volume_ok(candles_15m, n=20, min_pct=params.get('min_volume_pct_of_avg', 0.7)):
            return None
        side = 'SELL'
        reasons.append(f"rsi={cur_rsi:.1f}≥{params.get('rsi_short_min')}")
        reasons.append(f"struct=ok({len(pivot_highs)}phs)")
        reasons.append(f"wick=ok")
        reasons.append(f"vol=ok")
        reasons.append(f"htf1h={htf_1h}")
    else:
        return None

    # SL / TP from ATR — TP1 partial, TP2 runner per spec
    a = atr(candles_15m, params.get('atr_period', 14))
    if not a or a <= 0:
        return None

    # ATR floor: if ATR below threshold, no edge — skip (spec §3.4)
    atr_floor_pct = params.get('atr_floor_pct', 0.0015)  # 0.15% min volatility
    if a / cur_price < atr_floor_pct:
        return None

    sl_atr_mult = params.get('sl_atr_mult', 2.0)
    tp1_atr_mult = params.get('tp1_atr_mult', 2.5)   # TP1 partial
    tp2_atr_mult = params.get('tp2_atr_mult', 5.0)   # TP2 runner
    if side == 'BUY':
        sl  = cur_price - sl_atr_mult * a
        tp1 = cur_price + tp1_atr_mult * a
        tp2 = cur_price + tp2_atr_mult * a
    else:
        sl  = cur_price + sl_atr_mult * a
        tp1 = cur_price - tp1_atr_mult * a
        tp2 = cur_price - tp2_atr_mult * a
    rr = abs(tp1 - cur_price) / max(0.0001, abs(cur_price - sl))

    # Confidence: HTF alignment bumps, opposing HTF reduces
    confidence = 50
    if side == 'BUY' and htf_1h == 'BULL': confidence += 15
    if side == 'BUY' and htf_4h == 'BULL': confidence += 10
    if side == 'BUY' and htf_1h == 'BEAR': confidence -= 15
    if side == 'SELL' and htf_1h == 'BEAR': confidence += 15
    if side == 'SELL' and htf_4h == 'BEAR': confidence += 10
    if side == 'SELL' and htf_1h == 'BULL': confidence -= 15
    confidence = max(0, min(100, confidence))

    return {
        'symbol': coin,
        'side': side,
        'entry': round(cur_price, 8),
        'sl': round(sl, 8),
        'tp1': round(tp1, 8),
        'tp2': round(tp2, 8),
        'tp': round(tp1, 8),  # legacy alias = TP1 for executor compat
        'rr': round(rr, 2),
        'confidence': confidence,
        'reasons': reasons,
        'tf': '15m',
        'atr': round(a, 8),
        'atr_pct': round(a / cur_price, 5),
        'htf_1h': htf_1h,
        'htf_4h': htf_4h,
        # Confluence score — used for sizing multiplier in risk engine
        # 0..100 based on alignment factors (HTF + RSI + structure)
        'confluence': min(100, confidence + (10 if htf_4h == htf_1h and htf_1h != 'NEUTRAL' else 0)),
    }
