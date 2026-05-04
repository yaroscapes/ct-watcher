"""Notifications: macOS native + optional ntfy.sh push.

Two flavors:
  - notify_available(...) — high priority, sent when new dates open up.
  - notify_error(summary) — default priority, deduped by caller.

macOS notifications use osascript (no extra deps). ntfy.sh is optional
and only fires if NTFY_TOPIC is set in the environment.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import urllib.error
import urllib.request


NTFY_BASE = "https://ntfy.sh"
TIMEOUT_SECONDS = 10


def _macos_notify(title: str, body: str) -> None:
    # AppleScript display notification — quote-safe via shlex.
    script = (
        f'display notification {shlex.quote(body)} '
        f'with title {shlex.quote(title)} sound name "Glass"'
    )
    try:
        subprocess.run(
            ["osascript", "-e", script],
            check=False,
            timeout=5,
            capture_output=True,
        )
    except (subprocess.SubprocessError, OSError):
        pass


def _ntfy_post(title: str, body: str, priority: str, tags: str, click_url: str | None) -> None:
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        return
    if not all(c.isalnum() or c in "-_" for c in topic):
        # Refuse to send to a weird topic.
        return

    headers = {
        "Title": title,
        "Priority": priority,
        "Tags": tags,
        "Content-Type": "text/plain; charset=utf-8",
    }
    if click_url:
        headers["Click"] = click_url

    req = urllib.request.Request(
        f"{NTFY_BASE}/{topic}",
        data=body.encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            if resp.status >= 300:
                print(f"  ntfy http {resp.status}")
    except urllib.error.URLError as e:
        print(f"  ntfy network error: {type(e).__name__}")


def notify_available(
    target_name: str,
    newly_open: list[str],
    all_open: list[str],
    enabled_macos: bool = True,
    enabled_ntfy: bool = True,
    click_url: str | None = None,
) -> list[str]:
    """Send a high-priority push when new dates open up.

    Returns a list of channel names that actually fired (e.g.
    ["macos", "ntfy"]) — empty list if nothing was attempted.

    The body contains target name and dates — these go to the user's
    phone via ntfy.sh (private). They are NOT printed to logs by the
    caller (watcher.py uses generic "target N" status only).
    """
    title = f"{target_name}: slot open!"
    lines = [f"New: {', '.join(newly_open)}"]
    if set(all_open) != set(newly_open):
        lines.append(f"All in window: {', '.join(all_open)}")
    if click_url:
        lines.append(f"Book: {click_url}")
    body = "\n".join(lines)

    fired: list[str] = []
    if enabled_macos:
        _macos_notify(title, body)
        fired.append("macos")
    if enabled_ntfy:
        _ntfy_post(title, body, priority="high", tags="bell", click_url=click_url)
        fired.append("ntfy")
    return fired


def notify_error(
    summary: str,
    enabled_macos: bool = True,
    enabled_ntfy: bool = True,
) -> None:
    title = "Watcher: errors"
    if enabled_macos:
        _macos_notify(title, summary)
    if enabled_ntfy:
        _ntfy_post(title, summary, priority="default", tags="warning", click_url=None)
