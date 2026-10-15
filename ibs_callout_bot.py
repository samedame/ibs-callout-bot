#!/usr/bin/env python3
"""IBS callout bot — daily alerts for the index-ETF IBS mean-reversion strategy.

Rules (frozen 2026-09-12, from the out-of-sample study):
  Universe : SPY, QQQ, IWM, DIA   (equal sleeves; MDY optional)
  ENTRY    : IBS = (close - low) / (high - low) < 0.10  at the daily close
             -> buy at the NEXT regular-session open
  EXIT     : close > previous day's high   OR   10 trading days held
             -> sell at the NEXT open
  Sizing   : one sleeve per ETF (25% of the bot's capital each), no leverage, no averaging down.

The bot only sends alerts; you place the orders (market-on-open works; a limit near the open also works).
State (which sleeves are open, which orders are still pending) is kept in state.json.

Usage:
  python ibs_callout_bot.py --webhook $DISCORD_WEBHOOK          # evening run, after the close
  python ibs_callout_bot.py --webhook $DISCORD_WEBHOOK --morning # pre-open reminder of pending orders
  python ibs_callout_bot.py --dry-run                            # print, don't post, don't save
  python ibs_callout_bot.py --force                              # re-evaluate a close that was already processed

Exit code: 0 = done (or nothing to do); 1 = the run could not deliver (stale price data, Discord refused the message,
no webhook). The workflow turns a non-zero exit into a "this run failed" message in Discord.

What the evening run guarantees (each of these was a silent failure before 2026-10-14):
  * It evaluates ONE specific session: the last one that has closed as of the New York clock. A price file that has
    not caught up yet (Yahoo lists the new session with no close for a while) is retried for ~15 minutes; if it never
    does, you get a "no signals tonight" message instead of old signals presented as new ones.
  * Dates in messages are New York dates (the GitHub runner clock is UTC, so a late run used to print tomorrow's date).
  * Hold counts come from the price history (bars after the signal bar), not from "how many times the bot ran".
  * Running it twice for the same close does nothing the second time (unless --force).
  * Orders are saved to state.json BEFORE the Discord post, so the 9:00 AM ET reminder can repeat them even if
    the evening message never arrived.
"""
import argparse, datetime as dt, json, os, sys, time
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

import pandas as pd

TICKERS = ["SPY", "QQQ", "IWM", "DIA"]
IBS_ENTRY = 0.10
MAX_HOLD_DAYS = 10
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
NOTIFIED_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".notified")   # tells notify_failure.py to stay quiet
UA = {"User-Agent": "Mozilla/5.0 (compatible; ibs-callout-bot/1.0)"}

ET = ZoneInfo("America/New_York")
OPEN_AT = dt.time(9, 30)
SETTLE_AT = dt.time(16, 5)      # a session's daily bar counts as final from here
FRESH_TRIES = 4                 # looks at the price data per evening run ...
FRESH_WAIT_S = 300              # ... this many seconds apart (so ~15 min in total before giving up)

# NYSE full-day holidays 2026-2027. Only used to know which session a run should be looking at and which
# mornings have no market. Verify annually (nyse.com/markets/hours-calendars); an unlisted holiday shows up as a
# loud "price data is stale" message, never as a wrong signal.
HOLIDAYS = {"2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19", "2026-07-03",
            "2026-09-07", "2026-11-26", "2026-12-25", "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26",
            "2027-05-31", "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24"}


# ----------------------------------------------------------------------------- calendar / dates

def now_et():
    return dt.datetime.now(ET)


def parse_now(s):
    t = dt.datetime.fromisoformat(s)
    return t if t.tzinfo else t.replace(tzinfo=ET)


def is_session(d):
    return d.weekday() < 5 and d.isoformat() not in HOLIDAYS


def prev_session(d):
    d -= dt.timedelta(days=1)
    while not is_session(d):
        d -= dt.timedelta(days=1)
    return d


def next_session(d):
    d += dt.timedelta(days=1)
    while not is_session(d):
        d += dt.timedelta(days=1)
    return d


