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

# Shared with the IBS bot (same repo): New York clock, NYSE calendar, Discord post with retries.
from ibs_callout_bot import (NOTIFIED_FILE, expected_last_session, fmt_day, next_session, now_et, parse_now,
                             post_discord, prev_session)

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
# A rebalance is only trusted when almost the whole universe could be scored. Before, "10 names scored" was enough,
# so a Yahoo rate-limit day could have produced a top 10 of whatever happened to download.
MIN_SCORED = 95
# The score below is P(last completed month-end) / P(11 month-ends earlier) - 1, i.e. it INCLUDES the most recent
# completed month. The research description of "12-1" (claude/momentum-sleeve-preregistration.md: "12 months back to
# 1 month back", panel.shift(1) / panel.shift(12) on month-end rows) skips that month: P(M-2) / P(M-13) for a rebalance
# in month M. If the research script decided on the month-end row and traded the next month, flip this to True.
# Left False because it changes which stocks get picked; confirm against the research script first (README, "Open question").
SKIP_LAST_COMPLETED_MONTH = False
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


def get_closes(ticker, attempts=2, sleep=time.sleep):
    for name, fn in (("yahoo", fetch_yahoo), ("stooq", fetch_stooq)):
        for i in range(attempts):
            try:
                df = fn(ticker)
                if len(df) > 280:
                    return df
                print(f"  ! {ticker} via {name}: only {len(df)} bars", file=sys.stderr)
                break
            except Exception as e:
                print(f"  ! {ticker} via {name}: {e}", file=sys.stderr)
                if i < attempts - 1:
                    sleep(2)
    return None


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"holdings": {}, "last_rebalanced_month": "", "history": []}


def save_state(st):
    with open(STATE_FILE, "w") as f:
        json.dump(st, f, indent=1)


def completed_months(df, month_start):
    """Month-end closes of the months that are OVER as of `month_start` (the first day of the current New York month).
    The in-progress month is never included: a run on the evening of Oct 1 must not treat Oct 1's close as a month-end."""
    per = df["close"].groupby(df.index.to_period("M")).last()
    return per[per.index < pd.Period(month_start, "M")]


def has_month_end(df, month_start):
    """The newest bar must reach the last session of the previous month, or its month-end close isn't in the file."""
    return len(df) > 0 and df.index[-1].date() >= prev_session(month_start)


def score_one(df, month_start, skip_last=None):
    skip_last = SKIP_LAST_COMPLETED_MONTH if skip_last is None else skip_last
    cm = completed_months(df, month_start)
    need = 13 if skip_last else 12
    if len(cm) < need:
        return None
    p_recent = cm.iloc[-2] if skip_last else cm.iloc[-1]
    p_distant = cm.iloc[-13] if skip_last else cm.iloc[-12]
    if not p_distant or p_distant <= 0 or pd.isna(p_recent) or pd.isna(p_distant):
        return None
    return float(p_recent / p_distant - 1)


def compute_top_n(month_start, get=get_closes, sleep=time.sleep):
    """Fetch the universe, score each name on trailing momentum, return (top 10, all scores, names that could not be scored)."""
    scores, missing = {}, []
    for t in UNIVERSE:
        df = get(t)
        sc = score_one(df, month_start) if df is not None and has_month_end(df, month_start) else None
        if sc is None:
            missing.append(t)
        else:
            scores[t] = sc
        sleep(0.08)
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    return [t for t, _ in ranked[:TOP_N]], scores, missing


def skipped_notice(this_month, n_scored, missing):
    shown = ", ".join(missing[:25]) + (f" … (+{len(missing) - 25} more)" if len(missing) > 25 else "")
    return (f"⚠️ **Momentum sleeve — the {this_month} rebalance was NOT done.** Only {n_scored} of {len(UNIVERSE)} names could be "
            f"scored (need {MIN_SCORED}); unavailable: {shown}.\nNothing was changed and your holdings list is the same as "
            f"last month's. The daily check tries again tomorrow. (Shadow/paper mode: no money depends on this.)")


def main(argv=None, now=None, get=get_closes, post=post_discord, sleep=time.sleep):
    ap = argparse.ArgumentParser()
    ap.add_argument("--webhook", default=os.environ.get("DISCORD_WEBHOOK", ""))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force-rebalance", action="store_true",
                     help="rebalance regardless of month, for testing")
    ap.add_argument("--now", help="test hook: pretend it is this time (ISO-8601; naive = New York)")
    a = ap.parse_args(argv)

    now = now or (parse_now(a.now) if a.now else now_et())
    state = load_state()
    today = now.date()                      # New York date: the runner's clock is UTC, which rolls over at 8 PM ET
    this_month = today.strftime("%Y-%m")
    month_start = today.replace(day=1)

    if this_month == state.get("last_rebalanced_month") and not a.force_rebalance:
        print(f"Already rebalanced for {this_month}. Next rebalance: first run in a new month.")
        return 0

    print(f"Scoring {len(UNIVERSE)}-name universe on trailing 12-1 momentum...")
    top10, scores, missing = compute_top_n(month_start, get, sleep)
    if len(scores) < MIN_SCORED:
        notice = skipped_notice(this_month, len(scores), missing)
        print(notice, file=sys.stderr)
        if a.webhook and not a.dry_run:
            try:
                post(a.webhook, notice)
                with open(NOTIFIED_FILE, "w") as f:      # Discord already has the reason: no second "run failed" message
                    f.write(notice[:200])
            except Exception as e:  # noqa
                print(f"could not post the notice either: {e}", file=sys.stderr)
        return 1

    current = set(state["holdings"].keys())
    target = set(top10)
    sells, buys = current - target, target - current
    nxt = next_session(expected_last_session(now))

    msg = [f"**Momentum sleeve -- rebalance {fmt_day(today)}**",
           "_(prototype -- shadow/paper mode; see README before sizing with real capital)_"]
    if sells:
        msg.append(f"\n__SELL at the {fmt_day(nxt)} open__: " + ", ".join(sorted(sells)))
    if buys:
        msg.append(f"\n__BUY at the {fmt_day(nxt)} open__ (equal weight, ~{100 / TOP_N:.0f}% of the sleeve each): "
                   + ", ".join(sorted(buys)))
    if not sells and not buys:
        msg.append("\nNo changes -- same top 10 as last month.")
    msg.append("\n_Holding: " + ", ".join(sorted(target)) + "_")
    if missing:
        msg.append(f"_(Scored {len(scores)} of {len(UNIVERSE)} names; unavailable today: {', '.join(missing)}.)_")
    text = "\n".join(msg)
    print(text)

    if a.dry_run:
        return 0
    if not a.webhook:
        print("DISCORD_WEBHOOK is not set: the rebalance was NOT posted and NOT recorded; it will be retried.", file=sys.stderr)
        return 1
    try:
        post(a.webhook, text)
    except Exception as e:  # noqa
        print(f"Discord refused the message ({e}): the rebalance was NOT recorded; the next daily run retries it.", file=sys.stderr)
        return 1
    # Recorded only AFTER the message went out, so a failed post can never leave a rebalance that Sam never saw.
    for t in sells:
        pos = state["holdings"].pop(t, {})
        state["history"].append({"sym": t, **pos, "exit_month": this_month})
    for t in buys:
        state["holdings"][t] = {"entry_month": this_month}
    state["last_rebalanced_month"] = this_month
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
