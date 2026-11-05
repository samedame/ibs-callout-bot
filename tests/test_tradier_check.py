"""Offline tests for tradier_check.py (fake Tradier responses; nothing touches the network)."""
import io
import json
import os
import sys
import unittest
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "."))
import tradier_check as tc  # noqa: E402

NOW = 1_800_000_000.0
KEY = {"TRADIER_API_KEY": "SECRET-ABC", "TRADIER_MIN_GAP_S": "0"}


class Resp:
    def __init__(self, d):
        self.b = json.dumps(d).encode()

    def read(self):
        return self.b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


def opener(age_s, state="open", http=None, no_quote=False):
    def op(req, timeout=30):
        u = req.full_url
        if http:
            raise urllib.error.HTTPError(u, http, "x", {}, io.BytesIO(b""))
        st = int((NOW - age_s) * 1000)
        if "/clock" in u:
            return Resp({"clock": {"state": state}})
        if "/quotes" in u:
            return Resp({"quotes": {}} if no_quote else {"quotes": {"quote": {
                "symbol": "SPY", "last": 600.0, "bid": 599.9, "ask": 600.1, "trade_date": st, "bid_date": st, "ask_date": st}}})
        if "/expirations" in u:
            return Resp({"expirations": {"date": ["2026-11-05"]}})
        if "/chains" in u:
            return Resp({"options": {"option": [{"strike": 600, "bid_date": st, "ask_date": st}]}})
        raise AssertionError(u)
    return op


class Check(unittest.TestCase):
    def go(self, env, **kw):
        out = []
        rc = tc.run(env=env, now=NOW, say=out.append, sleep=lambda s: None, **kw)
        self.assertFalse(any("SECRET-ABC" in l for l in out), "the key must never be printed")
        return rc, out

    def test_live_production(self):
        self.assertEqual(self.go(KEY, opener=opener(10))[0], 0)

    def test_empty_base_url_secret_means_production(self):
        self.assertEqual(self.go(dict(KEY, TRADIER_BASE_URL=""), opener=opener(10))[0], 0)

    def test_stale_quotes_while_open_fail(self):
        self.assertEqual(self.go(KEY, opener=opener(14 * 60))[0], 1)

    def test_closed_market_passes_without_judging_freshness(self):
        self.assertEqual(self.go(KEY, opener=opener(7200, "closed"))[0], 0)

    def test_sandbox_host_fails(self):
        rc, out = self.go(dict(KEY, TRADIER_BASE_URL="https://sandbox.tradier.com/v1"), opener=opener(10))
        self.assertEqual(rc, 1)
        self.assertIn("sandbox", out[-1])

    def test_missing_key_fails(self):
        self.assertEqual(self.go({"TRADIER_API_KEY": ""}, opener=opener(10))[0], 1)

    def test_unknown_host_is_refused(self):
        self.assertEqual(self.go(dict(KEY, TRADIER_BASE_URL="https://evil.example.com/v1"), opener=opener(10))[0], 1)

    def test_rejected_key_fails(self):
        self.assertEqual(self.go(KEY, opener=opener(10, http=401))[0], 1)

    def test_no_quote_fails(self):
        self.assertEqual(self.go(KEY, opener=opener(10, no_quote=True))[0], 1)


if __name__ == "__main__":
    unittest.main()
