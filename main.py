"""
main.py — PreCog Local Engine.

Two-plane architecture (per spec):
  THIS PROCESS (Mac): WS feed + signals + risk + dispatch + state
  RENDER (cloud):     webhook receiver + execution only

Run:
  pip install websockets
  python main.py

Optional:
  PRECOG_DRY_RUN=1 python main.py    # don't actually POST to Render
  PRECOG_CONFIG=mycfg.json python main.py
  PRECOG_STATE_FILE=state.json       # disk snapshot location

Health server: http://localhost:8765/health
"""

import asyncio
import json
import logging
import os
import signal as _signal
import sys
import time
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from feed import CandleFeed
from buffer import TimeframeBuffer
from strategy import evaluate_signal
from risk import RiskEngine
from regime import classify as classify_regime
from dispatch import Dispatcher
from state_io import save_state, load_state


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)-12s] %(levelname)s | %(message)s',
    datefmt='%H:%M:%S',
)
log = logging.getLogger("main")


# ─── HEALTH SERVER (local, lightweight) ────────────────────────────────
class _HealthState:
    feed = None
    tfbuf = None
    risk = None
    dispatcher = None
    last_tick_ts = 0
    tick_count = 0
    signals_evaluated = 0
    signals_passed = 0
    btc_regime = ('UNKNOWN', 0)


def _make_handler(state):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a, **kw): pass
        def do_GET(self):
            if self.path != '/health':
                self.send_response(404); self.end_headers(); return
            payload = {
                'ok': True,
                'now': time.time(),
                'last_tick_ts': state.last_tick_ts,
                'tick_count': state.tick_count,
                'signals_evaluated': state.signals_evaluated,
                'signals_passed': state.signals_passed,
                'btc_regime': list(state.btc_regime),
                'feed': state.feed.status() if state.feed else {},
                'tfbuf': state.tfbuf.status() if state.tfbuf else {},
                'risk': state.risk.status() if state.risk else {},
                'dispatch': state.dispatcher.status() if state.dispatcher else {},
            }
            body = json.dumps(payload, indent=2, default=str).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    return H


