#!/usr/bin/env python3
"""Manual live-data check for this bot's Tradier setup. Read-only: posts nothing, writes nothing, never prints the token.

Run from the "Tradier check (manual)" workflow in the Actions tab (it uses this repo's own secrets), or locally:
    TRADIER_API_KEY=... [TRADIER_BASE_URL=...] python tradier_check.py

Exit 0  = production host and (when the market is open) quotes under 2 minutes old.
Exit 1  = not live: no key, sandbox host (about 15 min delayed), stale quotes, or Tradier unreachable.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tradier  # noqa: E402  (this repo's own client; standard library only)

FRESH_MAX_MIN = 2.0


def _age_min(rec, now):
    stamps = [rec.get(k) for k in ("trade_date", "bid_date", "ask_date") if rec.get(k)]
    return None if not stamps else max(0.0, now - max(stamps) / 1000.0) / 60.0


def run(env=None, symbol="SPY", now=None, say=print, **kw):
    """kw is passed to every Tradier call (tests inject a fake opener)."""
    now = time.time() if now is None else now
    if not tradier.configured(env):
        say("FAIL: the TRADIER_API_KEY secret is empty or missing in this repo.")
        return 1
    try:
        mode = tradier.mode(env)
    except tradier.TradierError as e:
        say(f"FAIL: {e}")
        return 1
    base = ((env if env is not None else os.environ).get("TRADIER_BASE_URL") or "").strip()
    say(f"Host: {mode} (TRADIER_BASE_URL secret is {'set to ' + base if base else 'empty, so production is the default'})")
    if mode == "sandbox":
        say("FAIL: sandbox host. Sandbox data is about 15 minutes delayed. Delete the TRADIER_BASE_URL secret or set it to "
            "https://api.tradier.com/v1")
        return 1
    try:
        clock = tradier._get("/markets/clock", env=env, **kw).get("clock", {})
        qs = tradier.quotes([symbol], env=env, **kw)
        q = qs.get(symbol.upper())
        if not q:
            say(f"FAIL: Tradier returned no quote for {symbol}.")
            return 1
        exps = tradier.expirations(symbol, env=env, **kw)
        oage = None
        if exps and q.get("last"):
            chain = tradier.chain(symbol, exps[0], greeks=False, env=env, **kw)
            spot = float(q["last"])
            near = sorted(chain, key=lambda o: abs(float(o["strike"]) - spot))[:6]
            ages = [a for a in (_age_min(o, now) for o in near) if a is not None]
            oage = min(ages) if ages else None
            say(f"Option chain {symbol} {exps[0]}: {len(chain)} contracts, freshest ATM quote "
                + ("has no timestamp" if oage is None else f"{oage:.1f} min old"))
    except tradier.TradierError as e:
        say(f"FAIL: {e}")
        return 1
    qage = _age_min(q, now)
    say(f"Market: {clock.get('state')}. {symbol} last={q.get('last')} bid={q.get('bid')} ask={q.get('ask')}, newest stamp "
        + ("none" if qage is None else f"{qage:.1f} min old"))
    if clock.get("state") != "open":
        say("PASS (key works on the production host). Freshness can't be judged while the market is closed; rerun 9:30-16:00 ET.")
        return 0
    worst = max([a for a in (qage, oage) if a is not None], default=None)
    if worst is None:
        say("FAIL: market is open but no quote timestamps came back, so freshness can't be confirmed.")
        return 1
    if worst > FRESH_MAX_MIN:
        say(f"FAIL: quotes are {worst:.0f} min old while the market is open (delayed feed).")
        return 1
    say("PASS: LIVE. Production host, market open, quotes under 2 minutes old.")
    return 0


if __name__ == "__main__":
    sys.exit(run(symbol=(sys.argv[1] if len(sys.argv) > 1 else "SPY").upper()))
