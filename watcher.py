"""Slot-availability watcher — entry point.

Polls a third-party booking API on schedule and pushes a notification
when a previously-empty target gains availability within a configurable
date window.

Configuration loading order (first source wins):
  1. CONFIG_JSON env variable (raw JSON; used in CI / GitHub Actions).
  2. CONFIG_FILE env variable (path to a local JSON file).
  3. ./.config.local.json next to this file (gitignored, for local dev).
  4. ./config.example.json (placeholder; raises if values are still
     placeholders so a misconfigured run fails loudly).

Logging policy (this code may run in a public repo where Actions logs
are world-readable):
  - Print only generic status: "Target N: <state>".
  - Never print target names, IDs, dates, response bodies, secret
    values, or stack traces.
  - On exceptions, print only the exception class name.

Notification content (warehouse-specific, dates) is sent to the user's
phone via ntfy.sh as request body and headers — those do NOT appear in
the public Actions log.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import signal
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from checker import Target, TargetResult, check_target
from notify import notify_available, notify_error


HERE = Path(__file__).resolve().parent
EXAMPLE_PATH = HERE / "config.example.json"
LOCAL_PATH = HERE / ".config.local.json"
STATE_PATH = HERE / ".state.json"

PLACEHOLDER_MARKER = "__REPLACE_ME__"


def _load_config() -> dict:
    """Load config from env (CI) or local file (dev). Fails loudly on placeholders."""
    raw = os.environ.get("CONFIG_JSON", "").strip()
    if raw:
        cfg = json.loads(raw)
        source = "CONFIG_JSON env"
    elif os.environ.get("CONFIG_FILE", ""):
        cfg = json.loads(Path(os.environ["CONFIG_FILE"]).read_text(encoding="utf-8"))
        source = "CONFIG_FILE"
    elif LOCAL_PATH.exists():
        cfg = json.loads(LOCAL_PATH.read_text(encoding="utf-8"))
        source = "local file"
    elif EXAMPLE_PATH.exists():
        cfg = json.loads(EXAMPLE_PATH.read_text(encoding="utf-8"))
        source = "example"
    else:
        raise RuntimeError("no config source available")

    # Sanity: refuse to run with placeholder values.
    if PLACEHOLDER_MARKER in json.dumps(cfg):
        raise RuntimeError(
            f"config from {source} contains {PLACEHOLDER_MARKER}; "
            f"set CONFIG_JSON, CONFIG_FILE, or {LOCAL_PATH.name}"
        )
    return cfg


def _load_state() -> dict:
    """Returns {open_per_target: {target_id: [iso_dates]}, had_errors: bool}."""
    if not STATE_PATH.exists():
        return {"open_per_target": {}, "had_errors": False}
    try:
        d = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"open_per_target": {}, "had_errors": False}
    if not isinstance(d, dict):
        return {"open_per_target": {}, "had_errors": False}
    raw = d.get("open_per_target") or d.get("open_per_warehouse") or {}
    cleaned: dict[str, list[str]] = {}
    for k, v in raw.items():
        if isinstance(v, list) and all(isinstance(x, str) for x in v):
            cleaned[str(k)] = sorted(set(v))
        else:
            cleaned[str(k)] = []
    return {"open_per_target": cleaned, "had_errors": bool(d.get("had_errors"))}


def _save_state(state: dict) -> None:
    try:
        STATE_PATH.write_text(json.dumps(state), encoding="utf-8")
    except OSError as e:
        print(f"warning: state save failed ({type(e).__name__})")


def _print_result(idx: int, r: TargetResult) -> None:
    """Emit a generic, non-identifying status line for the public log."""
    if r.error:
        print(f"  Target {idx}: error ({r.error})")
        return
    if r.slot_count == 0:
        print(f"  Target {idx}: no slots")
        return
    print(f"  Target {idx}: open ({r.slot_count} slot(s), {len(r.open_dates)} date(s))")


def _do_one_pass(cfg: dict) -> tuple[int, int]:
    targets = [Target.from_dict(d) for d in (cfg.get("targets") or cfg.get("warehouses") or [])]
    window_days = int(cfg["window_days"])
    max_slots = int(cfg.get("max_slots_per_target", cfg.get("max_slots_per_warehouse", 20)))
    tz_name = cfg.get("timezone") or "UTC"
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = _dt.timezone.utc
    service_block = cfg.get("service") or {}
    service_ids = list(service_block.get("service_ids") or [])
    resource_ids = list(service_block.get("resource_ids") or [])
    booking_url_template = service_block.get("booking_url_template", "")

    print(f"[{_dt.datetime.now(tz).strftime('%Y-%m-%d %H:%M:%S')}] checking "
          f"{len(targets)} target(s), window={window_days}d")

    results: list[tuple[int, TargetResult]] = []
    for i, t in enumerate(targets, start=1):
        r = check_target(
            t,
            window_days=window_days,
            max_slots=max_slots,
            resource_ids=resource_ids,
            service_ids=service_ids,
            timezone=tz,
        )
        _print_result(i, r)
        results.append((i, r))

    state = _load_state()
    prev = state["open_per_target"]
    new_state: dict[str, list[str]] = {}
    error_indices: list[int] = []
    new_hits = 0

    macos_enabled = bool(cfg.get("notifications", {}).get("macos", False))
    ntfy_topic_env = (cfg.get("notifications") or {}).get("ntfy_topic_env") or "NTFY_TOPIC"
    ntfy_topic = os.environ.get(ntfy_topic_env, "").strip()
    ntfy_enabled = bool(ntfy_topic)

    for idx, r in results:
        key = r.target.id
        if r.error:
            error_indices.append(idx)
            new_state[key] = prev.get(key, [])  # preserve on transient error
            continue
        cur_set = set(r.open_dates)
        prev_set = set(prev.get(key, []))
        new_state[key] = sorted(cur_set)
        newly = sorted(cur_set - prev_set)
        if not newly:
            continue
        try:
            click_url = booking_url_template.format(id=r.target.id) if booking_url_template else None
            notify_available(
                target_name=r.target.name,
                newly_open=newly,
                all_open=sorted(cur_set),
                enabled_macos=macos_enabled,
                enabled_ntfy=ntfy_enabled,
                click_url=click_url,
            )
            new_hits += 1
            print(f"  -> notified target {idx} ({len(newly)} new date(s))")
        except Exception as e:
            print(f"  -> notify failed target {idx} ({type(e).__name__})")
            new_state[key] = sorted(cur_set - set(newly))  # retry next pass

    has_errors = bool(error_indices)
    notify_errors = bool((cfg.get("notifications") or {}).get("errors", False))
    if has_errors and notify_errors and not state["had_errors"]:
        try:
            notify_error(
                f"{len(error_indices)} target(s) failed: " + ", ".join(f"#{i}" for i in error_indices),
                enabled_macos=macos_enabled,
                enabled_ntfy=ntfy_enabled,
            )
            print("  -> error notification sent")
        except Exception as e:
            print(f"  -> error notify failed ({type(e).__name__})")

    _save_state({"open_per_target": new_state, "had_errors": has_errors})
    return new_hits, len(error_indices)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", action="store_true", help="poll continuously")
    parser.add_argument("--once", action="store_true", help="single pass (default)")
    parser.add_argument("--interval", type=int, default=None,
                        help="poll interval seconds (overrides config)")
    args = parser.parse_args()

    try:
        cfg = _load_config()
    except Exception as e:
        print(f"config load failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 2

    interval = args.interval if args.interval is not None else int(cfg["poll_interval_seconds"])

    if not args.loop:
        hits, errs = _do_one_pass(cfg)
        print(f"done: {hits} new notification(s), {errs} error(s)")
        return 0 if errs == 0 else 1

    stop = {"flag": False}

    def _on_signal(signum, _frame):
        print(f"\nreceived signal {signum}, finishing pass...", flush=True)
        stop["flag"] = True

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    print(f"loop mode: every {interval}s")
    while not stop["flag"]:
        t0 = time.monotonic()
        try:
            _do_one_pass(cfg)
        except Exception as e:
            print(f"pass failed: {type(e).__name__}")
        if stop["flag"]:
            break
        elapsed = time.monotonic() - t0
        wait = max(5, interval - int(elapsed))
        print(f"sleeping {wait}s...", flush=True)
        for _ in range(wait):
            if stop["flag"]:
                break
            time.sleep(1)
    print("stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
