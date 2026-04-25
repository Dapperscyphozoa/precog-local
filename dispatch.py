"""
dispatch.py — Webhook client. POST TRADE_INTENT → Render.

Idempotency: trade_id in payload. Render dedupes.
Retry: once on connection failure. Drop after 2 attempts.
Timeout: 8s per attempt.
"""

import json
import logging
import time
import urllib.request
import urllib.error

log = logging.getLogger("dispatch")


class Dispatcher:
    def __init__(self, webhook_url, webhook_secret, dry_run=False):
        self.url = webhook_url
        self.secret = webhook_secret
        self.dry_run = dry_run
        self.stats = {
            'sent': 0,
            'dropped_dry_run': 0,
            'http_ok': 0,
            'http_err': 0,
            'retries': 0,
        }
        self._sent_trade_ids = set()  # in-process dedupe

    def send(self, intent):
        """Returns True if sent (or would be sent in dry_run)."""
        tid = intent['trade_id']
        if tid in self._sent_trade_ids:
            log.warning(f"DUPLICATE trade_id {tid} — refusing to resend")
            return False

        if self.dry_run:
            self.stats['dropped_dry_run'] += 1
            self._sent_trade_ids.add(tid)
            log.info(f"[DRY RUN] would send: {intent['symbol']} {intent['side']} "
                     f"size={intent['size']:.4f} sl={intent['sl']} tp={intent['tp']} "
                     f"conf={intent['confidence']} rr={intent['rr']}")
            return True

        # Format that matches existing /webhook expectation
        # Body shape: PreCog-style alert text (the existing handler parses this)
        body_text = (
            f"PRECOG_LOCAL: {intent['symbol']} {intent['side']} "
            f"trade_id={intent['trade_id']} "
            f"size={intent['size']:.6f} "
            f"entry={intent.get('entry', 0)} "
            f"sl={intent['sl']} tp={intent['tp']} "
            f"leverage={intent['leverage']} "
            f"confidence={intent['confidence']} "
            f"rr={intent['rr']}"
        )

        payload = json.dumps({
            'trade_id': tid,
            'symbol': intent['symbol'],
            'side': intent['side'],
            'size': intent['size'],
            'leverage': intent['leverage'],
            'entry_type': intent.get('entry_type', 'market'),
            'entry': intent.get('entry'),
            'sl': intent['sl'],
            'tp': intent.get('tp', intent.get('tp1')),
            'tp1': intent.get('tp1'),
            'tp2': intent.get('tp2'),
            'confidence': intent['confidence'],
            'confluence': intent.get('confluence', intent['confidence']),
            'rr': intent['rr'],
            'tf': intent.get('tf', '15m'),
            'coin_regime': intent.get('coin_regime'),
            'btc_regime': intent.get('btc_regime'),
            'reasons': intent.get('reasons', []),
            'source': 'precog_local',
            'alert_text': body_text,
        }).encode()

        for attempt in (1, 2):
            try:
                req = urllib.request.Request(
                    self.url, data=payload, method='POST',
                    headers={
                        'Content-Type': 'application/json',
                        'X-Webhook-Secret': self.secret,
                    }
                )
                with urllib.request.urlopen(req, timeout=8) as r:
                    code = r.getcode()
                    body = r.read()[:200]
                    if 200 <= code < 300:
                        self.stats['sent'] += 1
                        self.stats['http_ok'] += 1
                        self._sent_trade_ids.add(tid)
                        log.info(f"DISPATCH OK [{code}] {intent['symbol']} {intent['side']} "
                                 f"size={intent['size']:.4f} → {self.url}")
                        return True
                    else:
                        self.stats['http_err'] += 1
                        log.warning(f"DISPATCH non-2xx [{code}] {intent['symbol']}: {body}")
            except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
                self.stats['http_err'] += 1
                if attempt == 1:
                    self.stats['retries'] += 1
                    log.warning(f"DISPATCH attempt {attempt} failed for {intent['symbol']}: {e} — retrying once")
                    time.sleep(0.5)
                    continue
                log.error(f"DISPATCH FINAL FAIL {intent['symbol']}: {e}")
        return False

    def status(self):
        return {
            'url': self.url,
            'dry_run': self.dry_run,
            'sent_trade_ids': len(self._sent_trade_ids),
            **self.stats,
        }
