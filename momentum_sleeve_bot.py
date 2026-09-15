#!/usr/bin/env python3
"""Momentum sleeve bot -- 12-1 month cross-sectional momentum, alongside the IBS callout bot.

Added 2026-09-13 from the bot-addition scan (see claude/bot-addition-scan-verdict.md in the
"Personal investor" Claude project, and the full report linked from it). STATUS: verified
2026-09-15 (claude/momentum-sleeve-verdict.md), still shadow mode -- do not size with real
capital yet.

This sleeve tested well out-of-sample blended with IBS (2019-2026, equal-risk weighted ~71%
IBS / 29% this sleeve): Sharpe 1.19->1.45, CAGR 16.1%->19.4%, max drawdown -11.7%->-9.8%, and
it holds up in both halves of that window. 2026-09-15 update: ran the same 7-point
pre-registration/verification discipline the IBS rule itself went through. 5 of 6 gating checks
passed -- including a point-in-time-universe reconstruction (Sharpe survives at 1.38 once
survivorship bias is removed) and a walk-forward re-selection (realized Sharpe 1.16, no
lookahead). The one that didn't: a bootstrap confidence interval on the improvement over
IBS-alone includes zero at 90% confidence -- ~93 months isn't enough data yet to fully rule out
noise, even though the point estimate and every structural check are positive. Recommendation:
stay in shadow/paper mode -- watch the alerts, don't size real capital against them -- and
re-test the bootstrap check in ~12 months. Separately, and regardless of that re-test: an
account-size simulation found whole-share rounding is actively damaging below ~$5k (negative
Sharpe at $1k) -- confirm fractional-share support before this sleeve ever gets real capital.

Rule (frozen 2026-09-13, matching the backtested addition):
  Universe : 103 large, liquid US stocks (UNIVERSE below -- the exact list backtested)
  FORM     : trailing 12-month return skipping the most recent month ("12-1 momentum"),
             computed on month-end closes
  SELECT   : top 10 by that score, equal-weighted (10% of the sleeve each)
  REBALANCE: monthly -- the first time this script runs in a new calendar month
  CAPITAL  : recommended ~29% of combined bot capital; IBS keeps the other ~71%
             (see README -- and note most of this universe needs fractional shares on
             a $1k-$10k account)

This bot only sends alerts; you place the orders, exactly like the IBS bot. State (current
holdings) lives in momentum_state.json, separate from the IBS bot's state.json -- the IBS bot
is untouched by this addition.

Usage:
  python momentum_sleeve_bot.py --webhook $DISCORD_WEBHOOK
  python momentum_sleeve_bot.py --dry-run
  python momentum_sleeve_bot.py --dry-run --force-rebalance   # exercise the logic on any day
"""
import argparse, datetime as dt, json, os, sys, time
import urllib.request

import pandas as pd

UNIVERSE = ["AAPL", "ABBV", "ABT", "ACN", "ADBE", "ADI", "ADP", "AMD", "AMGN", "AMZN", "AON",
            "APD", "AVGO", "AXP", "BA", "BAC", "BKNG", "BLK", "BRK-B", "BSX", "C", "CAT", "CB",
            "CI", "CMCSA", "CME", "COST", "CRM", "CSCO", "CSX", "CVX", "DE", "DHR", "DIS", "DUK",
            "ELV", "EQIX", "ETN", "GE", "GILD", "GOOG", "GOOGL", "GS", "HD", "HON", "HUM", "IBM",
            "INTC", "INTU", "ISRG", "ITW", "JNJ", "JPM", "KLAC", "KO", "LIN", "LLY", "LMT", "LOW",
            "MA", "MCD", "MDT", "META", "MO", "MPC", "MRK", "MSFT", "MU", "NFLX", "NKE", "NOC",
            "NVDA", "ORCL", "PANW", "PEP", "PG", "PGR", "PLD", "PM", "PYPL", "QCOM", "REGN",
            "SBUX", "SCHW", "SLB", "SO", "SPGI", "SYK", "T", "TJX", "TMO", "TSLA", "TXN", "UNH",
            "UNP", "USB", "V", "VRTX", "VZ", "WFC", "WMT", "XOM", "ZTS"]