def expected_last_session(now):
    """The most recent session whose daily bar should be final at `now` (a New York time)."""
    d = now.date()
    if is_session(d) and now.time() >= SETTLE_AT:
        return d
    return prev_session(d)


def sessions_between(a, b):
    """Sessions strictly after `a` and strictly before `b`."""
    out, d = [], next_session(a)
    while d < b:
        out.append(d)
        d = next_session(d)
    return out


def fmt_day(d):
    return f"{d:%a %b} {d.day}"


# ----------------------------------------------------------------------------- data

def fetch_yahoo(ticker, days=90):
    """Daily OHLC from Yahoo's chart endpoint (no key needed)."""
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
           f"?range={days}d&interval=1d&events=div,splits")
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        j = json.load(r)
    res = j["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    idx = pd.to_datetime(res["timestamp"], unit="s", utc=True).tz_convert("America/New_York").normalize().tz_localize(None)
    df = pd.DataFrame({"open": q["open"], "high": q["high"], "low": q["low"], "close": q["close"]}, index=idx)
    return df.dropna()


def fetch_stooq(ticker, days=90):
    """Fallback: Stooq daily CSV."""
    url = f"https://stooq.com/q/d/l/?s={ticker.lower()}.us&i=d"
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        df = pd.read_csv(r)
    df.columns = [c.lower() for c in df.columns]
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    return df[["open", "high", "low", "close"]].tail(days + 5)


def trim_to(df, last_date):
    """Drop bars dated after `last_date` (a run before the close would otherwise see today's still-forming bar)."""
    return df[df.index.date <= last_date]


def last_bar_date(df):
    return df.index[-1].date() if len(df) else None


def get_bars(ticker, expected=None):
    """Daily bars for `ticker`. With `expected` (a date) the first source whose newest bar IS that session wins; if
    none has it, the freshest one is returned and the caller decides what a stale file means."""
    errs, best = [], None
    for name, fn in (("yahoo", fetch_yahoo), ("stooq", fetch_stooq)):
        try:
            df = fn(ticker)
        except Exception as e:  # noqa
            errs.append(f"{name}: {e}")
            continue
        if len(df) <= 20:
            errs.append(f"{name}: only {len(df)} bars")
            continue
        if expected is None or last_bar_date(trim_to(df, expected)) == expected:
            return df
        if best is None or df.index[-1] > best.index[-1]:
            best = df
    if best is not None:
        return best
    raise RuntimeError(f"no data for {ticker}: {'; '.join(errs)}")


class StaleData(Exception):
    def __init__(self, expected, problems, tries):
        self.expected, self.problems, self.tries = expected, problems, tries
        super().__init__(f"stale data for the {expected} close: {problems}")


def load_bars(expected, get=get_bars, sleep=time.sleep, tries=FRESH_TRIES, wait=FRESH_WAIT_S, log=print):
    """{ticker: bars up to and including `expected`} once EVERY ticker's newest bar is `expected`.
    Otherwise retry (the daily file is often a few minutes behind right after the close), then raise StaleData."""
    problems = {}
    for attempt in range(max(1, tries)):
        bars, problems = {}, {}
        for t in TICKERS:
            try:
                df = trim_to(get(t, expected), expected)
            except Exception as e:  # noqa
                problems[t] = f"fetch failed ({e})"
                continue
            last = last_bar_date(df)
            if last != expected or len(df) < 3:
                problems[t] = f"newest bar {last}" if last else "no bars"
            else:
                bars[t] = df
        if not problems:
            return bars
        if attempt < tries - 1:
            log(f"[ibs] price data not final for the {expected} close ({problems}); "
                f"looking again in {wait // 60} min ({attempt + 1}/{tries - 1})")
            sleep(wait)
    raise StaleData(expected, problems, tries)


def ibs(row):
    rng = row["high"] - row["low"]
    return 0.5 if rng <= 0 else (row["close"] - row["low"]) / rng


def held_bars(df, entry_date):
    """Trading days held so far: bars dated AFTER the signal bar (`entry_date`; the buy is at the next open)."""
    e = dt.date.fromisoformat(entry_date)
    return int((df.index.date > e).sum())


# ----------------------------------------------------------------------------- state

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"positions": {}, "history": []}


