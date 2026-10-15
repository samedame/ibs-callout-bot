#!/usr/bin/env python3
"""Post ONE Discord message saying a workflow run failed. Best effort: it never raises and never blocks the run.

A scheduled bot that crashes is otherwise invisible in Discord: no alert arrives, and "no alert" looks exactly like
"no signal today". The workflows call this from a final `if: failure()` step.

Usage:   python notify_failure.py "IBS callout bot" ["extra sentence"]
Env:     DISCORD_WEBHOOK or DISCORD_WEBHOOK_URL   RUN_URL (link to the failed run)   GITHUB_EVENT_NAME
Skipped when the bot already told Discord why it failed (it leaves a `.notified` file next to this script).
"""
import json
import os
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
MARKER = os.path.join(HERE, ".notified")
UA = {"User-Agent": "Mozilla/5.0 (compatible; bot-failure-notifier/1.0)"}


def message(name, extra="", run_url="", event=""):
    how = f" (started by {event})" if event else ""
    link = f" Details: {run_url}" if run_url else ""
    tail = f" {extra}" if extra else ""
    return (f"⚠️ **{name}: this run FAILED**{how}. Any alert you expected from it did NOT arrive, so don't read "
            f"silence as \"no signal\".{link}{tail}")


def post(webhook, text, tries=3, sleep=time.sleep, opener=urllib.request.urlopen):
    data = json.dumps({"content": text[:1900], "allowed_mentions": {"parse": []}}).encode()
    for i in range(tries):
        try:
            req = urllib.request.Request(webhook, data=data, headers={"Content-Type": "application/json", **UA})
            with opener(req, timeout=15) as r:
                if 200 <= r.status < 300:
                    return True
        except Exception as e:  # noqa
            print(f"[notify_failure] attempt {i + 1} failed: {e}", file=sys.stderr)
        if i < tries - 1:
            sleep(2 + 3 * i)
    return False


def main(argv=None, env=None):
    argv = sys.argv[1:] if argv is None else argv
    env = os.environ if env is None else env
    name = argv[0] if argv else "A bot"
    extra = argv[1] if len(argv) > 1 else ""
    if os.path.exists(MARKER):
        print("[notify_failure] the bot already reported this failure in Discord; staying quiet.")
        return 0
    webhook = env.get("DISCORD_WEBHOOK") or env.get("DISCORD_WEBHOOK_URL") or ""
    text = message(name, extra, env.get("RUN_URL", ""), env.get("GITHUB_EVENT_NAME", ""))
    if not webhook:
        print(f"[notify_failure] no webhook configured; would have said: {text}", file=sys.stderr)
        return 0
    return 0 if post(webhook, text) else 0       # always 0: this step must not hide the original failure


if __name__ == "__main__":
    sys.exit(main())
