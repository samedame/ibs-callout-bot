#!/usr/bin/env python3
"""Tradier market-data client for the alert bots. Standard library only (pandas is imported lazily in daily_bars).

READ-ONLY by construction: this module only ever calls GET /v1/markets/*. It never touches /accounts or /orders, so a
leaked or mis-scoped key cannot place a trade through this code.

Configuration (environment variables; in GitHub Actions they come from repository secrets, the same names in every bot):
  TRADIER_API_KEY   your Tradier access token (the name the condor bot already used; TRADIER_TOKEN also works). Unset or
                    empty -> configured() is False and every bot silently keeps the sources it used before
                    (Yahoo / Stooq / yfinance / Cboe). Never printed or logged.
  TRADIER_BASE_URL  optional. Leave it unset for a production key. For a sandbox key set it to
                    https://sandbox.tradier.com/v1 . Only api.tradier.com and sandbox.tradier.com are accepted, so the
                    token can never be sent to any other host. (TRADIER_ENV=sandbox|production does the same job.)
                    A sandbox key ONLY works on the sandbox host and a production key ONLY on the production host;
                    a mismatch is a 401. Sandbox data is about 15 minutes delayed; real-time needs a production key
                    on a funded Tradier Brokerage account. Bots label sandbox prices as delayed (see data_mode()).
  TRADIER_MIN_GAP_S optional override of the pause between requests (default 0.55 s production, 1.1 s sandbox, to stay
                    under Tradier's 120 / 60 requests per minute).

Self-check from a terminal (prints the mode, last price and quote age, never the token):
  TRADIER_API_KEY=... [TRADIER_BASE_URL=https://sandbox.tradier.com/v1] python tradier.py            # SPY
  TRADIER_API_KEY=... python tradier.py QQQ IWM
"""
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASES = {"production": "https://api.tradier.com/v1", "sandbox": "https://sandbox.tradier.com/v1"}
UA = "ibs-alert-bots/1.0 (+tradier-client)"
QUOTE_CHUNK = 50          # symbols per /markets/quotes call

_last_call = [0.0]


class TradierError(Exception):
    """Any failure to get usable data from Tradier. The message never contains the token."""


# ----------------------------------------------------------------------------- configuration

def _env(env=None):
    return os.environ if env is None else env


def token(env=None):
    e = _env(env)
    return (e.get("TRADIER_API_KEY") or e.get("TRADIER_TOKEN") or "").strip()


def configured(env=None):
    return bool(token(env))


_HOST_MODE = {"api.tradier.com": "production", "sandbox.tradier.com": "sandbox"}


def mode(env=None):
    """'production' or 'sandbox'. TRADIER_BASE_URL wins when set (and must be one of Tradier's two hosts over https);
    otherwise TRADIER_ENV; otherwise production."""
    e = _env(env)
    base = (e.get("TRADIER_BASE_URL") or "").strip()
    if base:
        u = urllib.parse.urlparse(base)
        if u.scheme != "https" or u.hostname not in _HOST_MODE:
            raise TradierError("TRADIER_BASE_URL must be https://api.tradier.com/v1 or https://sandbox.tradier.com/v1")
        return _HOST_MODE[u.hostname]
    m = (e.get("TRADIER_ENV") or "production").strip().lower()
    if m not in BASES:
        raise TradierError(f"TRADIER_ENV must be 'production' or 'sandbox', not {m!r}")
    return m


def data_mode(env=None):
    """Human label for alerts/logs: how fresh the prices from this key can be."""
    return "Tradier sandbox (about 15 min delayed)" if mode(env) == "sandbox" else "Tradier production"


def is_delayed(env=None):
    return mode(env) == "sandbox"


def _min_gap(env=None):
    raw = _env(env).get("TRADIER_MIN_GAP_S")
    if raw not in (None, ""):
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
    return 1.1 if mode(env) == "sandbox" else 0.55


# ----------------------------------------------------------------------------- transport

def _get(path, params=None, env=None, opener=None, tries=3, sleep=time.sleep, clock=time.monotonic):
    """GET one /markets endpoint and return the decoded JSON. Retries 429 / 5xx / network blips; fails fast on
    400 / 401 / 403 (bad key, wrong TRADIER_ENV, bad parameters) because repeating those never helps."""
    tok = token(env)
    if not tok:
        raise TradierError("TRADIER_API_KEY is not set")
    if not path.startswith("/markets/"):
        raise TradierError(f"refusing non-market path {path!r}")          # keeps this module read-only
    opener = opener or urllib.request.urlopen
    url = BASES[mode(env)] + path
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}", "Accept": "application/json",
                                               "User-Agent": UA})
    last = "no attempt made"
    for attempt in range(1, max(1, tries) + 1):
        gap = _min_gap(env) - (clock() - _last_call[0])
        if gap > 0:
            sleep(gap)
        _last_call[0] = clock()
        try:
            with opener(req, timeout=30) as r:
                body = r.read()
            return json.loads(body.decode("utf-8") if isinstance(body, bytes) else body)
        except urllib.error.HTTPError as e:
            if e.code in (400, 401, 403, 404):
                hint = {401: " (check TRADIER_API_KEY, and that the host matches the key: TRADIER_BASE_URL sandbox vs production)",
                        403: " (this key is not allowed to read that data)"}.get(e.code, "")
                raise TradierError(f"HTTP {e.code} from {path}{hint}") from None
            last = f"HTTP {e.code}"
            if e.code == 429:
                wait = 0
                try:
                    exp = int(e.headers.get("X-Ratelimit-Expiry", "0"))      # epoch ms when the window resets
                    wait = max(0.0, exp / 1000.0 - time.time())
                except Exception:  # noqa
                    pass
                sleep(min(max(wait, 2.0 * attempt), 30.0))
            else:
                sleep(2.0 * attempt)
        except (urllib.error.URLError, OSError, ValueError) as e:
            last = f"{type(e).__name__}"
            sleep(2.0 * attempt)
    raise TradierError(f"{path} failed after {tries} tries ({last})")