def save_state(st):
    with open(STATE_FILE, "w") as f:
        json.dump(st, f, indent=1)


# ----------------------------------------------------------------------------- alerts

class PostFailed(Exception):
    pass


def post_discord(webhook, content, tries=4, sleep=time.sleep, opener=urllib.request.urlopen):
    """POST one message. Retries 429 (honouring retry_after), 5xx and network errors; a 4xx other than 429 (dead
    webhook, bad request) is final. Raises PostFailed so the caller can tell the run failed instead of pretending."""
    data = json.dumps({"content": content[:1900], "allowed_mentions": {"parse": []}}).encode()
    last = "no attempt"
    for i in range(tries):
        req = urllib.request.Request(webhook, data=data, headers={"Content-Type": "application/json", **UA})
        try:
            with opener(req, timeout=30) as r:
                if 200 <= r.status < 300:
                    return r.status
                last = f"HTTP {r.status}"
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code == 429:
                try:
                    wait = float(json.loads(e.read().decode()).get("retry_after", 2))
                except Exception:  # noqa
                    wait = 2.0
                if i < tries - 1:
                    sleep(min(max(wait, 0.5), 15.0))
                continue
            if 400 <= e.code < 500:
                raise PostFailed(last) from e
        except (urllib.error.URLError, OSError) as e:
            last = f"network: {e}"
        if i < tries - 1:
            sleep(2 + 3 * i)
    raise PostFailed(last)


def stale_notice(e):
    which = ", ".join(f"{t} ({why})" for t, why in e.problems.items())
    return (f"⚠️ **IBS callout bot — NO SIGNALS for the {fmt_day(e.expected)} close.**\n"
            f"The price data never caught up after {e.tries} looks over ~{(e.tries - 1) * FRESH_WAIT_S // 60} min: {which}.\n"
            f"Nothing was evaluated and nothing was changed, so don't treat \"no message\" as \"no trade\". "
            f"Either check the four charts yourself (IBS = (close − low) / (high − low); below 0.10 → buy at the next open; "
            f"close above yesterday's high or 10 days held → sell at the next open), or re-run: GitHub → Actions → "
            f"IBS callout bot → Run workflow → mode: evening.")


# ----------------------------------------------------------------------------- the evening run

def evaluate(state, bars, expected):
    """What the close says. Pure: does not touch `state`."""
    rows, buys, sells = [], [], []
    for t in TICKERS:
        df = bars[t]
        last, prev = df.iloc[-1], df.iloc[-2]
        v = ibs(last)
        close = float(last["close"])
        pos = state["positions"].get(t)
        held = signal = None
        if pos:
            held = held_bars(df, pos["entry_date"])
            above = bool(last["close"] > prev["high"])
            if above or held >= MAX_HOLD_DAYS:
                signal = "SELL"
                sells.append({"sym": t, "close": close, "held": held, "entry_date": pos.get("entry_date"),
                              "why": "close above yesterday's high" if above else f"{held} trading days held"})
        elif v < IBS_ENTRY:
            signal = "BUY"
            buys.append({"sym": t, "ibs": v, "close": close})
        rows.append({"sym": t, "close": close, "ibs": v, "in_position": bool(pos), "held": held, "signal": signal})
    return {"bar": expected.isoformat(), "rows": rows, "buys": buys, "sells": sells}


def apply_changes(state, ev):
    """Record the orders the close produced (assumed filled at the next open, as before)."""
    b = ev["bar"]
    sold = {s["sym"] for s in ev["sells"]}
    for r in ev["rows"]:
        pos = state["positions"].get(r["sym"])
        if pos and r["sym"] not in sold:
            pos["bars"], pos["last_bar"] = r["held"], b
            if pos.get("pending") and pos.get("entry_date", b) < b:
                pos["pending"] = False          # its pre-open reminder never ran (see evening_notes); counted as held
    for s in ev["sells"]:
        pos = state["positions"].pop(s["sym"])
        pos["bars"], pos["last_bar"] = s["held"], b
        state["history"].append({**pos, "sym": s["sym"], "exit_signal_date": b, "reason": s["why"]})
        state.setdefault("pending_exits", {})[s["sym"]] = {"why": s["why"], "signal_bar": b,
                                                          "entry_date": s["entry_date"], "held": s["held"]}
    for x in ev["buys"]:
        state["positions"][x["sym"]] = {"entry_date": b, "signal_ibs": round(x["ibs"], 4), "bars": 0, "pending": True}
    state["last_bar"] = b


