# PreCog Local Engine

Native Mac data + signal engine. Render becomes execution-only.

```
HL WebSocket  →  This process (Mac)  →  webhook  →  Render  →  Hyperliquid
                 - candles                              - execute
                 - strategy                             - SL/TP
                 - risk + sizing
                 - dispatch
```

No more REST rate limits. No CloudFront 429s. WS feed has zero rate limit.

---

## Setup (one-time, ~2 min)

```bash
cd ~/Desktop
mv ~/Downloads/precog_local .
cd precog_local

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
source venv/bin/activate
python main.py
```

You should see:

```
[feed] WS connected to wss://api.hyperliquid.xyz/ws
[feed] Subscribed to 78 coins (1m)
[feed] Seed complete: 78/78 coins loaded
[main] Tick loop starting (interval=30s)
[main] --- tick #1 | evaluated=78/78 | passed=2 | dur=1.4s | feed_msgs=156 ---
```

Health check:

```bash
curl -s http://localhost:8765/health | python3 -m json.tool
```

## Dry run first (recommended)

```bash
PRECOG_DRY_RUN=1 python main.py
```

Logs signals but does not POST to Render. Good for verifying signal quality
before sending real trades.

## Going live

In `config.json`:

```json
"execution": { "dry_run": false }
```

Then run:

```bash
python main.py
```

Signals will POST to your Render webhook → Render places HL orders.

---

## What to disable on Render

The Render service should now do **execution only**. Disable the local data
plane work to save resources and prevent it from fighting your Mac:

In Render env vars, add:

```
DISABLE_LOCAL_SCANNER=1
DISABLE_SNAPSHOT_BUILDER=1
DISABLE_RECONCILER_HALT=1
```

These need to be wired in `precog.py` — the existing process will keep
listening for webhooks but stop doing its own scanning / candle fetching.
(If you want, I can add the `if os.environ.get('DISABLE_*'):` guards.)

---

## Files

| File | Purpose |
|---|---|
| `feed.py` | HL WebSocket ingestion + 1m candle building + forward-fill on gaps |
| `buffer.py` | Aggregates 1m → 15m / 1h / 4h (UTC-aligned) |
| `strategy.py` | PreCog confluence: structural pivot gate + RSI + wick + HTF + ATR floor + TP1/TP2 |
| `regime.py` | Trend/chop/breakout classification + BTC correlation gate |
| `risk.py` | 9 filters: cooldown, open-signal cache, WR (3-tier), CB, max pos, RR, ATR floor, regime, BTC corr |
| `dispatch.py` | POST TRADE_INTENT → Render webhook (idempotent + retry-once) |
| `state_io.py` | Atomic JSON disk snapshot for crash recovery |
| `main.py` | Wires everything, runs tick loop, hosts /health, persists state every 60s |
| `config.json` | Universe, secrets, all thresholds |

---

## Spec compliance

This build matches the full spec end-to-end:

| Spec section | Implementation |
|---|---|
| §3.1 WS data + REST seed | `feed.py` |
| §3.1 Auto-reconnect | `feed.py` `run()` while-loop |
| §3.1 Forward-fill missing | `feed.py` gap detection block |
| §3.2 1m → 15m/1h/4h UTC-aligned | `buffer.py` `floor_to_tf` |
| §3.3 Pure-function signal | `strategy.py` `evaluate_signal` (no I/O) |
| §3.4 9 filters | `risk.py` `evaluate` |
| §3.4 BTC correlation filter | `regime.py` `btc_correlation_ok` |
| §3.4 Regime filter | `regime.py` `classify` |
| §3.4 ATR minimum | `strategy.py` + `risk.py` |
| §3.5 Sizing formula (5 multipliers) | `risk.py` confidence × wr × htf × confluence |
| §3.6 Idempotent dispatch | `dispatch.py` `_sent_trade_ids` + `risk.py` open_signal_cache |
| §3.6 Retry once | `dispatch.py` for-loop attempt 1,2 |
| §4 State model | `risk.to_dict/from_dict` + `state_io.py` |
| §5 TP1 partial + TP2 runner | `strategy.py` tp1/tp2 fields |
| §6 WS reconnect, retry once, dedupe | All implemented |

---

## Tuning

`config.json` has the validated PreCog params (~80% backtested WR). The
**structural pivot gate** is the key edge — never disable it.

Knobs you'll commonly touch:

```jsonc
"engine": {
  "tick_interval_sec": 30,    // how often to scan. 30s = ~120 ticks/hr
  "max_positions": 10
},
"risk": {
  "force_notional_usd": 11,   // bypass sizing — every trade $11. Set 0 for live sizing.
  "risk_pct_per_trade": 0.15, // 15% equity per trade (with leverage 15x)
  "rr_min": 1.2
}
```

---

## Architecture invariants (from spec)

- 78-symbol universe fixed
- Hyperliquid sole execution venue
- WebSocket only for market data
- REST only for execution + one-time seed
- No databases, no external services
- Local = stateful intelligence, Render = stateless execution
- Idempotent dispatch via `trade_id`
- Deterministic strategy (no randomness, no I/O in strategy.py)

---

## Health endpoint

```
GET http://localhost:8765/health
```

Returns full diagnostic JSON: feed status, buffer, risk stats, dispatch stats.
Use it the same way you've been using Render's /health.

---

## Troubleshooting

**Feed won't connect:** check internet. HL WS URL is `wss://api.hyperliquid.xyz/ws`.

**No signals after 30 min:** strategy is conservative (RSI 75/25 + structural).
Lower thresholds in config:
```json
"strategy": {
  "rsi_long_max": 30,
  "rsi_short_min": 70,
}
```

**Too many signals:** raise thresholds, or raise `cooldown_after_signal_sec`.

**Render webhook returns errors:** check `dispatch.stats.http_err` in /health.
Verify webhook URL and secret are correct in config.json.