def _as_list(x):
    """Tradier returns a bare object for one result and a list for several; null for none."""
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


# ----------------------------------------------------------------------------- endpoints

def quotes(symbols, env=None, **kw):
    """{SYMBOL: quote dict} for every symbol Tradier recognises. Fields include last, bid, ask, volume, open, high,
    low, close, prevclose, trade_date / bid_date / ask_date (epoch ms)."""
    syms = [s.strip().upper() for s in ([symbols] if isinstance(symbols, str) else symbols) if s and s.strip()]
    out = {}
    for i in range(0, len(syms), QUOTE_CHUNK):
        j = _get("/markets/quotes", {"symbols": ",".join(syms[i:i + QUOTE_CHUNK]), "greeks": "false"}, env=env, **kw)
        for q in _as_list((j.get("quotes") or {}).get("quote")):
            if q.get("symbol"):
                out[q["symbol"].upper()] = q
    return out


def quote_age_s(q, now=None, env=None):
    """Seconds since the newest of this quote's trade / bid / ask timestamps; None when it carries none."""
    stamps = [q.get(k) for k in ("trade_date", "bid_date", "ask_date") if q.get(k)]
    if not stamps:
        return None
    now = time.time() if now is None else now
    return max(0.0, now - max(stamps) / 1000.0)


def mid(q):
    """(bid + ask) / 2 when both sides are live, else None. Never falls back to a stale last print."""
    try:
        b, a = float(q.get("bid") or 0), float(q.get("ask") or 0)
    except (TypeError, ValueError):
        return None
    return round((a + b) / 2.0, 4) if b > 0 and a >= b else None


def history(symbol, start, end=None, interval="daily", env=None, **kw):
    """Daily / weekly / monthly bars as a list of {date, open, high, low, close, volume}."""
    j = _get("/markets/history", {"symbol": symbol.upper(), "interval": interval, "start": start, "end": end},
             env=env, **kw)
    return _as_list((j.get("history") or {}).get("day"))


def timesales(symbol, start, end=None, interval="1min", session_filter="open", env=None, **kw):
    """Intraday bars: list of {time, timestamp, open, high, low, close, volume, vwap}. `start` / `end` are
    'YYYY-MM-DD HH:MM' in New York time."""
    j = _get("/markets/timesales", {"symbol": symbol.upper(), "interval": interval, "start": start, "end": end,
                                    "session_filter": session_filter}, env=env, **kw)
    return _as_list((j.get("series") or {}).get("data"))


def expirations(symbol, env=None, **kw):
    j = _get("/markets/options/expirations", {"symbol": symbol.upper(), "includeAllRoots": "true"}, env=env, **kw)
    return [str(d) for d in _as_list((j.get("expirations") or {}).get("date"))]


def chain(symbol, expiration, greeks=True, env=None, **kw):
    """Every listed option for one expiration: list of {symbol, strike, option_type ('call' / 'put'), bid, ask, last,
    volume, open_interest, bid_date, ask_date, greeks: {delta, mid_iv, ...}}."""
    j = _get("/markets/options/chains", {"symbol": symbol.upper(), "expiration": expiration,
                                         "greeks": "true" if greeks else "false"}, env=env, **kw)
    return _as_list((j.get("options") or {}).get("option"))


def option_mid(opt):
    return mid(opt)


# ----------------------------------------------------------------------------- DataFrames for the daily-bar bots

def daily_bars(symbol, days=90, env=None, today=None, **kw):
    """Daily bars as a DataFrame indexed by session date with open / high / low / close columns, the same shape the
    Yahoo and Stooq fetchers in these bots return. Raises TradierError when nothing usable comes back."""
    import pandas as pd
    start = ((today or dt.date.today()) - dt.timedelta(days=int(days))).isoformat()
    rows = history(symbol, start, env=env, **kw)
    if not rows:
        raise TradierError(f"no daily bars for {symbol}")
    df = pd.DataFrame(rows)
    df.index = pd.to_datetime(df["date"])
    df.index.name = None
    cols = [c for c in ("open", "high", "low", "close") if c in df.columns]
    return df[cols].astype(float).dropna()


# ----------------------------------------------------------------------------- self-check

def _check(symbols, env=None):
    if not configured(env):
        print("TRADIER_API_KEY is not set: nothing to check.")
        return 1
    print(f"mode: {data_mode(env)}")
    try:
        qs = quotes(symbols, env=env)
    except TradierError as e:
        print(f"FAILED: {e}")
        return 1
    missing = [s for s in symbols if s.upper() not in qs]
    for s, q in qs.items():
        age = quote_age_s(q)
        age_txt = "no timestamp" if age is None else f"{age / 60:.1f} min old"
        print(f"{s}: last {q.get('last')}  bid {q.get('bid')}  ask {q.get('ask')}  ({age_txt})")
    if missing:
        print("not found: " + ", ".join(missing))
    return 0 if qs and not missing else 1


if __name__ == "__main__":
    sys.exit(_check(sys.argv[1:] or ["SPY"]))
