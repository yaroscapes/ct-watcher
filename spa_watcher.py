"""Slot watcher for a second booking platform (per-service time slots).

One pass per run: fetches an anonymous API token, asks for the time
slots of one service on each configured date, and pushes a notification
when a time that wasn't open on the previous pass shows up.

Configuration loading order (first source wins):
  1. SPA_CONFIG_JSON env variable (raw JSON; used in CI / GitHub Actions).
  2. ./.spa.config.local.json next to this file (gitignored, for local runs).

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
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

from notify import notify_available, notify_error


HERE = Path(__file__).resolve().parent
LOCAL_PATH = HERE / ".spa.config.local.json"
STATE_PATH = Path(os.environ.get("SPA_STATE_FILE") or HERE / ".spa_state.json")

TIMEOUT_SECONDS = 20
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
REQUIRED_KEYS = ("base_url", "tenant_id", "property_id", "service_id", "service_code", "dates")


def _load_config() -> dict:
    raw = os.environ.get("SPA_CONFIG_JSON", "").strip()
    if raw:
        cfg = json.loads(raw)
    elif LOCAL_PATH.exists():
        cfg = json.loads(LOCAL_PATH.read_text(encoding="utf-8"))
    else:
        raise RuntimeError(f"set SPA_CONFIG_JSON or create {LOCAL_PATH.name}")
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


def _request(url: str, method: str = "GET", headers: dict | None = None,
             body: bytes | None = None) -> dict:
    h = {"User-Agent": USER_AGENT, "Accept": "application/json, text/plain, */*"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=body, headers=h, method=method)
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get_token(cfg: dict) -> str:
    url = (f"{cfg['base_url']}/wbe-admin-service/generatetoken/v2/tenants/"
           f"{cfg['tenant_id']}/propertyId/{cfg['property_id']}/appName/NA")
    token = _request(url).get("token")
    if not token:
        raise RuntimeError("no_token")
    return token


def _open_times(cfg: dict, token: str, date: str, today: str) -> list[str]:
    """Bookable start times on `date` (e.g. ["2:30 PM", "3:00 PM"]); [] if full."""
    url = (f"{cfg['base_url']}/wbe-spa-service/spa/tenants/{cfg['tenant_id']}"
           f"/propertyId/{cfg['property_id']}/v2/serviceId/{cfg['service_id']}"
           f"/date/{date}/therapistSlotDetails?appName=spa&serviceCode={cfg['service_code']}")
    data = _request(url, method="PUT", body=b"[]", headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "timeZone": cfg.get("api_timezone") or cfg.get("timezone") or "UTC",
        "propertyDTTM": f"{today}T00:00:00",
        "Origin": cfg["base_url"],
        "Referer": f"{cfg['base_url']}/onecart/",
    })
    # An unexpected shape must surface as an error, not as "no slots".
    lanes = data.get("therapist")
    if data.get("success") is not True or not isinstance(lanes, list):
        raise RuntimeError("bad_response")
    times = {s["time"] for lane in lanes for s in (lane.get("slots") or []) if s.get("time")}
    return sorted(times, key=lambda t: _dt.datetime.strptime(t, "%I:%M %p"))


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

    state = _load_state()
    prev_open: dict = state.get("open") or {}
    new_open: dict[str, list[str]] = {}
    error: str | None = None
    hits = 0

    try:
        token = _get_token(cfg)
    except Exception as e:
        error = _error_name(e)
        token = ""

    for i, date in enumerate(dates, start=1):
        if error:
            new_open[date] = prev_open.get(date, [])
            continue
        try:
            times = _open_times(cfg, token, date, today)
        except Exception as e:
            error = _error_name(e)
            new_open[date] = prev_open.get(date, [])
            print(f"  Date {i}: error ({error})")
            continue
        new_open[date] = times
        if not times:
            print(f"  Date {i}: no slots")
            continue
        print(f"  Date {i}: open ({len(times)} time(s))")
        newly = [t for t in times if t not in set(prev_open.get(date, []))]
        if not newly:
            continue
        pretty = _dt.date.fromisoformat(date).strftime("%a %b %-d")
        url = (cfg.get("booking_url") or "").format(date=date) or None
        try:
            fired = notify_available(
                target_name=f"{cfg.get('label', 'Booking')} {pretty}",
                newly_open=newly,
                all_open=times,
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
            notify_error(f"Spa watcher failing for {streak} checks in a row ({error}). "
                         f"It may not be catching openings.",
                         enabled_macos=macos_enabled, enabled_ntfy=ntfy_enabled)
            alerted = True
            print("  -> error notification sent")

    _save_state({"open": new_open, "fail_streak": streak, "fail_alerted": alerted})
    print(f"done: {hits} new notification(s), {'1 error' if error else '0 errors'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
