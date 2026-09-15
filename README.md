# IBS callout bot

Daily alerts for the index-ETF IBS mean-reversion strategy (the strategy that won the 2026-09 strategy sweep:
105 tested hypotheses across equities, options, volatility, crypto, FX and intraday).

## The rule

    IBS = (close - low) / (high - low)          # where today closed inside today's range

    BUY  when IBS < 0.10 at the close           -> at the next open, 25% of the bot sleeve
    SELL when close > previous day's high       -> at the next open
         or after 10 trading days in the trade

Universe: SPY, QQQ, IWM, DIA — one sleeve each, no leverage, no averaging down.

Out-of-sample (2019-01 → 2026-09, never used for parameter selection, net of costs):
Sharpe ≈ 0.97–1.01, CAGR ≈ 13.7–15%, max drawdown ≈ −18%, ~75 alerts/yr, 73% win rate, average trade +0.68%.

## Setup

1. Create a Discord webhook (Server settings → Integrations → Webhooks), copy the URL.
2. In this repo: Settings → Secrets and variables → Actions → New secret, name `DISCORD_WEBHOOK`.
3. Push these files. The workflow runs after the close (21:15 UTC) and pre-open (13:00 UTC) on weekdays.
4. Test locally first: `python ibs_callout_bot.py --dry-run`

`state.json` tracks open sleeves so exits can be signalled; the workflow commits it back to the repo.

## Rules of the road

- Execute at the open with market-on-open orders; skip a trade if you can't.
- Fractional shares are needed below ~$3k of capital (SPY/QQQ are ~$700+ per share).
- Do not add a "gut feel" filter: the tested edge is the mechanical rule.
- Review after 12 months: if the average trade over the next 100 alerts is below +0.2%, stop and re-test.

---

## Addition scan (2026-09-13)

Full findings: `claude/bot-addition-scan-verdict.md` in the Personal Investor project (and the
linked full report). Three new components were added here as a result. **`ibs_callout_bot.py`
above is untouched** -- every variant tested against it underperformed the deployed rule.

### 1. Momentum sleeve (`momentum_sleeve_bot.py`) -- verified 2026-09-15, still shadow mode

A 12-1 month cross-sectional momentum sleeve (top 10 of 103 large-caps, monthly rebalance),
run alongside IBS. Backtested blend (2019-2026, risk-weighted ~71% IBS / 29% momentum):
Sharpe 1.19->1.45, CAGR 16.1%->19.4%, max drawdown -11.7%->-9.8%.

**Update 2026-09-15:** ran the same 7-point pre-registration/verification discipline the IBS
rule itself went through. Full record: `claude/momentum-sleeve-verdict.md` in the Personal
Investor project. Short version -- 5 of 6 gating checks passed cleanly (independent
reimplementation, point-in-time-universe restriction, parameter-neighborhood check,
walk-forward re-selection, execution/cost stress all held up; point-in-time restriction alone
knocks the combined Sharpe from 1.45 down to a still-solid 1.38). The one that didn't: a
bootstrap confidence interval on the *improvement* over IBS-alone includes zero at 90%
confidence -- with ~93 months of data, the data can't yet rule out that this is noise on top of
an already-strong baseline, even though the point estimate and every structural check say real
diversification benefit. **Net: stay in shadow/paper mode, do not size with real capital yet.**
Re-test the bootstrap check in ~12 months once more paper-tracked months exist.

**Before sizing this with real capital, independent of the above:** the account-size
simulation in the verdict doc found whole-share rounding is not just suboptimal but actively
damaging at $1,000 (CAGR 1.8% vs. 23.5%, Sharpe goes negative) and a real 2-4pp CAGR drag even
at $5k-$10k -- **confirm your broker supports fractional shares for all 103 names before this
sleeve ever gets real capital**, regardless of what the bootstrap re-test shows.

**Capital split:** the backtest's own optimum was ~71/29 (IBS/momentum). E.g. on $5,000: about
$3,550 to the 4 IBS sleeves (~$887 each) and $1,450 across 10 momentum names (~$145 each) --
see the fractional-share note above.

Setup: same pattern as the IBS bot (`momentum-bot.yml` workflow, same `DISCORD_WEBHOOK`
secret). State lives in `momentum_state.json`, separate from the IBS bot's `state.json`.

### 2. Kill-switch monitor (`kill_switch_monitor.py`)

Automates the review rule already in this file ("if the average trade over the next 100 alerts
is below +0.2%, stop and re-test") plus the drawdown (>25%) and win-rate (<60%/100 alerts)
criteria from the verdict record. Read-only against `state.json` -- never writes it, never
touches the live bot. Runs weekly (`kill-switch.yml`), alerts to Discord only when a threshold
is actually breached (pass `--status` for an always-on summary, e.g. for manual runs).

Trade returns are *estimated* from the next session's open after each signal date (matching
the bot's own "execute at the next open" rule) -- a proxy for your real fills, not your actual
account P&L.

### 3. Combined-account drawdown monitor (`combined_drawdown_monitor.py`) -- IBS side only so far

Motivated by a real finding: on the 20 worst SPY days 2019-2026, the IBS sleeve was in a
position on **all 20**, averaging -1.61% that day -- the two bots are correlated on exactly the
days it matters most (0.69 overall, 0.38 in the worst 5% of days), so drawdown needs watching
jointly, not just per-bot.

**This only has the IBS leg wired right now.** It needs the options bot's own trade history to
compute a real combined number -- see the schema documented at the top of the script
(`options_state.json`: a list of `{entry_date, exit_date, pnl_pct}`). Until that file exists
next to the script, it reports the IBS-only view and says so explicitly rather than guessing.
Also set `IBS_WEIGHT` / `OPTIONS_WEIGHT` in the script to your real capital split (defaults to
a 50/50 placeholder). No schedule yet in `combined-drawdown.yml` (`workflow_dispatch` only) --
uncomment the cron once the options leg is wired in.

### Not changed, on purpose

Per the same scan: no change to the IBS bot's ETF universe, threshold, or weighting (every
tested variant underperformed), and no short-vol/SVXY overlay was added at any point (tested
return-negative with an -81% max drawdown on the real tradeable instrument).
