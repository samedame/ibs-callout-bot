"""Momentum sleeve bot and kill-switch monitor: month label, completed-month scoring, coverage guard, delivery order."""
import contextlib
import datetime as dt
import io
import json
import os
import tempfile
import unittest
from unittest import mock

import pandas as pd

import ibs_callout_bot as ibs_bot
import kill_switch_monitor as ks
import momentum_sleeve_bot as mom

D = dt.date
ET = ibs_bot.ET


def at(y, m, d, hh, mm=0):
    return dt.datetime(y, m, d, hh, mm, tzinfo=ET)


def daily(end, growth=0.0, months=18, level=100.0, last_close=None):
    """Business-day closes where every day of a month closes at that month's value level*(1+growth)^k (so the
    month-end close is exactly that value). `last_close` overrides the final bar."""
    idx = pd.bdate_range(end=pd.Timestamp(end), periods=int(months * 22))
    per = idx.to_period("M")
    val = {p: level * (1 + growth) ** i for i, p in enumerate(sorted(set(per)))}
    df = pd.DataFrame({"close": [val[p] for p in per]}, index=idx)
    if last_close is not None:
        df.iloc[-1, 0] = last_close
    return df


def universe(end, **over):
    """103 tickers; growth rises with list position so the top 10 are the last 10 in UNIVERSE."""
    out = {}
    for i, t in enumerate(mom.UNIVERSE):
        out[t] = over.get(t, daily(end, growth=0.0005 * i))
    return out


TOP = sorted(mom.UNIVERSE[-10:])


class Months(unittest.TestCase):
    def test_in_progress_month_is_never_a_month_end(self):
        df = daily(D(2026, 10, 1))                                   # Thursday, first trading day of October
        cm = mom.completed_months(df, D(2026, 10, 1))
        self.assertEqual(str(cm.index[-1]), "2026-09")

    def test_evening_of_the_last_day_of_the_month_is_still_that_month(self):
        # Sep 30, 9:30 PM ET is 01:30 UTC Oct 1. The old UTC date called this "October" and rebalanced using August.
        df = daily(D(2026, 9, 30))
        cm = mom.completed_months(df, D(2026, 9, 1))
        self.assertEqual(str(cm.index[-1]), "2026-08")

    def test_has_month_end(self):
        self.assertFalse(mom.has_month_end(daily(D(2026, 9, 29)), D(2026, 10, 1)))
        self.assertTrue(mom.has_month_end(daily(D(2026, 9, 30)), D(2026, 10, 1)))
        self.assertTrue(mom.has_month_end(daily(D(2026, 10, 1)), D(2026, 10, 1)))
        self.assertFalse(mom.has_month_end(daily(D(2026, 9, 30))[:0], D(2026, 10, 1)))

    def test_default_score_is_last_completed_month_over_eleven_months_earlier(self):
        df = daily(D(2026, 10, 1), growth=0.02)
        self.assertAlmostEqual(mom.score_one(df, D(2026, 10, 1), skip_last=False), 1.02 ** 11 - 1, places=9)

    def test_skip_option_ignores_the_most_recent_completed_month(self):
        flat = daily(D(2026, 9, 30), growth=0.0)
        spike = flat.copy()
        sept = spike.index.to_period("M") == pd.Period("2026-09")
        spike.loc[sept, "close"] = 150.0                              # September (the last completed month) jumps 50%
        self.assertAlmostEqual(mom.score_one(spike, D(2026, 10, 1), skip_last=False), 0.5, places=9)
        self.assertAlmostEqual(mom.score_one(spike, D(2026, 10, 1), skip_last=True), 0.0, places=9)

    def test_too_little_history_is_unscored(self):
        self.assertIsNone(mom.score_one(daily(D(2026, 10, 1), months=9), D(2026, 10, 1)))


class TopN(unittest.TestCase):
    MS = D(2026, 10, 1)

    def test_picks_the_ten_best_and_lists_what_it_could_not_score(self):
        data = universe(D(2026, 10, 1))
        data["AAPL"] = None
        data["ZTS"] = daily(D(2026, 9, 25))                           # stale: no September month-end in the file
        top, scores, missing = mom.compute_top_n(self.MS, get=data.get, sleep=lambda s: None)
        self.assertEqual(len(top), 10)
        self.assertEqual(sorted(top), sorted(mom.UNIVERSE[-11:-1]))      # ZTS would have ranked first but is stale
        self.assertEqual(sorted(missing), ["AAPL", "ZTS"])
        self.assertEqual(len(scores), 101)
        self.assertNotIn("AAPL", scores)

    def test_ranking_is_deterministic_on_ties(self):
        data = {t: daily(D(2026, 10, 1), growth=0.01) for t in mom.UNIVERSE}
        top, _, _ = mom.compute_top_n(self.MS, get=data.get, sleep=lambda s: None)
        self.assertEqual(top, sorted(mom.UNIVERSE)[:10])