def start_health_server(state, port=8765):
    handler = _make_handler(state)
    srv = HTTPServer(('0.0.0.0', port), handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    log.info(f"Health server: http://localhost:{port}/health")


# ─── STATE PERSISTENCE BACKGROUND TASK ──────────────────────────────────
async def state_persistence_loop(state, state_path, interval_sec=60):
    """Save state to disk every N seconds."""
    while True:
        await asyncio.sleep(interval_sec)
        try:
            snap = {
                'saved_at': time.time(),
                'tick_count': state.tick_count,
                'signals_evaluated': state.signals_evaluated,
                'signals_passed': state.signals_passed,
                'risk': state.risk.to_dict() if state.risk else {},
            }
            ok = save_state(state_path, snap)
            if ok:
                log.debug(f"state snapshot saved: {state_path}")
        except Exception as e:
            log.warning(f"state save err: {e}")


# ─── TICK LOOP ──────────────────────────────────────────────────────────
async def tick_loop(state, config):
    """Every N seconds: scan universe, evaluate signals, dispatch passing ones."""
    tick_interval = config['engine'].get('tick_interval_sec', 30)
    min_bars = config['engine'].get('min_bars_required', 50)
    universe = config['universe']
    strat_params = config['strategy']

    log.info(f"Tick loop starting (interval={tick_interval}s)")
    # Warm-up wait
    while True:
        if state.feed.is_healthy():
            ready = sum(1 for c in universe if len(state.feed.get_recent(c, n=min_bars)) >= min_bars)
            if ready >= len(universe) * 0.5:
                log.info(f"Feed warm: {ready}/{len(universe)} coins have ≥{min_bars} bars")
                break
        log.info(f"Waiting for feed warm-up… ({state.feed.status()})")
        await asyncio.sleep(5)

    while True:
        t_start = time.time()
        state.tick_count += 1
        state.last_tick_ts = t_start

        # ── BTC regime once per tick (used by every coin's correlation filter) ──
        btc_15m = state.tfbuf.get('BTC', '15m', n=120)
        btc_1h  = state.tfbuf.get('BTC', '1h', n=120)
        # Use 1h for BTC trend — slower, more stable signal
        state.btc_regime = classify_regime(btc_1h or btc_15m or [])

        passed_this_tick = 0
        evaluated_this_tick = 0
        for coin in universe:
            try:
                c15 = state.tfbuf.get(coin, '15m', n=200)
                c1h = state.tfbuf.get(coin, '1h', n=120)
                c4h = state.tfbuf.get(coin, '4h', n=80)
                if len(c15) < min_bars:
                    continue
                evaluated_this_tick += 1
                state.signals_evaluated += 1

                sig = evaluate_signal(coin, c15, c1h, c4h, strat_params)
                if not sig:
                    continue

                # Coin's own regime (15m for tactical view)
                coin_regime = classify_regime(c15)

                intent, reason = state.risk.evaluate(
                    sig, coin_regime=coin_regime, btc_regime=state.btc_regime
                )
                if not intent:
                    log.info(f"BLOCKED {coin} {sig['side']}: {reason} | conf={sig['confidence']} rr={sig['rr']}")
                    continue

                ok = state.dispatcher.send(intent)
                if ok:
                    state.risk.mark_position_open(coin)
                    passed_this_tick += 1
                    state.signals_passed += 1
                    log.info(f"PASSED {coin} {sig['side']} | conf={sig['confidence']} rr={sig['rr']} "
                             f"notional=${intent['notional_usd']} | reasons={intent['reasons']}")
            except Exception as e:
                log.exception(f"tick err {coin}: {e}")

        dur = time.time() - t_start
        log.info(f"--- tick #{state.tick_count} | evaluated={evaluated_this_tick}/{len(universe)} "
                 f"| passed={passed_this_tick} | btc={state.btc_regime[0]} | dur={dur:.1f}s "
                 f"| feed_msgs={state.feed.stats['msgs_received']} ---")

        sleep_for = max(1, tick_interval - dur)
        await asyncio.sleep(sleep_for)


# ─── BOOTSTRAP ──────────────────────────────────────────────────────────
def load_config():
    path = os.environ.get('PRECOG_CONFIG', 'config.json')
    if not os.path.exists(path):
        log.error(f"Config not found: {path}")
        sys.exit(1)
    with open(path) as f:
        return json.load(f)


async def amain():
    config = load_config()
    state_path = os.environ.get('PRECOG_STATE_FILE', 'precog_state.json')

    # ── Env var overrides (so we don't have to commit secrets to config.json) ──
    if os.environ.get('PRECOG_WEBHOOK_URL'):
        config['render']['webhook_url'] = os.environ['PRECOG_WEBHOOK_URL']
    if os.environ.get('PRECOG_WEBHOOK_SECRET'):
        config['render']['webhook_secret'] = os.environ['PRECOG_WEBHOOK_SECRET']
    if os.environ.get('PRECOG_FORCE_NOTIONAL_USD'):
        try:
            config['risk']['force_notional_usd'] = float(os.environ['PRECOG_FORCE_NOTIONAL_USD'])
        except Exception: pass
    if os.environ.get('PRECOG_EQUITY_USD'):
        try:
            config['risk']['equity_usd'] = float(os.environ['PRECOG_EQUITY_USD'])
        except Exception: pass

    if os.environ.get('PRECOG_DRY_RUN', '').lower() in ('1', 'true', 'yes'):
        config['execution']['dry_run'] = True
        log.warning("DRY RUN MODE — signals will NOT be sent to Render")

    log.info(f"Webhook target: {config['render']['webhook_url']}")
    log.info(f"Force notional: ${config['risk'].get('force_notional_usd', 0)}")
    log.info(f"Dry run: {config['execution'].get('dry_run', False)}")

    state = _HealthState()

    # Build pipeline
    state.feed = CandleFeed(config['universe'], interval='1m')
    state.tfbuf = TimeframeBuffer(state.feed, max_per_tf=config['engine'].get('buffer_size_per_tf', 500))
    state.risk = RiskEngine(config)
    state.dispatcher = Dispatcher(
        config['render']['webhook_url'],
        config['render']['webhook_secret'],
        dry_run=config['execution'].get('dry_run', False),
    )

    # Restore state from disk if present (crash recovery, spec §4)
    prev = load_state(state_path)
    if prev:
        state.tick_count = int(prev.get('tick_count') or 0)
        state.signals_evaluated = int(prev.get('signals_evaluated') or 0)
        state.signals_passed = int(prev.get('signals_passed') or 0)
        state.risk.from_dict(prev.get('risk') or {})
        log.info(f"State restored from {state_path} (last saved {prev.get('saved_at')})")
    else:
        log.info("No prior state — starting fresh")

    start_health_server(state, port=int(os.environ.get('PORT') or os.environ.get('PRECOG_HEALTH_PORT') or 8765))

    # Seed history (one-time)
    log.info("Seeding history via REST (1m + 15m, ~5 min for 78 coins)…")
    state.feed.seed_history(n_1m=200, n_15m=200)

    log.info("Launching WebSocket feed + tick loop + state persistence")
    await asyncio.gather(
        state.feed.run(),
        tick_loop(state, config),
        state_persistence_loop(state, state_path, interval_sec=60),
    )


if __name__ == '__main__':
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        log.info("Shutdown requested. Bye.")