def render_evening(ev, expected, now, notes=()):
    nxt = next_session(expected)
    when = f"{fmt_day(nxt)} open"
    out = [f"**IBS callout bot — signals from the {fmt_day(expected)} close**"]
    if now >= dt.datetime.combine(nxt, OPEN_AT, tzinfo=ET):
        out.append(f"⚠️ **Late message:** the {when} (9:30 AM ET) has already passed. Check the current price before "
                   f"placing anything; if the ETF has gapped far from the close, skipping is allowed.")
    out.extend(notes)
    if ev["buys"]:
        out.append(f"\n__BUY at the {when}__ (market-on-open, 25% of the bot's capital per ETF):")
        for x in ev["buys"]:
            out.append(f"• **{x['sym']}** — IBS {x['ibs']:.2f}, close {x['close']:.2f}")
    if ev["sells"]:
        out.append(f"\n__SELL at the {when}__ (market-on-open, the whole position):")
        for s in ev["sells"]:
            bought = fmt_day(next_session(dt.date.fromisoformat(s["entry_date"])))
            out.append(f"• **{s['sym']}** — {s['why']} (bought at the {bought} open, held {s['held']} days)")
    if ev["buys"] or ev["sells"]:
        out.append(f"\nA reminder with these orders posts at 9:00 AM ET (7:00 AM MT) on {fmt_day(nxt)}.")
    else:
        out.append(f"\nNo action at the {when}.")
    table = []
    for r in ev["rows"]:
        tag = {"BUY": "  [BUY signal]", "SELL": "  [SELL signal]"}.get(r["signal"])
        if tag is None and r["in_position"]:
            tag = f"  [in position, held {r['held']}/{MAX_HOLD_DAYS} days]"
        table.append(f"{r['sym']}: close {r['close']:.2f}  IBS {r['ibs']:.2f}" + (tag or ""))
    out.append("\n```\n" + "\n".join(table) + "\n```")
    return "\n".join(out)


def evening_notes(state, expected):
    notes = []
    last = state.get("last_bar")
    if last:
        missed = sessions_between(dt.date.fromisoformat(last), expected)
        if missed:
            notes.append("⚠️ No evening run was recorded for " + ", ".join(fmt_day(d) for d in missed) +
                         ": signals on those closes were not evaluated. Hold counts are re-counted from price "
                         "history, so they are still right.")
    never = [t for t, p in state["positions"].items() if p.get("pending") and p.get("entry_date", "9") < expected.isoformat()]
    if never:
        notes.append("⚠️ The 9:00 AM ET reminder never ran for the " + ", ".join(never) + " BUY signal"
                     f"{'s' if len(never) > 1 else ''} from earlier. The bot counts "
                     f"{'them' if len(never) > 1 else 'it'} as held; if you never bought, skip the exit too.")
    return notes


# ----------------------------------------------------------------------------- the pre-open reminder

def render_morning(state, now):
    sells = state.get("pending_exits") or {}
    buys = [t for t, p in state["positions"].items() if p.get("pending")]
    hold = [(t, p) for t, p in state["positions"].items() if not p.get("pending")]
    out = [f"**Pre-open reminder — {fmt_day(now.date())}** — place orders before the 9:30 AM ET open (7:30 AM MT)."]
    unsent = state.get("unsent")
    if unsent and unsent.get("kind") == "evening":
        out.append("⚠️ Last night's signal message never posted to Discord. The orders below come from the saved state.")
    if sells:
        out.append("**SELL at the open** (market-on-open, the whole position): " +
                   "; ".join(f"**{t}** — {v['why']} (signal: {fmt_day(dt.date.fromisoformat(v['signal_bar']))} close)"
                              for t, v in sells.items()))
    if buys:
        out.append("**BUY at the open** (market-on-open, 25% of the bot's capital per ETF): " +
                   ", ".join(f"**{t}**" for t in buys))
    if not sells and not buys:
        out.append("No orders today.")
    if hold:
        out.append("Holding, no action: " + ", ".join(
            f"{t} (held {p.get('bars', 0)} of {MAX_HOLD_DAYS} days so far)" for t, p in hold))
    return "\n".join(out)


