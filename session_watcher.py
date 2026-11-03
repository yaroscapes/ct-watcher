"""Slot watcher for a third booking platform (capacity-limited sessions).

One pass per run: lists the session occurrences of one session type on
each configured date and pushes a notification when a start time with
free spots shows up that wasn't open on the previous pass. No auth.

Configuration loading order (first source wins):
  1. SESSION_CONFIG_JSON env variable (raw JSON; used in CI / GitHub Actions).
  2. ./.session.config.local.json next to this file (gitignored, for local runs).

Same logging policy as watcher.py: only generic "Date N: <state>" lines,
never names, IDs, dates, or response bodies. Notification content goes
to ntfy.sh / macOS only.

API errors don't fail the process (exit 0) so CI still saves state; after
`notifications.error_after` consecutive failed passes, one error push is
sent so a silently broken watcher doesn't look like "no slots".
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

from notify import notify_available, notify_error


HERE = Path(__file__).resolve().parent
LOCAL_PATH = HERE / ".session.config.local.json"
STATE_PATH = Path(os.environ.get("SESSION_STATE_FILE") or HERE / ".session_state.json")

TIMEOUT_SECONDS = 20
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
REQUIRED_KEYS = ("api_base", "site_id", "session_type_id", "dates")


def _load_config() -> dict:
    raw = os.environ.get("SESSION_CONFIG_JSON", "").strip()
    if raw:
        cfg = json.loads(raw)
    elif LOCAL_PATH.exists():
        cfg = json.loads(LOCAL_PATH.read_text(encoding="utf-8"))
    else:
        raise RuntimeError(f"set SESSION_CONFIG_JSON or create {LOCAL_PATH.name}")
    missing = [k for k in REQUIRED_KEYS if not cfg.get(k)]
    if missing:
        raise RuntimeError(f"config missing: {', '.join(missing)}")
    return cfg


def _load_state() -> dict:
    try:
        d = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(d, dict):
            return d
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _save_state(state: dict) -> None:
    try:
        STATE_PATH.write_text(json.dumps(state), encoding="utf-8")
    except OSError as e:
        print(f"warning: state save failed ({type(e).__name__})")


def _request(url: str) -> dict:
    h = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _open_times(cfg: dict, date: str) -> dict[str, int]:
    """{"09:15": spots_left, ...} for sessions on `date` with enough spots; {} if full."""
    query = urllib.parse.urlencode({"date_from": date, "date_to": date})
    url = (f"{cfg['api_base']}/item-availability/sessions/"
           f"{cfg['site_id']}/{cfg['session_type_id']}?{query}")
    data = _request(url)
    # A fully booked day still lists its sessions (remaining_capacity 0), so
    # an empty list means a wrong ID or a closed day: surface it, don't hide it.
    sessions = data.get("data") if isinstance(data, dict) else None
    if not isinstance(sessions, list):
        raise RuntimeError("bad_response")
    if not sessions:
        raise RuntimeError("no_sessions")
    min_spots = int(cfg.get("min_spots", 1))
    times: dict[str, int] = {}
    for s in sessions:
        # start_time is site wall-clock time, e.g. "2026-11-08T09:15:00-06:00";
        # the shop displays the HH:MM as-is, whatever the offset says.
        start = s["start_time"]
        left = int(s.get("remaining_capacity") or 0)
        if start[:10] == date and s.get("is_valid") is True and left >= min_spots:
            times[start[11:16]] = left
    return dict(sorted(times.items()))


def _pretty_time(hhmm: str) -> str:
    return _dt.datetime.strptime(hhmm, "%H:%M").strftime("%-I:%M %p")


def _error_name(e: Exception) -> str:
    if isinstance(e, urllib.error.HTTPError):
        return f"http_{e.code}"
    if isinstance(e, RuntimeError) and str(e):
        return str(e)
    return type(e).__name__


def main() -> int:
    try:
        cfg = _load_config()
    except Exception as e:
        print(f"config load failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 2

    tz = ZoneInfo(cfg.get("timezone") or "UTC")
    now = _dt.datetime.now(tz)
    today = now.date().isoformat()
    dates = [d for d in cfg["dates"] if d >= today]
    print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] checking {len(dates)} date(s)")
    if not dates:
        print("done: all dates are in the past")
        return 0

    notif = cfg.get("notifications") or {}
    macos_enabled = bool(notif.get("macos", False))
    ntfy_enabled = bool(os.environ.get("NTFY_TOPIC", "").strip())
    error_after = int(notif.get("error_after", 3))
    label = cfg.get("label", "Booking")

    state = _load_state()
    prev_open: dict = state.get("open") or {}
    new_open: dict[str, list[str]] = {}
    error: str | None = None
    hits = 0

    for i, date in enumerate(dates, start=1):
        try:
            open_now = _open_times(cfg, date)
        except Exception as e:
            error = _error_name(e)
            new_open[date] = prev_open.get(date, [])
            print(f"  Date {i}: error ({error})")
            continue
        times = list(open_now)
        new_open[date] = times
        if not times:
            print(f"  Date {i}: no slots")
            continue
        print(f"  Date {i}: open ({len(times)} time(s))")
        newly = [t for t in times if t not in set(prev_open.get(date, []))]
        if not newly:
            continue
        show = {t: f"{_pretty_time(t)} ({n} left)" for t, n in open_now.items()}
        pretty = _dt.date.fromisoformat(date).strftime("%a %b %-d")
        url = (cfg.get("booking_url") or "").format(date=date) or None
        try:
            fired = notify_available(
                target_name=f"{label} {pretty}",
                newly_open=[show[t] for t in newly],
                all_open=[show[t] for t in times],
                enabled_macos=macos_enabled,
                enabled_ntfy=ntfy_enabled,
                click_url=url,
            )
            hits += 1
            print(f"  -> date {i}: {len(newly)} new time(s) "
                  f"[{','.join(fired) or 'no channels enabled!'}]")
        except Exception as e:
            print(f"  -> notify failed date {i} ({type(e).__name__})")
            new_open[date] = [t for t in times if t not in newly]  # retry next pass

    streak = int(state.get("fail_streak", 0)) + 1 if error else 0
    alerted = bool(state.get("fail_alerted")) and bool(error)
    if error:
        print(f"  error: {error} ({streak} pass(es) in a row)")
        if streak >= error_after and not alerted:
            notify_error(f"{label} watcher failing for {streak} checks in a row ({error}). "
                         f"It may not be catching openings.",
                         enabled_macos=macos_enabled, enabled_ntfy=ntfy_enabled)
            alerted = True
            print("  -> error notification sent")

    _save_state({"open": new_open, "fail_streak": streak, "fail_alerted": alerted})
    print(f"done: {hits} new notification(s), {'1 error' if error else '0 errors'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
