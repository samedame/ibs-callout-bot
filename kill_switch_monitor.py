#!/usr/bin/env python3
"""Kill-switch monitor for the IBS callout bot.

Added 2026-09-13 (bot-addition scan). Automates the check the bot's own README already
specified on paper: "if the average trade over the next 100 alerts is below +0.2%, stop and
re-test" -- plus the drawdown (>25%) and win-rate (<60%/100 alerts) criteria from the same
verdict record (claude/callout-bot-verdict.md).

This bot places no orders and changes no state -- it only READS state.json (never writes it)
and posts a Discord alert when a threshold is breached. It does not touch ibs_callout_bot.py.

How trade returns are estimated: state.json's history records entry_date (the day whose CLOSE
triggered the buy) and exit_signal_date (the day whose close/hold-limit triggered the sell) --
per the bot's own rule, the actual fill happens at the NEXT session's open in both cases. This
monitor fetches each ticker's daily OHLC and uses the open price on the trading day immediately
following entry_date and immediately following exit_signal_date as the fill proxy. That matches
the bot's documented execution rule but is still a proxy for your real fills -- if you routinely
fill materially away from the open, treat these numbers as directional, not exact.

Combined drawdown across the 4 ETF sleeves is approximated by compounding each closed trade's
return at 25% weight, in order of exit date. This is a simplification (it doesn't model true
day-by-day overlapping exposure while multiple sleeves are open at once) but is a reasonable,
auditable tripwire -- not a precision performance report.

Usage:
  python kill_switch_monitor.py --webhook $DISCORD_WEBHOOK            # alerts only on breach
  python kill_switch_monitor.py --webhook $DISCORD_WEBHOOK --status   # always posts a summary
  python kill_switch_monitor.py --dry-run                             # print, don't post
"""
import argparse, json, os, sys
import urllib.request

import pandas as pd

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
UA = {"User-Agent": "Mozilla/5.0 (compatible; ibs-kill-switch-monitor/1.0)"}
WINDOW = 100                  # "over 100 consecutive alerts" per the verdict doc
AVG_TRADE_FLOOR = 0.002       # 0.2%
DRAWDOWN_CEILING = 0.25       # 25%
WIN_RATE_FLOOR = 0.60         # 60%
SLEEVE_WEIGHT = 0.25          # 4 equal ETF sleeves


def fetch_yahoo(ticker, days=1100):
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
           f"?range={days}d&interval=1d&events=div,splits")
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        j = json.load(r)
    res = j["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    idx = (pd.to_datetime(res["timestamp"], unit="s", utc=True)
           .tz_convert("America/New_York").normalize().tz_localize(None))
    df = pd.DataFrame({"open": q["open"], "close": q["close"]}, index=idx)
    return df.dropna()


def fetch_stooq(ticker, days=1100):
    url = f"https://stooq.com/q/d/l/?s={ticker.lower()}.us&i=d"
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        df = pd.read_csv(r)
    df.columns = [c.lower() for c in df.columns]
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    return df[["open", "close"]].tail(days + 5)


def get_bars(ticker):
    for fn in (fetch_yahoo, fetch_stooq):
        try:
            df = fn(ticker)
            if len(df) > 20:
                return df
        except Exception as e:
            print(f"  ! {ticker} via {fn.__name__}: {e}", file=sys.stderr)
    return None


def next_session_open(df, after_date):
    ts = pd.Timestamp(after_date)
    later = df.index[df.index > ts]
    if len(later) == 0:
        return None
    return float(df.loc[later[0], "open"])


def load_history():
    with open(STATE_FILE) as f:
        return json.load(f).get("history", [])


def estimate_trade_returns(history):
    bars_cache, out = {}, []
    for h in history:
        t = h["sym"]
        if t not in bars_cache:
            bars_cache[t] = get_bars(t)
        df = bars_cache[t]
        if df is None:
            continue
        entry_px = next_session_open(df, h["entry_date"])
        exit_px = next_session_open(df, h["exit_signal_date"])
        if entry_px is None or exit_px is None or entry_px <= 0:
            continue
        out.append({"sym": t, "exit_signal_date": h["exit_signal_date"],
                    "ret": exit_px / entry_px - 1.0})
    out.sort(key=lambda r: r["exit_signal_date"])
    return out


def blended_drawdown(trades):
    equity, peak, max_dd = 1.0, 1.0, 0.0
    for tr in trades:
        equity *= (1 + SLEEVE_WEIGHT * tr["ret"])
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak)
    return max_dd


def post_discord(webhook, content):
    data = json.dumps({"content": content[:1900]}).encode()
    req = urllib.request.Request(webhook, data=data, headers={"Content-Type": "application/json", **UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--webhook", default=os.environ.get("DISCORD_WEBHOOK", ""))
    ap.add_argument("--status", action="store_true", help="post a summary even with no breach")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    history = load_history()
    if len(history) == 0:
        print("No closed trades yet -- nothing to check.")
        return

    trades = estimate_trade_returns(history)
    window = trades[-WINDOW:]
    n = len(window)
    avg_trade = sum(t["ret"] for t in window) / n
    win_rate = sum(1 for t in window if t["ret"] > 0) / n
    max_dd = blended_drawdown(trades)  # drawdown uses FULL history, not just the trailing window

    breaches = []
    if avg_trade < AVG_TRADE_FLOOR:
        breaches.append(f"avg trade {avg_trade:+.2%} over last {n} < floor {AVG_TRADE_FLOOR:.1%}")
    if max_dd > DRAWDOWN_CEILING:
        breaches.append(f"blended drawdown {max_dd:.1%} > ceiling {DRAWDOWN_CEILING:.0%}")
    if win_rate < WIN_RATE_FLOOR:
        breaches.append(f"win rate {win_rate:.1%} over last {n} < floor {WIN_RATE_FLOOR:.0%}")

    primed = n >= WINDOW
    summary = (f"IBS bot health -- {n} trade(s) in window (primed: {primed}), "
               f"avg trade {avg_trade:+.2%}, win rate {win_rate:.1%}, "
               f"blended drawdown {max_dd:.1%}")
    print(summary)

    if breaches:
        text = ("**⚠ IBS bot kill-switch check -- threshold breached**\n"
                + "\n".join(f"- {b}" for b in breaches)
                + f"\n\n{summary}"
                + "\n\nPer the verdict record: stop and re-test before the next signal.")
    elif a.status:
        text = f"**IBS bot health check -- OK**\n{summary}"
    else:
        text = None  # healthy and not asked for a status post -- stay quiet

    if text:
        print(text)
        if a.webhook and not a.dry_run:
            post_discord(a.webhook, text)


if __name__ == "__main__":
    main()