def mark_morning_done(state):
    for p in state["positions"].values():
        p["pending"] = False
    state["pending_exits"] = {}
    state.pop("unsent", None)


# ----------------------------------------------------------------------------- runs

def deliver(state, text, kind, bar, webhook, post, on_ok=None):
    """Post `text`; save state either way. Returns 0 if it was posted, 1 if not (and says so on stderr)."""
    problem = None
    if not webhook:
        problem = "DISCORD_WEBHOOK is not set"
    else:
        try:
            post(webhook, text)
        except Exception as e:  # noqa
            problem = f"Discord refused the message ({e})"
    if problem:
        print(f"[ibs] NOT POSTED: {problem}. The orders are saved in state.json; the 9:00 AM ET reminder repeats them.",
              file=sys.stderr)
        state["unsent"] = {"kind": kind, "bar": bar}
        save_state(state)
        return 1
    if on_ok:
        on_ok(state)
    state.pop("unsent", None)
    save_state(state)
    return 0


def run_evening(state, now, webhook="", dry_run=False, force=False, get=get_bars, sleep=time.sleep, post=post_discord):
    expected = expected_last_session(now)
    b = expected.isoformat()
    saved = state.get("last_evening") or {}
    if state.get("last_bar") == b and saved.get("bar") == b:
        # This close was already evaluated. Never evaluate twice (that is what used to double-count hold days).
        un = state.get("unsent") or {}
        if (un.get("kind") == "evening" and un.get("bar") == b) or force:
            print(f"[ibs] re-sending the saved {b} message.")
            print(saved["text"])
            return 0 if dry_run else deliver(state, saved["text"], "evening", b, webhook, post)
        print(f"[ibs] the {b} close was already processed and posted; nothing to do "
              f"(--force re-sends that message). Saved message:\n{saved['text']}")
        return 0
    try:
        bars = load_bars(expected, get=get, sleep=sleep)
    except StaleData as e:
        notice = stale_notice(e)
        print(notice)
        if webhook and not dry_run:
            try:
                post(webhook, notice)
                with open(NOTIFIED_FILE, "w") as f:         # Discord already has the reason: no second "run failed" message
                    f.write(notice[:200])
            except Exception as pe:  # noqa
                print(f"[ibs] could not post the stale-data notice either: {pe}", file=sys.stderr)
        return 1
    ev = evaluate(state, bars, expected)
    text = render_evening(ev, expected, now, evening_notes(state, expected))
    print(text)
    if dry_run:
        return 0
    apply_changes(state, ev)
    state["last_evening"] = {"bar": b, "text": text}
    return deliver(state, text, "evening", b, webhook, post)


def run_morning(state, now, webhook="", dry_run=False, post=post_discord):
    if not is_session(now.date()):
        print(f"[ibs] {fmt_day(now.date())}: the market is closed; no reminder. Pending orders stay saved for the next session.")
        return 0
    text = render_morning(state, now)
    print(text)
    if dry_run:
        return 0
    return deliver(state, text, "morning", now.date().isoformat(), webhook, post, on_ok=mark_morning_done)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--webhook", default=os.environ.get("DISCORD_WEBHOOK", ""))
    ap.add_argument("--morning", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="evaluate even if this close was already processed")
    ap.add_argument("--now", help="test hook: pretend it is this time (ISO-8601; naive = New York)")
    a = ap.parse_args(argv)
    now = parse_now(a.now) if a.now else now_et()
    state = load_state()
    if a.morning:
        return run_morning(state, now, a.webhook, a.dry_run)
    return run_evening(state, now, a.webhook, a.dry_run, a.force)


if __name__ == "__main__":
    sys.exit(main())
