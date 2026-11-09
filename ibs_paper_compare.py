#!/usr/bin/env python3
"""Forward-only paper comparison for the two IBS exposure policies."""
import argparse
import datetime as dt
import json
import os
from pathlib import Path

import ibs_callout_bot as ibs

INITIAL = 1500.0
FRICTION = .0005
STATE = Path(__file__).resolve().parent / "paper_ibs" / "comparison.json"
POLICIES = {"original": None, "cap85": .85}


def new_account():
    return dict(cash=INITIAL, unsettled=[], positions={}, orders=dict(buys=[], sells=[]),
                peak=INITIAL, max_drawdown=0.0, fills=0)


def mark(positions, prices, field):
    return sum(pos["shares"] * prices[symbol][field] for symbol, pos in positions.items())


def step(account, cap, date, frames, prior_equity):
    day = date.isoformat()
    rows = {s: frames[s].iloc[-1] for s in ibs.TICKERS}
    cash = account["cash"]
    unsettled = account["unsettled"]
    positions = account["positions"]
    orders = account["orders"]
    fills = []
    for transfer in unsettled[:]:
        if transfer["date"] <= day:
            cash += transfer["amount"]
            unsettled.remove(transfer)
    for symbol in orders["sells"]:
        position = positions.pop(symbol)
        price = float(rows[symbol]["open"]) * (1 - FRICTION)
        proceeds = position["shares"] * price
        unsettled.append(dict(date=ibs.next_session(date).isoformat(), amount=proceeds))
        fills.append(dict(side="sell", ticker=symbol, price=price, amount=proceeds))
    budget = .25 * prior_equity
    if cap is not None and orders["buys"]:
        invested = mark(positions, rows, "open")
        open_equity = cash + sum(r["amount"] for r in unsettled) + invested
        headroom = max(0.0, cap * open_equity - invested)
        allowed = headroom * (1 + FRICTION) / (1 + cap * FRICTION)
        budget = min(budget, allowed / len(orders["buys"]))
    skips = []
    for symbol in orders["buys"]:
        spend = min(cash, budget)
        if spend < 1:
            skips.append(symbol)
            continue
        price = float(rows[symbol]["open"]) * (1 + FRICTION)
        positions[symbol] = dict(entry_date=ibs.prev_session(date).isoformat(), shares=spend/price)
        cash -= spend
        fills.append(dict(side="buy", ticker=symbol, price=price, amount=spend))
    invested = mark(positions, rows, "close")
    equity = cash + sum(r["amount"] for r in unsettled) + invested
    account["cash"] = cash
    account["peak"] = max(account["peak"], equity)
    account["max_drawdown"] = min(account["max_drawdown"], equity/account["peak"] - 1)
    account["fills"] += len(fills)
    signals = ibs.evaluate(account, frames, date)
    account["orders"] = {side: [order["sym"] for order in signals[side]] for side in ("buys", "sells")}
    return dict(equity=equity, exposure=invested/equity, fills=fills, skipped=skips,
                next_open=account["orders"], max_drawdown=account["max_drawdown"])


def advance(state, frames, latest):
    previous = state["history"][-1] if state["history"] else None
    day = ibs.next_session(dt.date.fromisoformat(previous["date"])) if previous else latest
    while day <= latest:
        yesterday = ibs.prev_session(day)
        # IBS hold counts also depend on intermediate sessions. Check the
        # recent calendar before replaying so a missing bar cannot be skipped.
        required = [day, yesterday]
        for _ in range(10):
            required.append(ibs.prev_session(required[-1]))
        daily = {s: ibs.trim_to(frame, day) for s, frame in frames.items()}
        for symbol, frame in daily.items():
            if not set(required).issubset(frame.index.date):
                raise RuntimeError(f"{symbol} has missing bars near {day}")
        record = {"date": day.isoformat()}
        for name, cap in POLICIES.items():
            prior_equity = previous[name]["equity"] if previous else INITIAL
            record[name] = step(state["accounts"][name], cap, day, daily, prior_equity)
        state["history"].append(record)
        previous = record
        day = ibs.next_session(day)


def summary(state):
    first, latest = state["history"][0], state["history"][-1]
    lines = [
        "# IBS paper comparison",
        "",
        f"Started {first['date']} · Latest close {latest['date']} · $1,500 simulated per policy",
        "",
        "| Policy | Paper equity | Return | Max drawdown | Exposure | Simulated fills |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for name, label in (("original", "Original"), ("cap85", "85% entry cap")):
        row = latest[name]
        lines.append(f"| {label} | ${row['equity']:,.2f} | {row['equity']/INITIAL-1:+.2%} | "
                     f"{row['max_drawdown']:.2%} | {row['exposure']:.2%} | {state['accounts'][name]['fills']} |")
    difference = latest["cap85"]["equity"] - latest["original"]["equity"]
    lines += ["", f"85% cap minus original: {difference:+.2f} USD.",
              "", "Modeled opening fills, 5 bp per side, price-only P&L. Dividend cash flows are excluded."]
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    state = json.loads(STATE.read_text()) if STATE.exists() else dict(
        accounts={name: new_account() for name in POLICIES}, history=[]
    )
    latest = ibs.expected_last_session(ibs.now_et())
    if not state["history"] or state["history"][-1]["date"] < latest.isoformat():
        advance(state, ibs.load_bars(latest), latest)
        if not args.dry_run:
            STATE.parent.mkdir(parents=True, exist_ok=True)
            temp = STATE.with_suffix(".tmp")
            temp.write_text(json.dumps(state, indent=2) + "\n")
            temp.replace(STATE)
    report = summary(state)
    print(report)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as stream:
            stream.write(report)


if __name__ == "__main__":
    main()
