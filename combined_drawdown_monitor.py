#!/usr/bin/env python3
"""Combined-account drawdown monitor across the IBS bot and the options/VRP bot.

Added 2026-09-13 (bot-addition scan). Finding that motivated this: on the 20 worst single-day
SPY returns 2019-2026, the IBS sleeve was in a position on all 20 of them (avg -1.61% that day),
and daily-return correlation between the two bots is 0.69 overall (0.38 in the worst 5% of
days) -- the two bots are NOT independent risk buckets. Monitoring each bot's drawdown
separately can miss a day that hits both at once.

STATUS: the IBS leg below is fully wired (reuses kill_switch_monitor's trade-return estimate).
The options-bot leg is NOT wired yet -- this script needs that bot's own trade history to
compute a real combined number, and doesn't have it in this session. See "Wiring in the
options bot" below for the schema it expects. Until then this reports the IBS-only view and
says so plainly -- it does not fabricate a combined number from a leg it can't see.

Wiring in the options bot:
  Drop a file named options_state.json next to this script, shaped like:
    {"history": [{"entry_date": "YYYY-MM-DD", "exit_date": "YYYY-MM-DD", "pnl_pct": 0.0123}, ...]}
  where pnl_pct is that trade's realized P&L as a fraction of capital ALLOCATED TO THAT BOT
  (not the combined account). Once present, this script blends it in automatically.

Capital split: set IBS_WEIGHT / OPTIONS_WEIGHT below to your actual dollar split between the
two bots (defaults to an even 50/50 placeholder -- almost certainly not your real split).

Usage:
  python combined_drawdown_monitor.py --webhook $DISCORD_WEBHOOK
  python combined_drawdown_monitor.py --dry-run --status
"""
import argparse, json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kill_switch_monitor import load_history, estimate_trade_returns, post_discord  # noqa: E402

OPTIONS_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "options_state.json")
DRAWDOWN_CEILING = 0.25   # starting point -- tune once the options leg is wired and you've seen real numbers

# Placeholder -- set these to your actual capital split between the two bots.
IBS_WEIGHT = 0.50
OPTIONS_WEIGHT = 0.50


def combined_curve(ibs_trades, options_trades):
    """Blend both legs into one chronological equity curve by exit date. Each leg's trade
    return is weighted by (that bot's capital share) x (25% per IBS sleeve, or 100% for a
    single options trade at a time -- adjust OPTIONS_PER_TRADE_WEIGHT if the options bot ever
    runs more than one concurrent spread)."""
    OPTIONS_PER_TRADE_WEIGHT = 1.0
    events = []
    for t in ibs_trades:
        events.append((t["exit_signal_date"], IBS_WEIGHT * 0.25 * t["ret"]))
    for t in options_trades:
        events.append((t["exit_date"], OPTIONS_WEIGHT * OPTIONS_PER_TRADE_WEIGHT * t["pnl_pct"]))
    events.sort(key=lambda e: e[0])
    equity, peak, max_dd = 1.0, 1.0, 0.0
    for _, weighted_ret in events:
        equity *= (1 + weighted_ret)
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak)
    return max_dd, len(events)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--webhook", default=os.environ.get("DISCORD_WEBHOOK", ""))
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    ibs_history = load_history()
    ibs_trades = estimate_trade_returns(ibs_history) if ibs_history else []

    options_trades = []
    wired = os.path.exists(OPTIONS_STATE_FILE)
    if wired:
        with open(OPTIONS_STATE_FILE) as f:
            options_trades = json.load(f).get("history", [])

    if not ibs_trades and not options_trades:
        print("No closed trades on either leg yet -- nothing to check.")
        return

    max_dd, n_events = combined_curve(ibs_trades, options_trades)

    if not wired:
        summary = (f"Combined drawdown check: IBS-ONLY view (options bot not wired -- see "
                    f"docstring), {len(ibs_trades)} IBS trades, drawdown {max_dd:.1%}. "
                    f"This UNDERSTATES the real combined number.")
    else:
        summary = (f"Combined drawdown: {len(ibs_trades)} IBS + {len(options_trades)} options "
                    f"trades ({n_events} total), blended drawdown {max_dd:.1%} "
                    f"(weights: IBS {IBS_WEIGHT:.0%} / options {OPTIONS_WEIGHT:.0%} -- "
                    f"confirm these match your real capital split).")
    print(summary)

    breach = max_dd > DRAWDOWN_CEILING
    if breach:
        text = (f"**⚠ Combined-account drawdown check -- {max_dd:.1%} exceeds "
                f"{DRAWDOWN_CEILING:.0%} ceiling**\n{summary}")
    elif a.status:
        text = f"**Combined-account drawdown check -- OK**\n{summary}"
    else:
        text = None

    if text:
        print(text)
        if a.webhook and not a.dry_run:
            post_discord(a.webhook, text)


if __name__ == "__main__":
    main()