TOP_N = 10
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "momentum_state.json")
UA = {"User-Agent": "Mozilla/5.0 (compatible; momentum-sleeve-bot/1.0)"}


def fetch_yahoo(ticker, days=420):
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
           f"?range={days}d&interval=1d&events=div,splits")
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        j = json.load(r)
    res = j["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    idx = (pd.to_datetime(res["timestamp"], unit="s", utc=True)
           .tz_convert("America/New_York").normalize().tz_localize(None))
    df = pd.DataFrame({"close": q["close"]}, index=idx)
    return df.dropna()


def fetch_stooq(ticker, days=420):
    url = f"https://stooq.com/q/d/l/?s={ticker.lower()}.us&i=d"
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        df = pd.read_csv(r)
    df.columns = [c.lower() for c in df.columns]
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    return df[["close"]].tail(days + 30)


def get_closes(ticker):
    for fn in (fetch_yahoo, fetch_stooq):
        try:
            df = fn(ticker)
            if len(df) > 280:
                return df
        except Exception as e:
            print(f"  ! {ticker} via {fn.__name__}: {e}", file=sys.stderr)
    return None


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"holdings": {}, "last_rebalanced_month": "", "history": []}


def save_state(st):
    with open(STATE_FILE, "w") as f:
        json.dump(st, f, indent=1)


def post_discord(webhook, content):
    data = json.dumps({"content": content[:1900]}).encode()
    req = urllib.request.Request(webhook, data=data, headers={"Content-Type": "application/json", **UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status


def compute_top_n():
    """Fetch the universe, score by trailing 12-1 momentum on month-end closes, return top 10."""
    scores = {}
    for t in UNIVERSE:
        df = get_closes(t)
        if df is None:
            continue
        monthly = df["close"].resample("ME").last()
        if len(monthly) < 13:
            continue
        p_recent = monthly.iloc[-2]    # last FULLY completed month (skip the in-progress one)
        p_distant = monthly.iloc[-13]  # 12 months before that
        if p_distant and p_distant > 0:
            scores[t] = float(p_recent / p_distant - 1)
        time.sleep(0.08)
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    return [t for t, _ in ranked[:TOP_N]], scores


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--webhook", default=os.environ.get("DISCORD_WEBHOOK", ""))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force-rebalance", action="store_true",
                     help="rebalance regardless of month, for testing")
    a = ap.parse_args()

    state = load_state()
    today = dt.date.today()
    this_month = today.strftime("%Y-%m")

    if this_month == state.get("last_rebalanced_month") and not a.force_rebalance:
        print(f"Already rebalanced for {this_month}. Next rebalance: first run in a new month.")
        return

    print(f"Scoring {len(UNIVERSE)}-name universe on trailing 12-1 momentum...")
    top10, scores = compute_top_n()
    if len(top10) < TOP_N:
        print(f"WARNING: only {len(top10)}/{TOP_N} names scored this run -- data issue, "
              f"not rebalancing.", file=sys.stderr)
        return

    current = set(state["holdings"].keys())
    target = set(top10)
    sells, buys = current - target, target - current

    msg = [f"**Momentum sleeve -- rebalance {today.isoformat()}**",
           "_(prototype -- shadow/paper mode; see README before sizing with real capital)_"]
    if sells:
        msg.append("\n__SELL at next open__: " + ", ".join(sorted(sells)))
    if buys:
        msg.append("\n__BUY at next open__ (equal weight, ~{:.0f}% of sleeve each): ".format(100 / TOP_N)
                    + ", ".join(sorted(buys)))
    if not sells and not buys:
        msg.append("\nNo changes -- same top 10 as last month.")
    msg.append("\n_Holding: " + ", ".join(sorted(target)) + "_")
    text = "\n".join(msg)
    print(text)

    if not a.dry_run:
        for t in sells:
            pos = state["holdings"].pop(t, {})
            state["history"].append({"sym": t, **pos, "exit_month": this_month})
        for t in buys:
            state["holdings"][t] = {"entry_month": this_month}
        state["last_rebalanced_month"] = this_month
        save_state(state)
        if a.webhook:
            post_discord(a.webhook, text)


if __name__ == "__main__":
    main()
