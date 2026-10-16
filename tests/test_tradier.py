"""Tradier client and its wiring into the bots' price fetchers. Everything runs offline with a fake opener."""
import datetime as dt
import io
import json
import unittest
import urllib.error
from unittest import mock

import pandas as pd

import ibs_callout_bot as bot
import kill_switch_monitor as ks
import momentum_sleeve_bot as mom
import tradier

ENV = {"TRADIER_API_KEY": "SECRET-TOKEN-123", "TRADIER_MIN_GAP_S": "0"}
SANDBOX = dict(ENV, TRADIER_BASE_URL="https://sandbox.tradier.com/v1")
NOSLEEP = lambda s: None  # noqa: E731


class Resp:
    def __init__(self, payload):
        self.body = json.dumps(payload).encode()

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def http_error(code, headers=None):
    return urllib.error.HTTPError("https://x", code, "err", headers or {}, io.BytesIO(b"{}"))


class Opener:
    """Returns / raises the queued items in order and records the requests it saw."""

    def __init__(self, *items):
        self.items, self.seen = list(items), []

    def __call__(self, req, timeout=None):
        self.seen.append(req)
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return Resp(item)


class Config(unittest.TestCase):
    def test_configured_needs_a_token(self):
        self.assertFalse(tradier.configured({}))
        self.assertFalse(tradier.configured({"TRADIER_API_KEY": "  "}))
        self.assertTrue(tradier.configured(ENV))
        self.assertTrue(tradier.configured({"TRADIER_TOKEN": "old-name-still-works"}))

    def test_mode_defaults_to_production_and_rejects_typos(self):
        self.assertEqual(tradier.mode({}), "production")
        self.assertEqual(tradier.mode({"TRADIER_ENV": ""}), "production")
        self.assertEqual(tradier.mode({"TRADIER_ENV": "Sandbox"}), "sandbox")
        with self.assertRaises(tradier.TradierError):
            tradier.mode({"TRADIER_ENV": "paper"})

    def test_base_url_picks_the_host_and_only_tradier_hosts_are_allowed(self):
        self.assertEqual(tradier.mode({"TRADIER_BASE_URL": "https://sandbox.tradier.com/v1"}), "sandbox")
        self.assertEqual(tradier.mode({"TRADIER_BASE_URL": "https://api.tradier.com/v1"}), "production")
        self.assertEqual(tradier.mode({"TRADIER_BASE_URL": ""}), "production")        # unset GitHub secret = empty string
        for bad in ("https://evil.example.com/v1", "http://api.tradier.com/v1", "https://api.tradier.com.evil.io/v1"):
            with self.assertRaises(tradier.TradierError):
                tradier.mode({"TRADIER_BASE_URL": bad})

    def test_a_bad_base_url_never_sends_the_token_anywhere(self):
        op = Opener()
        with self.assertRaises(tradier.TradierError):
            tradier.quotes(["SPY"], env=dict(ENV, TRADIER_BASE_URL="https://evil.example.com/v1"), opener=op,
                           sleep=NOSLEEP)
        self.assertEqual(op.seen, [])

    def test_sandbox_is_labelled_delayed(self):
        self.assertTrue(tradier.is_delayed(SANDBOX))
        self.assertIn("delayed", tradier.data_mode(SANDBOX))
        self.assertFalse(tradier.is_delayed(ENV))


