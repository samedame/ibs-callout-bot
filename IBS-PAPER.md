# IBS 85% cap paper comparison

Open **Actions → IBS 85% cap paper comparison** for a daily scorecard. The workflow runs on weekdays at 22:43 UTC and can also be started with **Run workflow**. The first successful run initializes both portfolios at $1,500, evaluates that session's close, and queues the first paper trades for the next session's open. Subsequent runs catch up missed sessions from finalized bars.

Both paper portfolios use the live signal evaluator, SPY/QQQ/IWM/DIA, fractional shares, 5 basis points of cost per side, and one-session settlement. The 85% entry cap evenly reduces simultaneous buys when needed. Existing positions can rise above 85% before their ordinary exits. The paper ledger is price-only because the bot's price feed does not provide a consistent dividend series across providers. Historical adjusted-price replay and paper results can differ for that reason.

The two modeled accounts are tracked in `paper_ibs/comparison.json`, with closing equity, drawdown, exposure, paper fills, skips and queued orders. This is a paper-to-paper comparison using the same quotes and start date. The workflow publishes its scorecard in GitHub Actions and sends no alerts.
