"""run_guard.py - drop-in lateness + duplicate guard for GitHub-Actions bots that must act at a
precise America/New_York time. Standard library only (Python 3.9+).

Two ways to integrate. (A) No code changes - wrap the command in the workflow:

    python run_guard.py --job ibs-preopen --target 09:00 --usable-until 09:29 -- \
        python ibs_callout_bot.py --webhook "$DISCORD_WEBHOOK" --morning

(B) Two lines at the bottom of the bot's entry script:

    from run_guard import GuardConfig, run_guarded
    run_guarded(GuardConfig(job="ibs-preopen", target_et="08:30", usable_until_et="09:29"), main)

What it does, in order:
  1. Manual runs (workflow_dispatch from the UI, trigger "manual"/"test") bypass everything.
  2. If this job already completed for today's ET date -> exit silently (DUPLICATE). This is what
     stops the GitHub backup cron from double-firing after the external scheduler already ran.
  3. If the run started long before the intended time -> exit silently (TOO_EARLY). This is the
     wrong-DST-season copy of the backup cron.
  4. Lateness is measured against the INTENDED time (SCHEDULED_FOR_ET from the scheduler, else
     target_et), not against when the runner happened to boot:
        <= on_time_min          ON_TIME       run normally
        before usable_until_et  LATE_USABLE   post a one-line "started N min late" note, then run
        after usable_until_et   TOO_LATE      post ONE "missed window" notice and stop
                                              (on_too_late="run_flagged" runs anyway, flagged)
  5. After main() returns without raising, a marker is written so later triggers see DUPLICATE.
     If main() raises, no marker is written, so the backup cron can retry.

The marker file (default .run_guard.json) MUST be committed back by the workflow, the same way
the bot's state.json is - see workflows/example-open-bot.yml.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Callable, Optional
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
FORCE_TRIGGERS = {"manual", "test"}
KEEP_DAYS = 14


class Status(str, Enum):
    ON_TIME = "on_time"
    LATE_USABLE = "late_usable"
    TOO_LATE = "too_late"
    TOO_EARLY = "too_early"
    DUPLICATE = "duplicate"
    FORCED = "forced"


@dataclass(frozen=True)
class GuardConfig:
    job: str                      # stable id, e.g. "ibs-preopen" (matches jobs.json id)
    target_et: str                # "HH:MM" the job is supposed to fire
    usable_until_et: str          # "HH:MM" after which acting is pointless/unsafe (e.g. the open)
    on_time_min: int = 10         # <= this late counts as on time
    early_tolerance_min: int = 20  # earlier than this before target = wrong-season cron copy
    on_too_late: str = "notice"   # "notice" (post + stop) | "run_flagged" (post + run anyway)
    state_path: str = ".run_guard.json"


@dataclass
class Decision:
    status: Status
    should_run: bool
    late_min: float
    scheduled_for: datetime
    trigger: str
    notice: str = ""   # post this to Discord before doing anything else ("" = stay quiet)
    note: str = ""     # short prefix the bot may add to its own message (also in $RUN_GUARD_LATE_NOTE)


def _hm(s: str) -> tuple[int, int]:
    h, m = s.split(":")
    return int(h), int(m)


def _at(day: datetime, hhmm: str) -> datetime:
    h, m = _hm(hhmm)
    return day.astimezone(ET).replace(hour=h, minute=m, second=0, microsecond=0)


def discord_post(text: str, webhook_url: Optional[str] = None, retries: int = 3) -> bool:
    """Best-effort Discord webhook post. Never raises. Sends a custom User-Agent because Discord's
    edge rejects the default Python-urllib one."""
    url = webhook_url or os.environ.get("DISCORD_WEBHOOK_URL") or os.environ.get("DISCORD_WEBHOOK", "")
    if not url:
        print(f"[run_guard] (no DISCORD_WEBHOOK_URL / DISCORD_WEBHOOK set) {text}", file=sys.stderr)
        return False
    body = json.dumps({"content": text[:1900]}).encode()
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url, data=body, method="POST",
                headers={"Content-Type": "application/json", "User-Agent": "run-guard/1.0"},
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                if 200 <= r.status < 300:
                    return True
        except (urllib.error.URLError, OSError) as e:
            print(f"[run_guard] discord attempt {attempt + 1} failed: {e}", file=sys.stderr)
        if attempt < retries - 1:
            time.sleep(1 + 2 * attempt)
    return False


class RunGuard:
    def __init__(self, cfg: GuardConfig):
        if cfg.on_too_late not in ("notice", "run_flagged"):
            raise ValueError('on_too_late must be "notice" or "run_flagged"')
        if ":" in cfg.job:
            raise ValueError("job id must not contain ':'")
        self.cfg = cfg

    # ---- state -------------------------------------------------------------------------
    def _load(self) -> dict:
        try:
            with open(self.cfg.state_path) as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except (json.JSONDecodeError, OSError) as e:
            print(f"[run_guard] state file unreadable ({e}); treating as empty", file=sys.stderr)
            return {}

    def _save(self, state: dict, today: datetime) -> None:
        cutoff = (today - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
        state = {k: v for k, v in state.items() if k.split(":")[1] >= cutoff}
        tmp = self.cfg.state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, indent=1, sort_keys=True)
        os.replace(tmp, self.cfg.state_path)

    def _key(self, scheduled_for: datetime, suffix: str = "") -> str:
        return f"{self.cfg.job}:{scheduled_for:%Y-%m-%d}{suffix}"

    # ---- decision ----------------------------------------------------------------------
    def _scheduled_for(self, now: datetime, env) -> datetime:
        raw = (env.get("SCHEDULED_FOR_ET") or "").strip()
        if raw:
            try:
                dt = datetime.fromisoformat(raw)
                dt = dt.replace(tzinfo=ET) if dt.tzinfo is None else dt.astimezone(ET)
                if abs((now - dt).total_seconds()) < 20 * 3600:
                    return dt
            except ValueError:
                pass
            print(f"[run_guard] ignoring bad SCHEDULED_FOR_ET={raw!r}", file=sys.stderr)
        return _at(now, self.cfg.target_et)

    def evaluate(self, now: Optional[datetime] = None, env=None) -> Decision:
        env = os.environ if env is None else env
        now = (now or datetime.now(ET)).astimezone(ET)
        cfg = self.cfg
        trigger = (env.get("RUN_TRIGGER") or "unknown").strip() or "unknown"
        sched = self._scheduled_for(now, env)
        late = (now - sched).total_seconds() / 60.0
        base = dict(late_min=late, scheduled_for=sched, trigger=trigger)

        if env.get("RUN_GUARD_FORCE") or trigger in FORCE_TRIGGERS:
            return Decision(Status.FORCED, True, **base)

        state = self._load()
        done = state.get(self._key(sched))
        if isinstance(done, dict) and done.get("done_at"):
            return Decision(Status.DUPLICATE, False, **base)

        if late < -cfg.early_tolerance_min:
            return Decision(Status.TOO_EARLY, False, **base)

        backup = (
            f"ℹ️ {cfg.job}: started by the GitHub backup cron - the external scheduler did not "
            f"complete this run today. Check the open-scheduler Worker."
            if trigger == "schedule" else ""
        )
        usable_until = _at(sched, cfg.usable_until_et)
        s_hm, n_hm, u_hm = f"{sched:%H:%M}", f"{now:%H:%M}", cfg.usable_until_et

        if late <= cfg.on_time_min:
            return Decision(Status.ON_TIME, True, notice=backup, **base)

        if now < usable_until:
            msg = (f"⏱ {cfg.job}: started {late:.0f} min late (scheduled {s_hm} ET, now {n_hm} ET). "
                   f"Still inside the usable window (until {u_hm} ET) - running.")
            note = f"⏱ {late:.0f} min late (scheduled {s_hm} ET)"
            return Decision(Status.LATE_USABLE, True, notice=(backup + "\n" + msg).strip(), note=note, **base)

        # too late
        already = bool(state.get(self._key(sched, ":notice")))
        if cfg.on_too_late == "run_flagged":
            msg = (f"⚠️ {cfg.job}: started {late:.0f} min late (scheduled {s_hm} ET, now {n_hm} ET), "
                   f"past the usable window ({u_hm} ET). Running anyway - treat anything time-sensitive as stale.")
            note = f"⚠️ {late:.0f} min late, past {u_hm} ET"
            return Decision(Status.TOO_LATE, True, notice="" if already else msg, note=note, **base)
        msg = (f"⚠️ {cfg.job} missed its window: scheduled {s_hm} ET, started {n_hm} ET "
               f"({late:.0f} min late; usable until {u_hm} ET; trigger: {trigger}). Skipped - nothing "
               f"was sent for this run. If it still matters, act manually from your last alert.")
        return Decision(Status.TOO_LATE, False, notice="" if already else msg, **base)

    # ---- markers -----------------------------------------------------------------------
    def mark_done(self, d: Decision) -> None:
        if d.status == Status.FORCED:
            return  # manual/test runs must not suppress the real scheduled run
        state = self._load()
        state[self._key(d.scheduled_for)] = {
            "done_at": datetime.now(ET).isoformat(timespec="seconds"),
            "trigger": d.trigger, "late_min": round(d.late_min, 1), "status": d.status.value,
        }
        self._save(state, d.scheduled_for)

    def mark_notice(self, d: Decision) -> None:
        state = self._load()
        state[self._key(d.scheduled_for, ":notice")] = datetime.now(ET).isoformat(timespec="seconds")
        self._save(state, d.scheduled_for)


def run_guarded(
    cfg: GuardConfig,
    main: Callable[[], object],
    post: Optional[Callable[[str], object]] = None,
    now: Optional[datetime] = None,
    env=None,
):
    """Wrap a bot's main(). Returns main()'s result, or None if the guard decided not to run."""
    guard = RunGuard(cfg)
    d = guard.evaluate(now=now, env=env)
    print(f"[run_guard] job={cfg.job} status={d.status.value} late={d.late_min:.1f}min "
          f"scheduled={d.scheduled_for:%H:%M} trigger={d.trigger} run={d.should_run}")
    post = post or discord_post
    if d.notice:
        post(d.notice)
        if d.status == Status.TOO_LATE:
            guard.mark_notice(d)
    if not d.should_run:
        return None
    os.environ["RUN_GUARD_LATE_NOTE"] = d.note
    result = main()
    guard.mark_done(d)
    return result


def _cli(argv=None) -> int:
    import argparse
    import subprocess

    ap = argparse.ArgumentParser(
        prog="run_guard.py",
        description="Run a command only if it is on time / not a duplicate. Everything after `--` is the command.",
    )
    ap.add_argument("--job", required=True)
    ap.add_argument("--target", required=True, help="HH:MM ET the job is supposed to fire")
    ap.add_argument("--usable-until", required=True, help="HH:MM ET after which running is pointless")
    ap.add_argument("--on-time-min", type=int, default=10)
    ap.add_argument("--on-too-late", choices=("notice", "run_flagged"), default="notice")
    ap.add_argument("--state-path", default=".run_guard.json")
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    args = ap.parse_args(argv)
    cmd = args.cmd[1:] if args.cmd[:1] == ["--"] else args.cmd
    if not cmd:
        ap.error("missing command: put it after `--`")

    cfg = GuardConfig(job=args.job, target_et=args.target, usable_until_et=args.usable_until,
                      on_time_min=args.on_time_min, on_too_late=args.on_too_late, state_path=args.state_path)

    def main():
        rc = subprocess.run(cmd).returncode
        if rc != 0:
            raise SystemExit(rc)  # propagate failure; no marker is written, so the backup can retry

    run_guarded(cfg, main)
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