class Main(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "momentum_state.json")
        self.marker = os.path.join(self.tmp.name, ".notified")
        for name, value in (("STATE_FILE", self.path), ("NOTIFIED_FILE", self.marker)):
            p = mock.patch.object(mom, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.posts = []

    def post(self, webhook, text):
        self.posts.append(text)

    def run_main(self, now, data, argv=(), post=None, webhook="https://discord.test/h"):
        args = list(argv) + (["--webhook", webhook] if webhook else [])
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            rc = mom.main(args, now=now, get=data.get, post=post or self.post, sleep=lambda s: None)
        self.out, self.err = out.getvalue(), err.getvalue()
        return rc

    def write(self, state):
        with open(self.path, "w") as f:
            json.dump(state, f)

    def saved(self):
        with open(self.path) as f:
            return json.load(f)

    def test_first_run_of_a_new_month_posts_the_rebalance_and_records_it(self):
        self.write({"holdings": {"AAPL": {"entry_month": "2026-09"}}, "last_rebalanced_month": "2026-09", "history": []})
        rc = self.run_main(at(2026, 10, 1, 17, 25), universe(D(2026, 10, 1)))
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.posts), 1)
        text = self.posts[0]
        self.assertIn("rebalance Thu Oct 1", text)
        self.assertIn("SELL at the Fri Oct 2 open__: AAPL", text)
        self.assertIn("BUY at the Fri Oct 2 open__", text)
        st = self.saved()
        self.assertEqual(sorted(st["holdings"]), TOP)
        self.assertEqual(st["last_rebalanced_month"], "2026-10")
        self.assertEqual(st["history"][0]["sym"], "AAPL")

    def test_the_evening_of_the_last_day_of_the_month_does_not_rebalance_early(self):
        """The 10/01 bug: a run at 9:30 PM ET on Sep 30 (01:30 UTC Oct 1) used the UTC date, called it October and scored
        August's month-end because September looked 'in progress'. Now it is still September and already done."""
        self.write({"holdings": {}, "last_rebalanced_month": "2026-09", "history": []})
        rc = self.run_main(at(2026, 9, 30, 21, 30), universe(D(2026, 9, 30)))
        self.assertEqual(rc, 0)
        self.assertEqual(self.posts, [])
        self.assertIn("Already rebalanced for 2026-09", self.out)

    def test_same_month_second_run_is_a_no_op_and_force_overrides(self):
        self.write({"holdings": {t: {"entry_month": "2026-10"} for t in TOP}, "last_rebalanced_month": "2026-10", "history": []})
        data = universe(D(2026, 10, 1))
        self.assertEqual(self.run_main(at(2026, 10, 5, 17, 25), data), 0)
        self.assertEqual(self.posts, [])
        self.assertEqual(self.run_main(at(2026, 10, 5, 17, 25), data, argv=["--force-rebalance"]), 0)
        self.assertIn("No changes -- same top 10 as last month.", self.posts[0])

    def test_too_few_names_scored_means_no_rebalance_a_notice_and_a_failed_run(self):
        self.write({"holdings": {"AAPL": {"entry_month": "2026-09"}}, "last_rebalanced_month": "2026-09", "history": []})
        data = universe(D(2026, 10, 1))
        for t in mom.UNIVERSE[:20]:
            data[t] = None                                            # a Yahoo rate-limit day
        before = self.saved()
        rc = self.run_main(at(2026, 10, 1, 17, 25), data)
        self.assertEqual(rc, 1)
        self.assertEqual(self.saved(), before)
        self.assertEqual(len(self.posts), 1)
        self.assertIn("rebalance was NOT done", self.posts[0])
        self.assertIn("Only 83 of 103 names", self.posts[0])
        self.assertIn("AAPL", self.posts[0])
        self.assertTrue(os.path.exists(self.marker))

    def test_a_handful_of_missing_names_is_tolerated_and_named(self):
        self.write({"holdings": {}, "last_rebalanced_month": "2026-09", "history": []})
        data = universe(D(2026, 10, 1))
        data["INTC"] = None
        data["T"] = None
        rc = self.run_main(at(2026, 10, 1, 17, 25), data)
        self.assertEqual(rc, 0)
        self.assertIn("Scored 101 of 103 names; unavailable today: INTC, T.", self.posts[0])

    def test_a_failed_post_records_nothing_so_tomorrow_retries(self):
        self.write({"holdings": {}, "last_rebalanced_month": "2026-09", "history": []})
        before = self.saved()

        def boom(webhook, text):
            raise ibs_bot.PostFailed("HTTP 500")

        rc = self.run_main(at(2026, 10, 1, 17, 25), universe(D(2026, 10, 1)), post=boom)
        self.assertEqual(rc, 1)
        self.assertEqual(self.saved(), before)
        self.assertIn("NOT recorded", self.err)

    def test_no_webhook_is_a_failure_not_a_silent_success(self):
        self.write({"holdings": {}, "last_rebalanced_month": "2026-09", "history": []})
        before = self.saved()
        rc = self.run_main(at(2026, 10, 1, 17, 25), universe(D(2026, 10, 1)), webhook="")
        self.assertEqual(rc, 1)
        self.assertEqual(self.saved(), before)

    def test_dry_run_posts_and_saves_nothing(self):
        self.write({"holdings": {}, "last_rebalanced_month": "2026-09", "history": []})
        before = self.saved()
        rc = self.run_main(at(2026, 10, 1, 17, 25), universe(D(2026, 10, 1)), argv=["--dry-run"])
        self.assertEqual(rc, 0)
        self.assertEqual((self.posts, self.saved()), ([], before))
        self.assertIn("BUY at the Fri Oct 2 open", self.out)

    def test_the_real_state_file_still_loads(self):
        real = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "momentum_state.json")
        with open(real) as f:
            st = json.load(f)
        self.assertEqual(len(st["holdings"]), 10)
        self.assertRegex(st["last_rebalanced_month"], r"^\d{4}-\d{2}$")


