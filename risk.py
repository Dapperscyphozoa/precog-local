"""
risk.py — Hard filters + position sizing.

Filters applied in order (per spec §3.4):
  1. Cooldown (per coin)
  2. Open signal cache (idempotency — block duplicates BEFORE dispatch)
  3. WR threshold (three-tier: hard reject < 0.20, soft size×0.3 < 0.35)
  4. Consecutive loss circuit breaker
  5. Max position cap
  6. R:R minimum
  7. ATR floor (signal-side; volatility minimum)
  8. Regime filter (CHOP/BREAKOUT → block, TREND aligned → ok)
  9. BTC correlation (block trades against BTC trend)

Sizing (per spec §3.5):
  size = equity × base_risk × confidence_mult × wr_mult × htf_mult × confluence_mult / price
  Force-notional override available for testing.
  Hard floor: min_notional_usd to satisfy HL exchange minimum.
"""

import time
from regime import btc_correlation_ok


class RiskEngine:
    def __init__(self, config):
        self.cfg = config['risk']
        self.engine_cfg = config['engine']
        self._cooldowns = {}      # {coin: ts_when_cooldown_ends}
        self._coin_wr = {}        # {coin: {'wins': int, 'losses': int}}
        self._consec_losses = 0
        self._open_positions = set()
        # Open signal cache: trade_ids that have been generated but
        # not yet confirmed as positions. Blocks duplicate firing.
        self._open_signal_cache = {}  # {coin: trade_id}
        self.stats = {
            'evaluated': 0,
            'blocked_cooldown': 0,
            'blocked_open_signal': 0,
            'blocked_wr_hard': 0,
            'sized_down_wr_soft': 0,
            'blocked_circuit_breaker': 0,
            'blocked_max_positions': 0,
            'blocked_rr': 0,
            'blocked_atr_floor': 0,
            'blocked_regime': 0,
            'blocked_btc_corr': 0,
            'blocked_min_notional': 0,
            'passed': 0,
        }

    def record_outcome(self, coin, won):
        d = self._coin_wr.setdefault(coin, {'wins': 0, 'losses': 0})
        if won:
            d['wins'] += 1
            self._consec_losses = 0
        else:
            d['losses'] += 1
            self._consec_losses += 1

    def mark_position_open(self, coin):
        self._open_positions.add(coin)
        self._cooldowns[coin] = time.time() + self.cfg.get('cooldown_after_signal_sec', 1800)

    def mark_position_closed(self, coin):
        self._open_positions.discard(coin)
        self._open_signal_cache.pop(coin, None)

    def coin_wr(self, coin):
        d = self._coin_wr.get(coin)
        if not d:
            return None, 0
        n = d['wins'] + d['losses']
        if n == 0:
            return None, 0
        return d['wins'] / n, n

    def evaluate(self, signal, coin_regime=None, btc_regime=None):
        """Apply filters + sizing.

        Args:
          signal: dict from strategy.evaluate_signal
          coin_regime: tuple (regime_str, score) from regime.classify(coin candles)
          btc_regime: tuple (regime_str, score) from regime.classify(BTC candles)

        Returns: (intent_dict, reason) or (None, reason).
        """
        self.stats['evaluated'] += 1
        coin = signal['symbol']

        # Filter 1: cooldown
        cd = self._cooldowns.get(coin, 0)
        if time.time() < cd:
            self.stats['blocked_cooldown'] += 1
            return None, f"cooldown ({int(cd - time.time())}s left)"

        # Filter 2: open signal cache (pre-dispatch dedupe)
        if coin in self._open_signal_cache:
            self.stats['blocked_open_signal'] += 1
            return None, f"open_signal exists for {coin} ({self._open_signal_cache[coin][:8]})"

        # Filter 3: WR
        wr, n = self.coin_wr(coin)
        size_mult_wr = 1.0
        wr_reason = None
        if wr is not None and n >= 10:
            if wr < self.cfg.get('wr_hard_floor', 0.20):
                self.stats['blocked_wr_hard'] += 1
                return None, f"wr_hard_reject (wr={wr:.0%}, n={n})"
            elif wr < self.cfg.get('wr_soft_floor', 0.35):
                size_mult_wr = self.cfg.get('wr_soft_size_mult', 0.3)
                wr_reason = f"wr_soft_size×{size_mult_wr} (wr={wr:.0%})"
                self.stats['sized_down_wr_soft'] += 1

        # Filter 4: circuit breaker
        if self._consec_losses >= self.cfg.get('max_consecutive_losses', 5):
            self.stats['blocked_circuit_breaker'] += 1
            return None, f"circuit_breaker ({self._consec_losses} consec losses)"

        # Filter 5: max positions
        if len(self._open_positions) >= self.engine_cfg.get('max_positions', 10):
            self.stats['blocked_max_positions'] += 1
            return None, f"max_positions ({len(self._open_positions)} open)"

        # Filter 6: RR
        rr = signal.get('rr', 0)
        if rr < self.cfg.get('rr_min', 1.2):
            self.stats['blocked_rr'] += 1
            return None, f"rr_low ({rr:.2f} < {self.cfg.get('rr_min')})"

        # Filter 7: ATR floor (already applied in strategy, but re-check from config)
        atr_pct = signal.get('atr_pct', 0)
        atr_floor = self.cfg.get('atr_floor_pct', 0.0015)
        if atr_pct and atr_pct < atr_floor:
            self.stats['blocked_atr_floor'] += 1
            return None, f"atr_floor ({atr_pct:.4%} < {atr_floor:.4%})"

        # Filter 8: regime (block in CHOP and BREAKOUT)
        if coin_regime:
            r_name, _ = coin_regime
            if r_name == 'CHOP':
                self.stats['blocked_regime'] += 1
                return None, f"regime_chop"
            if r_name == 'BREAKOUT':
                self.stats['blocked_regime'] += 1
                return None, f"regime_breakout (undefined direction)"

        # Filter 9: BTC correlation
        if btc_regime:
            btc_name, _ = btc_regime
            if not btc_correlation_ok(signal['side'], btc_name):
                self.stats['blocked_btc_corr'] += 1
                return None, f"btc_corr_block (BTC={btc_name}, signal={signal['side']})"

        # ── Sizing ──
        equity = self.cfg.get('equity_usd', 1000)
        base_risk = self.cfg.get('risk_pct_per_trade', 0.15)
        leverage = self.cfg.get('leverage', 15)

        # Multipliers
        confidence_mult = signal.get('confidence', 50) / 100.0
        confluence_mult = signal.get('confluence', 50) / 100.0
        # HTF alignment: same direction = 1.0, opposing = 0.5, neutral = 0.75
        htf_1h = signal.get('htf_1h', 'NEUTRAL')
        htf_4h = signal.get('htf_4h', 'NEUTRAL')
        wanted = 'BULL' if signal['side'] == 'BUY' else 'BEAR'
        htf_mult = 1.0
        if htf_1h == wanted and htf_4h == wanted:
            htf_mult = 1.0
        elif htf_1h == wanted or htf_4h == wanted:
            htf_mult = 0.85
        elif htf_1h == 'NEUTRAL' and htf_4h == 'NEUTRAL':
            htf_mult = 0.75
        else:
            htf_mult = 0.5  # both HTFs opposing — already gated, but conservative

        # Final formula (spec §3.5)
        notional = equity * base_risk * leverage * confidence_mult * \
                   size_mult_wr * htf_mult * confluence_mult

        force_notional = self.cfg.get('force_notional_usd', 0)
        if force_notional and force_notional > 0:
            notional = force_notional

        min_notional = self.cfg.get('min_notional_usd', 11)
        if notional < min_notional:
            self.stats['blocked_min_notional'] += 1
            return None, f"notional_below_min (${notional:.2f} < ${min_notional})"

        size = notional / signal['entry']

        intent = {
            'trade_id': f"{coin}_{int(time.time()*1000)}",
            'symbol': coin,
            'side': signal['side'],
            'size': size,
            'leverage': leverage,
            'entry_type': 'market',
            'entry': signal['entry'],
            'sl': signal['sl'],
            'tp1': signal.get('tp1', signal.get('tp')),
            'tp2': signal.get('tp2', signal.get('tp')),
            'tp': signal.get('tp1', signal.get('tp')),  # legacy alias
            'confidence': signal['confidence'],
            'confluence': signal.get('confluence', signal['confidence']),
            'rr': rr,
            'notional_usd': round(notional, 2),
            'size_mults': {
                'confidence': round(confidence_mult, 2),
                'wr': round(size_mult_wr, 2),
                'htf': round(htf_mult, 2),
                'confluence': round(confluence_mult, 2),
            },
            'reasons': signal.get('reasons', []) + ([wr_reason] if wr_reason else []),
            'tf': signal.get('tf', '15m'),
            'coin_regime': coin_regime[0] if coin_regime else 'UNKNOWN',
            'btc_regime': btc_regime[0] if btc_regime else 'UNKNOWN',
        }
        # Cache the open signal — prevents duplicates until position closed
        self._open_signal_cache[coin] = intent['trade_id']
        self.stats['passed'] += 1
        return intent, "ok"

    # ── Persistence helpers ──────────────────────────────────────────
    def to_dict(self):
        return {
            'cooldowns': self._cooldowns,
            'coin_wr': self._coin_wr,
            'consec_losses': self._consec_losses,
            'open_positions': sorted(self._open_positions),
            'open_signal_cache': self._open_signal_cache,
        }

    def from_dict(self, d):
        if not d:
            return
        self._cooldowns = {k: float(v) for k, v in (d.get('cooldowns') or {}).items()}
        self._coin_wr = d.get('coin_wr') or {}
        self._consec_losses = int(d.get('consec_losses') or 0)
        self._open_positions = set(d.get('open_positions') or [])
        self._open_signal_cache = d.get('open_signal_cache') or {}

    def status(self):
        return {
            'open_positions': sorted(self._open_positions),
            'open_signals': dict(self._open_signal_cache),
            'consec_losses': self._consec_losses,
            'cooldown_active': len([c for c, t in self._cooldowns.items() if t > time.time()]),
            'coins_with_wr': len(self._coin_wr),
            **self.stats,
        }