class Transport(unittest.TestCase):
    def test_uses_the_host_that_matches_the_env_and_sends_the_bearer_token(self):
        op = Opener({"quotes": {"quote": {"symbol": "SPY", "last": 1}}})
        tradier.quotes(["SPY"], env=ENV, opener=op, sleep=NOSLEEP)
        self.assertTrue(op.seen[0].full_url.startswith("https://api.tradier.com/v1/markets/quotes?"))
        self.assertEqual(op.seen[0].get_header("Authorization"), "Bearer SECRET-TOKEN-123")
        op = Opener({"quotes": {"quote": {"symbol": "SPY", "last": 1}}})
        tradier.quotes(["SPY"], env=SANDBOX, opener=op, sleep=NOSLEEP)
        self.assertTrue(op.seen[0].full_url.startswith("https://sandbox.tradier.com/v1/"))

    def test_only_market_data_paths_are_allowed(self):
        for path in ("/accounts/123/orders", "/user/profile", "/markets"):
            with self.assertRaises(tradier.TradierError):
                tradier._get(path, env=ENV, opener=Opener(), sleep=NOSLEEP)

    def test_no_token_means_no_request(self):
        op = Opener()
        with self.assertRaises(tradier.TradierError):
            tradier._get("/markets/quotes", {"symbols": "SPY"}, env={}, opener=op, sleep=NOSLEEP)
        self.assertEqual(op.seen, [])

    def test_bad_key_fails_fast_without_leaking_the_token(self):
        op = Opener(http_error(401), {"never": "used"})
        with self.assertRaises(tradier.TradierError) as cm:
            tradier.quotes(["SPY"], env=ENV, opener=op, sleep=NOSLEEP)
        self.assertEqual(len(op.seen), 1)                               # 401 is not retried
        self.assertNotIn("SECRET-TOKEN-123", str(cm.exception))
        self.assertIn("TRADIER_BASE_URL", str(cm.exception))            # tells you sandbox vs production may be the issue

    def test_rate_limit_and_server_errors_are_retried(self):
        op = Opener(http_error(429), http_error(503), {"quotes": {"quote": {"symbol": "SPY", "last": 5}}})
        out = tradier.quotes(["SPY"], env=ENV, opener=op, sleep=NOSLEEP)
        self.assertEqual(out["SPY"]["last"], 5)
        self.assertEqual(len(op.seen), 3)

    def test_gives_up_after_the_retries(self):
        op = Opener(http_error(500), http_error(500), http_error(500))
        with self.assertRaises(tradier.TradierError) as cm:
            tradier.quotes(["SPY"], env=ENV, opener=op, sleep=NOSLEEP)
        self.assertNotIn("SECRET-TOKEN-123", str(cm.exception))

    def test_network_errors_are_retried_then_raised_as_tradier_errors(self):
        op = Opener(urllib.error.URLError("boom"), urllib.error.URLError("boom"), urllib.error.URLError("boom"))
        with self.assertRaises(tradier.TradierError):
            tradier.quotes(["SPY"], env=ENV, opener=op, sleep=NOSLEEP)


class Parsing(unittest.TestCase):
    def test_quotes_handles_one_result_many_results_and_none(self):
        one = Opener({"quotes": {"quote": {"symbol": "spy", "last": 1}}})
        self.assertEqual(list(tradier.quotes("spy", env=ENV, opener=one, sleep=NOSLEEP)), ["SPY"])
        many = Opener({"quotes": {"quote": [{"symbol": "SPY", "last": 1}, {"symbol": "QQQ", "last": 2}]}})
        self.assertEqual(sorted(tradier.quotes(["SPY", "QQQ"], env=ENV, opener=many, sleep=NOSLEEP)), ["QQQ", "SPY"])
        none = Opener({"quotes": {"unmatched_symbols": {"symbol": "ZZZZ"}}})
        self.assertEqual(tradier.quotes(["ZZZZ"], env=ENV, opener=none, sleep=NOSLEEP), {})

    def test_quotes_are_chunked(self):
        syms = [f"S{i}" for i in range(120)]
        op = Opener({"quotes": {"quote": []}}, {"quotes": {"quote": []}}, {"quotes": {"quote": []}})
        tradier.quotes(syms, env=ENV, opener=op, sleep=NOSLEEP)
        self.assertEqual(len(op.seen), 3)

    def test_quote_age_and_mid(self):
        q = {"trade_date": 1_000_000_000_000, "bid_date": 1_000_000_060_000, "bid": 1.0, "ask": 1.2}
        self.assertAlmostEqual(tradier.quote_age_s(q, now=1_000_000_120.0), 60.0)
        self.assertIsNone(tradier.quote_age_s({}))
        self.assertEqual(tradier.mid(q), 1.1)
        self.assertIsNone(tradier.mid({"bid": 0, "ask": 1.0}))          # a zero bid is not a price
        self.assertIsNone(tradier.mid({"bid": 2.0, "ask": 1.0}))        # crossed market is not a price
        self.assertIsNone(tradier.mid({"last": 3.0}))                   # never falls back to a stale last print

    def test_chain_and_expirations_single_item_forms(self):
        op = Opener({"options": {"option": {"symbol": "SPY261016C00700000", "strike": 700.0}}})
        self.assertEqual(len(tradier.chain("SPY", "2026-10-16", env=ENV, opener=op, sleep=NOSLEEP)), 1)
        op = Opener({"expirations": {"date": "2026-10-16"}})
        self.assertEqual(tradier.expirations("SPY", env=ENV, opener=op, sleep=NOSLEEP), ["2026-10-16"])
        op = Opener({"options": None})
        self.assertEqual(tradier.chain("SPY", "2026-10-16", env=ENV, opener=op, sleep=NOSLEEP), [])

    def test_timesales_none(self):
        self.assertEqual(tradier.timesales("SPY", "2026-10-16 09:30", env=ENV, opener=Opener({"series": None}),
                                           sleep=NOSLEEP), [])

    def test_daily_bars_dataframe_shape(self):
        days = [{"date": "2026-10-14", "open": 100, "high": 102, "low": 99, "close": 101, "volume": 5},
                {"date": "2026-10-15", "open": 101, "high": 103, "low": 100, "close": 102, "volume": 6}]
        df = tradier.daily_bars("SPY", env=ENV, opener=Opener({"history": {"day": days}}), sleep=NOSLEEP,
                                today=dt.date(2026, 10, 16))
        self.assertEqual(list(df.columns), ["open", "high", "low", "close"])
        self.assertEqual(df.index[-1], pd.Timestamp("2026-10-15"))
        self.assertEqual(float(df["close"].iloc[-1]), 102.0)

    def test_daily_bars_single_day_and_empty(self):
        one = {"history": {"day": {"date": "2026-10-15", "open": 1, "high": 2, "low": 0.5, "close": 1.5}}}
        self.assertEqual(len(tradier.daily_bars("SPY", env=ENV, opener=Opener(one), sleep=NOSLEEP)), 1)
        with self.assertRaises(tradier.TradierError):
            tradier.daily_bars("SPY", env=ENV, opener=Opener({"history": None}), sleep=NOSLEEP)


