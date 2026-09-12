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
