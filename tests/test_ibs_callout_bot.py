"""IBS callout bot: which session a run evaluates, hold counting, idempotency, delivery failures, both messages.

Everything runs offline: price data comes from a fake `get`, Discord from a fake `post`, state from a temp file."""
import contextlib
import copy
import datetime as dt
import io
import json
import os
import tempfile
import unittest
import urllib.error
from unittest import mock

import numpy as np
import pandas as pd

import ibs_callout_bot as bot

D = dt.date
ET = bot.ET


def at(y, m, d, hh, mm=0):
    return dt.datetime(y, m, d, hh, mm, tzinfo=ET)


def bars(end, n=40, last=None, closes=None):
    """Flat 99-101 bars (IBS 0.5, prev high 101) ending on `end`; `last` overrides the final bar's OHLC."""
    idx = pd.bdate_range(end=pd.Timestamp(end), periods=n)
    df = pd.DataFrame({"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0}, index=idx)
    if last:
        for k, v in last.items():
            df.iloc[-1, df.columns.get_loc(k)] = v
    return df


DIP = {"low": 99.0, "high": 101.0, "close": 99.1}            # IBS 0.05
POP = {"low": 99.5, "high": 102.0, "close": 101.5}           # closes above yesterday's 101 high


def universe(end, **by_ticker):
    return {t: bars(end, last=by_ticker.get(t)) for t in bot.TICKERS}


class Env(unittest.TestCase):
    """Temp state file + captured stdout/stderr + fake network."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "state.json")
        self.marker = os.path.join(self.tmp.name, ".notified")
        for name, value in (("STATE_FILE", self.path), ("NOTIFIED_FILE", self.marker)):
            p = mock.patch.object(bot, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.posts, self.sleeps = [], []

    def post(self, webhook, text):
        self.posts.append(text)

    def sleep(self, s):
        self.sleeps.append(s)

    def get_from(self, data):
        return lambda t, expected=None: data[t]

    def evening(self, now, data, state=None, webhook="https://discord.test/hook", **kw):
        state = state if state is not None else bot.load_state()
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            rc = bot.run_evening(state, now, webhook, get=self.get_from(data), sleep=self.sleep, post=kw.pop("post", self.post), **kw)
        self.out, self.err = out.getvalue(), err.getvalue()
        return rc

    def morning(self, now, state=None, webhook="https://discord.test/hook", **kw):
        state = state if state is not None else bot.load_state()
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            rc = bot.run_morning(state, now, webhook, post=kw.pop("post", self.post), **kw)
        self.out, self.err = out.getvalue(), err.getvalue()
        return rc

    def saved(self):
        with open(self.path) as f:
            return json.load(f)

    def write(self, state):
        with open(self.path, "w") as f:
            json.dump(state, f)


class Calendar(unittest.TestCase):
    def test_evening_run_looks_at_the_session_that_just_closed(self):
        self.assertEqual(bot.expected_last_session(at(2026, 10, 14, 17, 15)), D(2026, 10, 14))

    def test_before_the_bar_is_final_it_is_still_the_previous_session(self):
        self.assertEqual(bot.expected_last_session(at(2026, 10, 14, 16, 4)), D(2026, 10, 13))
        self.assertEqual(bot.expected_last_session(at(2026, 10, 14, 16, 5)), D(2026, 10, 14))
        self.assertEqual(bot.expected_last_session(at(2026, 10, 14, 9, 7)), D(2026, 10, 13))

    def test_a_run_that_drifts_past_midnight_still_means_that_evening(self):
        # GitHub's cron can land hours late: 00:30 ET Thursday is still Wednesday's close
        self.assertEqual(bot.expected_last_session(at(2026, 10, 15, 0, 30)), D(2026, 10, 14))

    def test_weekends_and_holidays_look_back_to_the_last_real_session(self):
        self.assertEqual(bot.expected_last_session(at(2026, 10, 17, 10)), D(2026, 10, 16))     # Saturday
        self.assertEqual(bot.expected_last_session(at(2026, 11, 26, 17)), D(2026, 11, 25))     # Thanksgiving
        self.assertEqual(bot.expected_last_session(at(2026, 11, 27, 17)), D(2026, 11, 27))     # day after: early close, still a session

    def test_next_session_skips_weekends_and_holidays(self):
        self.assertEqual(bot.next_session(D(2026, 10, 16)), D(2026, 10, 19))
        self.assertEqual(bot.next_session(D(2026, 11, 25)), D(2026, 11, 27))
        self.assertEqual(bot.next_session(D(2026, 7, 2)), D(2026, 7, 6))                     # Jul 3 observed holiday

    def test_sessions_between(self):
        self.assertEqual(bot.sessions_between(D(2026, 10, 9), D(2026, 10, 14)), [D(2026, 10, 12), D(2026, 10, 13)])
        self.assertEqual(bot.sessions_between(D(2026, 10, 13), D(2026, 10, 14)), [])

    def test_fmt_day(self):
        self.assertEqual(bot.fmt_day(D(2026, 10, 14)), "Wed Oct 14")
        self.assertEqual(bot.fmt_day(D(2026, 11, 3)), "Tue Nov 3")


class Evaluate(unittest.TestCase):
    EXP = D(2026, 10, 14)

    def state(self, **pos):
        return {"positions": pos, "history": []}

    def test_a_low_ibs_close_is_a_buy(self):
        ev = bot.evaluate(self.state(), universe(self.EXP, SPY=DIP), self.EXP)
        self.assertEqual([b["sym"] for b in ev["buys"]], ["SPY"])
        self.assertAlmostEqual(ev["buys"][0]["ibs"], 0.05, places=2)
        self.assertEqual(ev["sells"], [])

    def test_ibs_exactly_at_the_threshold_is_not_a_buy(self):
        ev = bot.evaluate(self.state(), universe(self.EXP, SPY={"low": 99.0, "high": 101.0, "close": 99.2}), self.EXP)
        self.assertEqual(ev["buys"], [])

    def test_a_held_etf_never_gets_a_second_buy(self):
        st = self.state(SPY={"entry_date": "2026-10-12", "bars": 1, "pending": False})
        ev = bot.evaluate(st, universe(self.EXP, SPY=DIP), self.EXP)
        self.assertEqual(ev["buys"], [])

    def test_close_above_yesterdays_high_is_a_sell(self):
        st = self.state(IWM={"entry_date": "2026-10-12", "bars": 1, "pending": False})
        ev = bot.evaluate(st, universe(self.EXP, IWM=POP), self.EXP)
        self.assertEqual([(s["sym"], s["why"], s["held"]) for s in ev["sells"]],
                         [("IWM", "close above yesterday's high", 2)])

    def test_ten_days_held_is_a_sell_even_without_a_pop(self):
        st = self.state(DIA={"entry_date": "2026-09-30", "bars": 3, "pending": False})        # 10/1..10/14 = 10 bars
        ev = bot.evaluate(st, universe(self.EXP), self.EXP)
        self.assertEqual([(s["sym"], s["why"]) for s in ev["sells"]], [("DIA", "10 trading days held")])

    def test_nine_days_is_not_yet(self):
        st = self.state(DIA={"entry_date": "2026-10-01", "bars": 3, "pending": False})        # 10/2..10/14 = 9 bars
        ev = bot.evaluate(st, universe(self.EXP), self.EXP)
        self.assertEqual(ev["sells"], [])

    def test_hold_count_comes_from_the_price_history_not_from_how_often_the_bot_ran(self):
        """Real case: SPY signalled 9/30, no evening run on Fri 10/2, so the old per-run counter said 2 on 10/5 (really 3)."""
        st = self.state(SPY={"entry_date": "2026-09-30", "bars": 1, "pending": False})
        df = bars(D(2026, 10, 5))
        self.assertEqual(bot.held_bars(df, "2026-09-30"), 3)
        self.assertEqual(bot.held_bars(bars(D(2026, 10, 6)), "2026-09-30"), 4)
        ev = bot.evaluate(st, universe(D(2026, 10, 5)), D(2026, 10, 5))
        self.assertEqual(ev["rows"][0]["held"], 3)

    def test_evaluate_does_not_touch_state(self):
        st = self.state(IWM={"entry_date": "2026-10-12", "bars": 1, "pending": False})
        before = copy.deepcopy(st)
        bot.evaluate(st, universe(self.EXP, IWM=POP), self.EXP)
        self.assertEqual(st, before)


class Apply(unittest.TestCase):
    EXP = D(2026, 10, 14)

    def test_buy_is_recorded_pending_with_the_signal_bar_date(self):
        st = {"positions": {}, "history": []}
        bot.apply_changes(st, bot.evaluate(st, universe(self.EXP, QQQ=DIP), self.EXP))
        self.assertEqual(st["positions"]["QQQ"]["entry_date"], "2026-10-14")
        self.assertTrue(st["positions"]["QQQ"]["pending"])
        self.assertEqual(st["last_bar"], "2026-10-14")

    def test_sell_moves_the_position_to_history_and_to_pending_exits(self):
        st = {"positions": {"IWM": {"entry_date": "2026-10-12", "signal_ibs": 0.08, "bars": 1, "pending": False}}, "history": []}
        bot.apply_changes(st, bot.evaluate(st, universe(self.EXP, IWM=POP), self.EXP))
        self.assertNotIn("IWM", st["positions"])
        self.assertEqual(st["history"][-1]["exit_signal_date"], "2026-10-14")          # the bar's date, not the runner's UTC date
        self.assertEqual(st["history"][-1]["reason"], "close above yesterday's high")
        self.assertEqual(st["pending_exits"]["IWM"]["signal_bar"], "2026-10-14")

    def test_continuing_positions_get_their_real_count(self):
        st = {"positions": {"SPY": {"entry_date": "2026-10-09", "bars": 0, "pending": False}}, "history": []}
        bot.apply_changes(st, bot.evaluate(st, universe(self.EXP), self.EXP))
        self.assertEqual((st["positions"]["SPY"]["bars"], st["positions"]["SPY"]["last_bar"]), (3, "2026-10-14"))


class EveningRun(Env):
    NOW = at(2026, 10, 14, 17, 15)
    EXP = D(2026, 10, 14)

    def test_posts_one_message_and_saves_the_orders(self):
        rc = self.evening(self.NOW, universe(self.EXP, SPY=DIP))
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.posts), 1)
        text = self.posts[0]
        self.assertIn("signals from the Wed Oct 14 close", text)
        self.assertIn("BUY at the Thu Oct 15 open", text)
        self.assertIn("**SPY** — IBS 0.05, close 99.10", text)
        self.assertIn("SPY: close 99.10  IBS 0.05  [BUY signal]", text)
        self.assertIn("9:00 AM ET (7:00 AM MT)", text)
        st = self.saved()
        self.assertTrue(st["positions"]["SPY"]["pending"])
        self.assertEqual(st["last_bar"], "2026-10-14")
        self.assertNotIn("unsent", st)

    def test_no_signals_says_so_plainly(self):
        self.evening(self.NOW, universe(self.EXP))
        self.assertIn("No action at the Thu Oct 15 open.", self.posts[0])
        self.assertNotIn("reminder with these orders", self.posts[0])

    def test_friday_signals_are_for_monday(self):
        self.evening(at(2026, 10, 16, 17, 15), universe(D(2026, 10, 16), SPY=DIP))
        self.assertIn("signals from the Fri Oct 16 close", self.posts[0])
        self.assertIn("BUY at the Mon Oct 19 open", self.posts[0])

    def test_a_run_after_utc_midnight_still_shows_the_new_york_date(self):
        # 20:30 ET Wednesday is 00:30 UTC Thursday: the old header printed Thursday
        self.evening(at(2026, 10, 14, 20, 30), universe(self.EXP))
        self.assertIn("Wed Oct 14 close", self.posts[0])
        self.assertNotIn("Oct 15 close", self.posts[0])

    def test_a_run_before_the_close_ignores_todays_forming_bar(self):
        data = {t: pd.concat([bars(D(2026, 10, 13)), bars(self.EXP).iloc[[-1]]]) for t in bot.TICKERS}
        data["SPY"].iloc[-1, data["SPY"].columns.get_loc("close")] = 99.1                     # today's partial bar looks like a dip
        self.evening(at(2026, 10, 14, 11, 0), data)
        self.assertIn("signals from the Tue Oct 13 close", self.posts[0])
        self.assertNotIn("[BUY signal]", self.posts[0])

    def test_late_message_gets_a_banner_when_the_open_has_passed(self):
        self.evening(at(2026, 10, 15, 10, 0), universe(self.EXP, SPY=DIP))
        self.assertIn("Late message", self.posts[0])
        self.assertIn("Thu Oct 15 open", self.posts[0])

    def test_on_time_message_has_no_late_banner(self):
        self.evening(self.NOW, universe(self.EXP))
        self.assertNotIn("Late message", self.posts[0])

    # ---- stale price data ---------------------------------------------------------------------------------
    def test_stale_data_never_becomes_signals_and_leaves_state_alone(self):
        data = universe(self.EXP, SPY=DIP)
        data["DIA"] = bars(D(2026, 10, 13))                       # Yahoo has not published Wednesday's bar for DIA
        rc = self.evening(self.NOW, data)
        self.assertEqual(rc, 1)
        self.assertEqual(self.sleeps, [bot.FRESH_WAIT_S] * (bot.FRESH_TRIES - 1))
        self.assertEqual(len(self.posts), 1)
        self.assertIn("NO SIGNALS for the Wed Oct 14 close", self.posts[0])
        self.assertIn("DIA (newest bar 2026-10-13)", self.posts[0])
        self.assertIn("Run workflow", self.posts[0])
        self.assertFalse(os.path.exists(self.path), "nothing may be saved when nothing was evaluated")
        self.assertTrue(os.path.exists(self.marker), "tells notify_failure.py not to post a second message")

    def test_data_that_catches_up_during_the_wait_is_used(self):
        fresh, stale = universe(self.EXP, SPY=DIP), universe(D(2026, 10, 13))
        calls = {"n": 0}

        def get(t, expected=None):
            calls["n"] += 1
            return stale[t] if calls["n"] <= len(bot.TICKERS) * 2 else fresh[t]          # stale for the first two looks

        state = bot.load_state()
        with contextlib.redirect_stdout(io.StringIO()):
            rc = bot.run_evening(state, self.NOW, "https://discord.test/hook", get=get, sleep=self.sleep, post=self.post)
        self.assertEqual(rc, 0)
        self.assertEqual(self.sleeps, [bot.FRESH_WAIT_S, bot.FRESH_WAIT_S])
        self.assertIn("[BUY signal]", self.posts[0])

    def test_a_fetch_error_for_one_ticker_is_handled_like_stale_data(self):
        def get(t, expected=None):
            if t == "QQQ":
                raise RuntimeError("no data for QQQ: HTTP 429")
            return bars(self.EXP)

        state = bot.load_state()
        with contextlib.redirect_stdout(io.StringIO()):
            rc = bot.run_evening(state, self.NOW, "https://discord.test/hook", get=get, sleep=self.sleep, post=self.post)
        self.assertEqual(rc, 1)
        self.assertIn("QQQ (fetch failed", self.posts[0])

    def test_a_holiday_evening_does_nothing_when_the_close_was_already_processed(self):
        self.evening(at(2026, 11, 25, 17, 15), universe(D(2026, 11, 25)))
        self.posts.clear()
        rc = self.evening(at(2026, 11, 26, 17, 15), universe(D(2026, 11, 25)))             # Thanksgiving evening
        self.assertEqual(rc, 0)
        self.assertEqual(self.posts, [])
        self.assertIn("already processed", self.out)

    # ---- idempotency ----------------------------------------------------------------------------------------
    def test_running_twice_for_the_same_close_changes_nothing_and_posts_once(self):
        data = universe(self.EXP, SPY=DIP)
        self.evening(self.NOW, data)
        first = self.saved()
        self.evening(at(2026, 10, 14, 22, 0), data)
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.saved(), first)

    def test_hold_counts_do_not_move_on_a_second_run(self):
        self.write({"positions": {"IWM": {"entry_date": "2026-10-12", "signal_ibs": 0.08, "bars": 1, "pending": False}}, "history": []})
        data = universe(self.EXP)
        self.evening(self.NOW, data)
        self.evening(self.NOW, data)
        self.evening(self.NOW, data)
        self.assertEqual(self.saved()["positions"]["IWM"]["bars"], 2)

    def test_force_re_sends_the_saved_message_without_re_evaluating(self):
        data = universe(self.EXP, SPY=DIP)
        self.evening(self.NOW, data)
        self.evening(self.NOW, data, force=True)
        self.assertEqual(self.posts[0], self.posts[1])
        self.assertEqual(self.saved()["positions"]["SPY"]["bars"], 0)

    def test_dry_run_prints_posts_nothing_and_saves_nothing(self):
        rc = self.evening(self.NOW, universe(self.EXP, SPY=DIP), dry_run=True)
        self.assertEqual(rc, 0)
        self.assertEqual(self.posts, [])
        self.assertFalse(os.path.exists(self.path))
        self.assertIn("[BUY signal]", self.out)

    # ---- delivery failures ----------------------------------------------------------------------------------
    def test_a_failed_post_fails_the_run_but_keeps_the_orders(self):
        def boom(webhook, text):
            raise bot.PostFailed("HTTP 500")

        rc = self.evening(self.NOW, universe(self.EXP, SPY=DIP), post=boom)
        self.assertEqual(rc, 1)
        st = self.saved()
        self.assertTrue(st["positions"]["SPY"]["pending"])
        self.assertEqual(st["unsent"], {"kind": "evening", "bar": "2026-10-14"})
        self.assertIn("NOT POSTED", self.err)

    def test_a_rerun_after_a_failed_post_sends_the_same_message_and_clears_the_flag(self):
        def boom(webhook, text):
            raise bot.PostFailed("HTTP 500")

        data = universe(self.EXP, SPY=DIP)
        self.evening(self.NOW, data, post=boom)
        saved_text = self.saved()["last_evening"]["text"]
        rc = self.evening(at(2026, 10, 14, 23, 0), data)
        self.assertEqual(rc, 0)
        self.assertEqual(self.posts, [saved_text])
        self.assertNotIn("unsent", self.saved())

    def test_no_webhook_is_a_failure_not_a_silent_success(self):
        rc = self.evening(self.NOW, universe(self.EXP, SPY=DIP), webhook="")
        self.assertEqual(rc, 1)
        self.assertEqual(self.posts, [])
        self.assertIn("DISCORD_WEBHOOK is not set", self.err)
        self.assertEqual(self.saved()["unsent"]["kind"], "evening")

    # ---- notes -----------------------------------------------------------------------------------------------
    def test_a_skipped_evening_is_mentioned(self):
        self.write({"positions": {}, "history": [], "last_bar": "2026-10-09"})
        self.evening(self.NOW, universe(self.EXP))
        self.assertIn("No evening run was recorded for Mon Oct 12, Tue Oct 13", self.posts[0])

    def test_a_consecutive_evening_has_no_missed_run_note(self):
        self.write({"positions": {}, "history": [], "last_bar": "2026-10-13"})
        self.evening(self.NOW, universe(self.EXP))
        self.assertNotIn("No evening run", self.posts[0])

    def test_a_buy_whose_reminder_never_ran_is_flagged_and_counted_as_held(self):
        self.write({"positions": {"SPY": {"entry_date": "2026-10-13", "signal_ibs": 0.05, "bars": 0, "pending": True}}, "history": []})
        self.evening(self.NOW, universe(self.EXP))
        self.assertIn("reminder never ran for the SPY BUY signal", self.posts[0])
        self.assertFalse(self.saved()["positions"]["SPY"]["pending"])
        self.assertEqual(self.saved()["positions"]["SPY"]["bars"], 1)

    def test_sell_message_names_the_signal_and_the_open(self):
        self.write({"positions": {"IWM": {"entry_date": "2026-10-12", "signal_ibs": 0.08, "bars": 1, "pending": False}}, "history": []})
        self.evening(self.NOW, universe(self.EXP, IWM=POP))
        text = self.posts[0]
        self.assertIn("SELL at the Thu Oct 15 open", text)
        self.assertIn("**IWM** — close above yesterday's high (bought at the Tue Oct 13 open, held 2 days)", text)
        self.assertIn("IWM: close 101.50  IBS 0.80  [SELL signal]", text)

    def test_held_positions_show_progress_in_the_table(self):
        self.write({"positions": {"DIA": {"entry_date": "2026-10-12", "signal_ibs": 0.08, "bars": 1, "pending": False}}, "history": []})
        self.evening(self.NOW, universe(self.EXP))
        self.assertIn("DIA: close 100.00  IBS 0.50  [in position, held 2/10 days]", self.posts[0])


class MorningRun(Env):
    NOW = at(2026, 10, 15, 9, 0)

    def seed(self, **extra):
        st = {"positions": {"SPY": {"entry_date": "2026-10-14", "signal_ibs": 0.05, "bars": 0, "pending": True},
                            "DIA": {"entry_date": "2026-10-09", "signal_ibs": 0.07, "bars": 4, "pending": False}},
              "history": [], "pending_exits": {"IWM": {"why": "close above yesterday's high", "signal_bar": "2026-10-14",
                                                       "entry_date": "2026-10-12", "held": 2}}}
        st.update(extra)
        self.write(st)

    def test_reminder_lists_the_sell_the_buy_and_what_is_held(self):
        self.seed()
        self.assertEqual(self.morning(self.NOW), 0)
        text = self.posts[0]
        self.assertIn("Pre-open reminder — Thu Oct 15", text)
        self.assertIn("before the 9:30 AM ET open (7:30 AM MT)", text)
        self.assertIn("**SELL at the open** (market-on-open, the whole position): **IWM** — close above yesterday's high "
                      "(signal: Wed Oct 14 close)", text)
        self.assertIn("**BUY at the open** (market-on-open, 25% of the bot's capital per ETF): **SPY**", text)
        self.assertIn("Holding, no action: DIA (held 4 of 10 days so far)", text)

    def test_after_a_posted_reminder_nothing_is_pending(self):
        self.seed()
        self.morning(self.NOW)
        st = self.saved()
        self.assertFalse(st["positions"]["SPY"]["pending"])
        self.assertEqual(st["pending_exits"], {})

    def test_nothing_to_do_says_so(self):
        self.write({"positions": {}, "history": []})
        self.morning(self.NOW)
        self.assertIn("No orders today.", self.posts[0])

    def test_a_failed_reminder_keeps_the_orders_pending(self):
        self.seed()

        def boom(webhook, text):
            raise bot.PostFailed("HTTP 500")

        self.assertEqual(self.morning(self.NOW, post=boom), 1)
        st = self.saved()
        self.assertTrue(st["positions"]["SPY"]["pending"])
        self.assertIn("IWM", st["pending_exits"])
        self.assertEqual(st["unsent"]["kind"], "morning")

    def test_it_repeats_the_orders_when_last_nights_message_never_posted(self):
        self.seed(unsent={"kind": "evening", "bar": "2026-10-14"})
        self.morning(self.NOW)
        self.assertIn("Last night's signal message never posted", self.posts[0])
        self.assertIn("**IWM**", self.posts[0])
        self.assertNotIn("unsent", self.saved())

    def test_a_market_holiday_sends_nothing_and_keeps_the_orders_for_the_next_session(self):
        self.seed()
        before = self.saved()
        rc = self.morning(at(2026, 11, 26, 9, 0))
        self.assertEqual(rc, 0)
        self.assertEqual(self.posts, [])
        self.assertEqual(self.saved(), before)

    def test_weekend_sends_nothing(self):
        self.seed()
        self.morning(at(2026, 10, 17, 9, 0))
        self.assertEqual(self.posts, [])

    def test_dry_run_changes_nothing(self):
        self.seed()
        before = self.saved()
        self.morning(self.NOW, dry_run=True)
        self.assertEqual(self.posts, [])
        self.assertEqual(self.saved(), before)

    def test_the_legacy_state_format_still_works(self):
        self.write({"positions": {"IWM": {"entry_date": "2026-10-12", "signal_ibs": 0.0815, "bars": 2, "pending": False,
                                          "last_bar": "2026-10-14"}}, "history": []})
        self.morning(self.NOW)
        self.assertIn("Holding, no action: IWM (held 2 of 10 days so far)", self.posts[0])


class PostDiscord(unittest.TestCase):
    class Resp:
        def __init__(self, status):
            self.status = status

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def http_error(self, code, body=b"{}"):
        return urllib.error.HTTPError("https://discord.test/hook", code, "x", {}, io.BytesIO(body))

    def opener(self, *results):
        seq, calls = list(results), []

        def op(req, timeout=None):
            calls.append(json.loads(req.data))
            r = seq.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

        op.calls = calls
        return op

    def test_success(self):
        op = self.opener(self.Resp(204))
        self.assertEqual(bot.post_discord("https://discord.test/hook", "hi", opener=op, sleep=lambda s: None), 204)
        self.assertEqual(op.calls[0], {"content": "hi", "allowed_mentions": {"parse": []}})

    def test_content_is_capped_below_discords_limit(self):
        op = self.opener(self.Resp(204))
        bot.post_discord("https://discord.test/hook", "x" * 5000, opener=op, sleep=lambda s: None)
        self.assertEqual(len(op.calls[0]["content"]), 1900)

    def test_rate_limit_waits_the_time_discord_asked_for_then_succeeds(self):
        sleeps = []
        op = self.opener(self.http_error(429, b'{"retry_after": 1.5}'), self.Resp(204))
        bot.post_discord("https://discord.test/hook", "hi", opener=op, sleep=sleeps.append)
        self.assertEqual(sleeps, [1.5])
        self.assertEqual(len(op.calls), 2)

    def test_server_errors_are_retried_then_reported(self):
        op = self.opener(*[self.http_error(500)] * 4)
        with self.assertRaises(bot.PostFailed) as cm:
            bot.post_discord("https://discord.test/hook", "hi", opener=op, sleep=lambda s: None)
        self.assertIn("HTTP 500", str(cm.exception))
        self.assertEqual(len(op.calls), 4)

    def test_a_dead_webhook_is_not_retried(self):
        op = self.opener(self.http_error(404))
        with self.assertRaises(bot.PostFailed):
            bot.post_discord("https://discord.test/hook", "hi", opener=op, sleep=lambda s: None)
        self.assertEqual(len(op.calls), 1)

    def test_network_errors_are_retried(self):
        op = self.opener(urllib.error.URLError("dns"), self.Resp(204))
        self.assertEqual(bot.post_discord("https://discord.test/hook", "hi", opener=op, sleep=lambda s: None), 204)


class GetBars(unittest.TestCase):
    def test_falls_back_to_stooq_when_yahoo_is_a_day_behind(self):
        exp = D(2026, 10, 14)
        with mock.patch.object(bot, "fetch_yahoo", return_value=bars(D(2026, 10, 13))), \
                mock.patch.object(bot, "fetch_stooq", return_value=bars(exp)):
            self.assertEqual(bot.last_bar_date(bot.get_bars("SPY", exp)), exp)

    def test_returns_the_freshest_when_nobody_has_the_session(self):
        exp = D(2026, 10, 14)
        with mock.patch.object(bot, "fetch_yahoo", return_value=bars(D(2026, 10, 12))), \
                mock.patch.object(bot, "fetch_stooq", return_value=bars(D(2026, 10, 13))):
            self.assertEqual(bot.last_bar_date(bot.get_bars("SPY", exp)), D(2026, 10, 13))

    def test_both_sources_down_raises(self):
        with mock.patch.object(bot, "fetch_yahoo", side_effect=OSError("down")), \
                mock.patch.object(bot, "fetch_stooq", side_effect=OSError("also down")):
            with self.assertRaisesRegex(RuntimeError, "no data for SPY"):
                bot.get_bars("SPY", D(2026, 10, 14))

    def test_without_an_expected_date_the_first_good_source_wins(self):
        with mock.patch.object(bot, "fetch_yahoo", return_value=bars(D(2026, 10, 13))):
            self.assertEqual(bot.last_bar_date(bot.get_bars("SPY")), D(2026, 10, 13))

    def test_trim_to_drops_later_bars(self):
        df = bars(D(2026, 10, 14))
        self.assertEqual(bot.last_bar_date(bot.trim_to(df, D(2026, 10, 12))), D(2026, 10, 12))


class NotifyFailure(unittest.TestCase):
    def setUp(self):
        import notify_failure as nf
        self.nf = nf
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(nf, "MARKER", os.path.join(self.tmp.name, ".notified"))
        p.start()
        self.addCleanup(p.stop)

    def test_message_says_nothing_arrived_and_links_the_run(self):
        t = self.nf.message("IBS callout bot", run_url="https://github.com/o/r/actions/runs/1", event="schedule")
        self.assertIn("IBS callout bot: this run FAILED** (started by schedule)", t)
        self.assertIn("did NOT arrive", t)
        self.assertIn("https://github.com/o/r/actions/runs/1", t)

    def test_posts_once_with_the_webhook(self):
        with mock.patch.object(self.nf, "post", return_value=True) as post, contextlib.redirect_stdout(io.StringIO()):
            rc = self.nf.main(["IBS callout bot"], env={"DISCORD_WEBHOOK": "https://discord.test/h", "RUN_URL": "u"})
        self.assertEqual(rc, 0)
        post.assert_called_once()
        self.assertEqual(post.call_args.args[0], "https://discord.test/h")

    def test_stays_quiet_when_the_bot_already_explained_itself(self):
        open(self.nf.MARKER, "w").close()
        with mock.patch.object(self.nf, "post") as post, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.nf.main(["x"], env={"DISCORD_WEBHOOK": "https://discord.test/h"}), 0)
        post.assert_not_called()

    def test_never_fails_the_step_even_without_a_webhook_or_when_discord_is_down(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.nf.main(["x"], env={}), 0)
        with mock.patch.object(self.nf, "post", return_value=False):
            self.assertEqual(self.nf.main(["x"], env={"DISCORD_WEBHOOK": "https://discord.test/h"}), 0)

    def test_post_retries_then_gives_up_quietly(self):
        calls = []

        def op(req, timeout=None):
            calls.append(1)
            raise OSError("down")

        with contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(self.nf.post("https://discord.test/h", "x", opener=op, sleep=lambda s: None))
        self.assertEqual(len(calls), 3)


try:
    import yaml  # noqa: F401
except ImportError:                                  # PyYAML is only needed for the workflow checks (CI installs it)
    yaml = None


@unittest.skipUnless(yaml, "PyYAML not installed")
class Workflows(unittest.TestCase):
    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def load(self, name):
        with open(os.path.join(self.ROOT, ".github", "workflows", name)) as f:
            return yaml.safe_load(f)

    def text(self, name):
        with open(os.path.join(self.ROOT, ".github", "workflows", name)) as f:
            return f.read()

    def test_every_workflow_parses(self):
        for name in sorted(os.listdir(os.path.join(self.ROOT, ".github", "workflows"))):
            self.assertIn("jobs", self.load(name), name)

    def test_state_is_committed_even_when_the_run_failed_and_a_failed_push_is_loud(self):
        steps = self.load("ibs-bot.yml")["jobs"]["run"]["steps"]
        commit = next(s for s in steps if s.get("name") == "Commit state")
        self.assertEqual(commit["if"], "always()")
        self.assertIn("could not push state after 5 attempts", commit["run"])
        self.assertNotIn("|| true", commit["run"].replace("git add state.json || true", "").replace(
            "git add .run_guard.json 2>/dev/null || true", ""))

    def test_a_failed_run_tells_discord(self):
        for name, job in (("ibs-bot.yml", "run"), ("momentum-bot.yml", "run"), ("kill-switch.yml", "run")):
            steps = self.load(name)["jobs"][job]["steps"]
            last = steps[-1]
            self.assertEqual(last.get("if"), "failure()", name)
            self.assertIn("notify_failure.py", last["run"], name)

    def test_the_preopen_notice_points_at_the_real_orders(self):
        text = self.text("ibs-bot.yml")
        self.assertIn("--too-late-hint", text)
        self.assertIn("pending_exits", text)


class Main(Env):
    def test_cli_dry_run_end_to_end(self):
        data = universe(D(2026, 10, 14), SPY=DIP)
        with mock.patch.object(bot, "get_bars", side_effect=lambda t, expected=None: data[t]), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            rc = bot.main(["--dry-run", "--now", "2026-10-14T17:15:00"])
        self.assertEqual(rc, 0)
        self.assertIn("signals from the Wed Oct 14 close", out.getvalue())

    def test_cli_morning_dry_run(self):
        self.write({"positions": {}, "history": []})
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc = bot.main(["--morning", "--dry-run", "--now", "2026-10-15T09:00:00"])
        self.assertEqual(rc, 0)
        self.assertIn("No orders today.", out.getvalue())


if __name__ == "__main__":
    unittest.main()