class GetCloses(unittest.TestCase):
    def test_falls_back_to_stooq_after_yahoo_fails(self):
        good = daily(D(2026, 10, 1), months=20)
        with mock.patch.object(mom, "fetch_yahoo", side_effect=OSError("429")), \
                mock.patch.object(mom, "fetch_stooq", return_value=good), contextlib.redirect_stderr(io.StringIO()):
            self.assertIs(mom.get_closes("AAPL", sleep=lambda s: None), good)

    def test_none_when_every_source_fails(self):
        with mock.patch.object(mom, "fetch_yahoo", side_effect=OSError("a")), \
                mock.patch.object(mom, "fetch_stooq", side_effect=OSError("b")), contextlib.redirect_stderr(io.StringIO()):
            self.assertIsNone(mom.get_closes("AAPL", sleep=lambda s: None))


class KillSwitch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = os.path.join(self.tmp.name, "state.json")
        self.marker = os.path.join(self.tmp.name, ".notified")
        for target, name, value in ((ks, "STATE_FILE", self.state), (ks, "NOTIFIED_FILE", self.marker)):
            p = mock.patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.posts = []

    def history(self, n=2):
        return [{"sym": "SPY", "entry_date": "2026-09-01", "exit_signal_date": "2026-09-09"} for _ in range(n)]

    def write(self, history):
        with open(self.state, "w") as f:
            json.dump({"positions": {}, "history": history}, f)

    def run_ks(self, argv=("--webhook", "https://discord.test/h")):
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            rc = ks.main(list(argv), post=lambda w, t: self.posts.append(t))
        return rc, out.getvalue(), err.getvalue()

    def test_no_price_data_is_a_message_not_a_zero_division_crash(self):
        self.write(self.history())
        with mock.patch.object(ks, "get_bars", return_value=None):
            rc, out, err = self.run_ks()
        self.assertEqual(rc, 1)
        self.assertEqual(len(self.posts), 1)
        self.assertIn("could not run", self.posts[0])
        self.assertTrue(os.path.exists(self.marker))

    def test_no_closed_trades_yet_is_fine(self):
        self.write([])
        rc, out, _ = self.run_ks()
        self.assertEqual(rc, 0)
        self.assertIn("No closed trades yet", out)

    def test_a_healthy_book_stays_quiet_and_status_posts_a_summary(self):
        self.write(self.history(3))
        idx = pd.bdate_range("2026-08-20", "2026-09-30")
        bars = pd.DataFrame({"open": [100 + i * 0.1 for i in range(len(idx))], "close": 100.0}, index=idx)
        with mock.patch.object(ks, "get_bars", return_value=bars):
            rc, out, _ = self.run_ks(["--webhook", "https://discord.test/h"])
            self.assertEqual((rc, self.posts), (0, []))
            rc, out, _ = self.run_ks(["--webhook", "https://discord.test/h", "--status"])
        self.assertEqual(rc, 0)
        self.assertIn("not enough trades yet (3/100", self.posts[0])

    def test_discord_refusing_the_report_fails_the_run(self):
        self.write(self.history(3))
        idx = pd.bdate_range("2026-08-20", "2026-09-30")
        bars = pd.DataFrame({"open": 100.0, "close": 100.0}, index=idx)

        def boom(webhook, text):
            raise ibs_bot.PostFailed("HTTP 500")

        with mock.patch.object(ks, "get_bars", return_value=bars), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            rc = ks.main(["--webhook", "https://discord.test/h", "--status"], post=boom)
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
