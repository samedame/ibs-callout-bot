#!/usr/bin/env python3
"""IBS callout bot — daily alerts for the index-ETF IBS mean-reversion strategy.

Rules (frozen 2026-09-12, from the out-of-sample study):
  Universe : SPY, QQQ, IWM, DIA   (equal sleeves; MDY optional)
  ENTRY    : IBS = (close - low) / (high - low) < 0.10  at the daily close
             -> buy at the NEXT regular-session open
  EXIT     : close > previous day's high   OR   10 trading days held
             -> sell at the NEXT open
  Sizing   : one sleeve per ETF (25% of the bot's capital each), no leverage, no averaging down.

The bot only sends alerts; you place the orders (market-on-open works; a limit near the open also works).
State (which sleeves are open) is kept in state.json so exits can be tracked.

Usage:
  python ibs_callout_bot.py --webhook $DISCORD_WEBHOOK          # evening run, after the close
  python ibs_callout_bot.py --webhook $DISCORD_WEBHOOK --morning # pre-open reminder of pending orders
  python ibs_callout_bot.py --dry-run                            # print, don't post
"""
import argparse, datetime as dt, json, os, sys
import urllib.request

import pandas as pd

TICKERS = ["SPY", "QQQ", "IWM", "DIA"]
IBS_ENTRY = 0.10
MAX_HOLD_DAYS = 10
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
UA = {"User-Agent": "Mozilla/5.0 (compatible; ibs-callout-bot/1.0)"}


# ----------------------------------------------------------------------------- data

def fetch_yahoo(ticker, days=90):
    """Daily OHLC from Yahoo's chart endpoint (no key needed)."""
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
           f"?range={days}d&interval=1d&events=div,splits")
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        j = json.load(r)
    res = j["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    idx = pd.to_datetime(res["timestamp"], unit="s", utc=True).tz_convert("America/New_York").normalize().tz_localize(None)
    df = pd.DataFrame({"open": q["open"], "high": q["high"], "low": q["low"], "close": q["close"]}, index=idx)
    return df.dropna()


def fetch_stooq(ticker, days=90):
    """Fallback: Stooq daily CSV."""
    url = f"https://stooq.com/q/d/l/?s={ticker.lower()}.us&i=d"
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        df = pd.read_csv(r)
    df.columns = [c.lower() for c in df.columns]
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    return df[["open", "high", "low", "close"]].tail(days + 5)


def get_bars(ticker):
    errs = []
    for fn in (fetch_yahoo, fetch_stooq):
        try:
            df = fn(ticker)
            if len(df) > 20:
                return df
        except Exception as e:  # noqa
            errs.append(f"{fn.__name__}: {e}")
    raise RuntimeError(f"no data for {ticker}: {'; '.join(errs)}")


def ibs(row):
    rng = row["high"] - row["low"]
    return 0.5 if rng <= 0 else (row["close"] - row["low"]) / rng


# ----------------------------------------------------------------------------- state

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"positions": {}, "history": []}


def save_state(st):
    with open(STATE_FILE, "w") as f:
        json.dump(st, f, indent=1)


# ----------------------------------------------------------------------------- alerts

def post_discord(webhook, content):
    data = json.dumps({"content": content[:1900]}).encode()
    req = urllib.request.Request(webhook, data=data, headers={"Content-Type": "application/json", **UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status


def evening_run(state, dry_run=False):
    lines, new_entries, exits = [], [], []
    for t in TICKERS:
        df = get_bars(t)
        last = df.iloc[-1]
        prev = df.iloc[-2]
        v = ibs(last)
        bar_date = df.index[-1].date().isoformat()
        pos = state["positions"].get(t)
        if pos:
            held = pos.get("bars", 0) + 1
            pos["bars"] = held
            pos["last_bar"] = bar_date
            exit_now = last["close"] > prev["high"] or held >= MAX_HOLD_DAYS
            why = "close above yesterday's high" if last["close"] > prev["high"] else f"{held} trading days held"
            if exit_now:
                exits.append((t, why, float(last["close"]), pos))
        else:
            if v < IBS_ENTRY:
                new_entries.append((t, v, float(last["close"]), bar_date))
        lines.append(f"{t}: close {last['close']:.2f}  IBS {v:.2f}" + ("  [in position]" if pos else ""))

    msg = [f"**IBS callout bot — {dt.date.today().isoformat()}**"]
    if new_entries:
        msg.append("\n__BUY at tomorrow's open__ (25% sleeve each):")
        for t, v, c, d in new_entries:
            msg.append(f"• **{t}** — IBS {v:.2f} on {d} close {c:.2f}")
    if exits:
        msg.append("\n__SELL at tomorrow's open__:")
        for t, why, c, pos in exits:
            msg.append(f"• **{t}** — {why} (entered {pos.get('entry_date')})")
    if not new_entries and not exits:
        msg.append("\nNo action tomorrow.")
    msg.append("\n```\n" + "\n".join(lines) + "\n```")
    text = "\n".join(msg)

    # apply state changes (orders are assumed filled at the next open)
    for t, why, c, pos in exits:
        state["history"].append({**pos, "sym": t, "exit_signal_date": dt.date.today().isoformat(), "reason": why})
        state["positions"].pop(t, None)
    for t, v, c, d in new_entries:
        state["positions"][t] = {"entry_date": d, "signal_ibs": round(v, 4), "bars": 0, "pending": True}
    return text


def morning_run(state):
    pend_in = [t for t, p in state["positions"].items() if p.get("pending")]
    msg = [f"**Pre-open reminder — {dt.date.today().isoformat()}**"]
    if pend_in:
        msg.append("Place BUY (market-on-open) for: " + ", ".join(f"**{t}**" for t in pend_in))
    else:
        msg.append("No new buys today.")
    open_pos = [t for t, p in state["positions"].items() if not p.get("pending")]
    if open_pos:
        msg.append("Currently holding: " + ", ".join(open_pos))
    for t, p in state["positions"].items():
        p["pending"] = False
    return "\n".join(msg)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--webhook", default=os.environ.get("DISCORD_WEBHOOK", ""))
    ap.add_argument("--morning", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    state = load_state()
    text = morning_run(state) if a.morning else evening_run(state, a.dry_run)
    print(text)
    if a.webhook and not a.dry_run:
        post_discord(a.webhook, text)
    if not a.dry_run:
        save_state(state)


if __name__ == "__main__":
    main()