def flat_bars(n=40):
    idx = pd.bdate_range(end=pd.Timestamp("2026-10-15"), periods=n)
    return pd.DataFrame({"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0}, index=idx)


class Wiring(unittest.TestCase):
    def test_without_a_key_the_bots_use_exactly_the_old_sources(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual([n for n, _ in bot.data_sources()], ["yahoo", "stooq"])
            self.assertEqual([n for n, _ in mom.data_sources()], ["yahoo", "stooq"])
            self.assertEqual([f.__name__ for f in ks.data_sources()], ["fetch_yahoo", "fetch_stooq"])

    def test_with_a_key_tradier_goes_first_and_the_old_sources_remain_as_fallbacks(self):
        with mock.patch.dict("os.environ", {"TRADIER_API_KEY": "x"}, clear=True):
            self.assertEqual([n for n, _ in bot.data_sources()], ["tradier", "yahoo", "stooq"])
            self.assertEqual([n for n, _ in mom.data_sources()], ["tradier", "yahoo", "stooq"])
            self.assertEqual([f.__name__ for f in ks.data_sources()],
                             ["fetch_tradier", "fetch_yahoo", "fetch_stooq"])

    def test_a_tradier_outage_falls_back_to_yahoo(self):
        yahoo = flat_bars()
        with mock.patch.dict("os.environ", {"TRADIER_API_KEY": "x"}, clear=True), \
                mock.patch.object(bot, "fetch_tradier", side_effect=tradier.TradierError("HTTP 401")), \
                mock.patch.object(bot, "fetch_yahoo", return_value=yahoo):
            df = bot.get_bars("SPY", expected=dt.date(2026, 10, 15))
        self.assertIs(df, yahoo)

    def test_tradier_bars_win_when_they_have_the_expected_session(self):
        tr = flat_bars()
        with mock.patch.dict("os.environ", {"TRADIER_API_KEY": "x"}, clear=True), \
                mock.patch.object(bot, "fetch_tradier", return_value=tr), \
                mock.patch.object(bot, "fetch_yahoo", side_effect=AssertionError("must not be called")):
            self.assertIs(bot.get_bars("SPY", expected=dt.date(2026, 10, 15)), tr)

    def test_momentum_closes_come_from_tradier_when_configured(self):
        idx = pd.bdate_range(end=pd.Timestamp("2026-10-15"), periods=300)
        tr = pd.DataFrame({"close": 100.0}, index=idx)
        with mock.patch.dict("os.environ", {"TRADIER_API_KEY": "x"}, clear=True), \
                mock.patch.object(mom, "fetch_tradier", return_value=tr), \
                mock.patch.object(mom, "fetch_yahoo", side_effect=AssertionError("must not be called")):
            self.assertIs(mom.get_closes("AAPL", sleep=NOSLEEP), tr)


if __name__ == "__main__":
    unittest.main()
