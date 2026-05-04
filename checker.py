"""HTTP availability checker for a third-party Waitwhile booking flow.

Endpoint shape (no auth, plain HTTPS GET):

  GET https://api.waitwhile.com/v2/public/visits/<locationId>/first-available-slots
      ?fromDate=YYYY-MM-DDTHH:MM
      &toDate=YYYY-MM-DDTHH:MM
      &maxNumSlots=<n>
      &resourceIds=<id>[&resourceIds=...]
      &serviceIds=<id>[&serviceIds=...]

Returns a JSON array of slot objects (empty `[]` when nothing is bookable).

This module is fully generic: it knows nothing about the merchant, the
type of service, or the geographic region. Targets are opaque
identifiers passed in via configuration.
"""
from __future__ import annotations

import datetime as _dt
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field


WAITWHILE_BASE = "https://api.waitwhile.com/v2/public"
DEFAULT_TIMEOUT = 15
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/147.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class Target:
    """One opaque booking location to monitor.

    `id` is the Waitwhile location identifier — opaque to the watcher.
    `name` is a display string used only in push notifications (which
    are sent to the user's phone, not to the public CI log).
    """
    id: str
    name: str

    @classmethod
    def from_dict(cls, d: dict) -> "Target":
        return cls(id=str(d["id"]), name=str(d["name"]))


@dataclass
class TargetResult:
    target: Target
    open_dates: list[str] = field(default_factory=list)   # ISO dates with bookable slots
    slot_count: int = 0
    error: str | None = None


def _http_get_json(url: str, timeout: int = DEFAULT_TIMEOUT) -> object:
    """Plain HTTPS GET, returns parsed JSON or raises RuntimeError with class name."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Accept-Language": "en-CA,en-US;q=0.9,en;q=0.8",
            "Origin": "https://waitwhile.com",
            "Referer": "https://waitwhile.com/",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                raise RuntimeError(f"http_{resp.status}")
            raw = resp.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"http_{e.code}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"network_{type(e.reason).__name__}") from None

    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError("non_json_response") from None


def _build_url(
    location: str,
    *,
    from_date: str,
    to_date: str,
    max_slots: int,
    resource_ids: list[str],
    service_ids: list[str],
) -> str:
    qs = urllib.parse.urlencode(
        {
            "fromDate": from_date,
            "toDate": to_date,
            "maxNumSlots": max_slots,
            "resourceIds": resource_ids,
            "serviceIds": service_ids,
        },
        doseq=True,
    )
    return f"{WAITWHILE_BASE}/visits/{location}/first-available-slots?{qs}"


def check_target(
    target: Target,
    *,
    window_days: int,
    max_slots: int,
    resource_ids: list[str],
    service_ids: list[str],
    timezone: _dt.tzinfo | None = None,
) -> TargetResult:
    """Hit the slots endpoint for one target. Returns dates with real slots."""
    if timezone is None:
        timezone = _dt.timezone.utc
    now = _dt.datetime.now(tz=timezone)
    from_str = now.strftime("%Y-%m-%dT%H:%M")
    to_str = (now + _dt.timedelta(days=window_days)).strftime("%Y-%m-%dT%H:%M")

    url = _build_url(
        target.id,
        from_date=from_str,
        to_date=to_str,
        max_slots=max_slots,
        resource_ids=resource_ids,
        service_ids=service_ids,
    )

    try:
        data = _http_get_json(url)
    except Exception as e:
        msg = str(e) if isinstance(e, RuntimeError) else type(e).__name__
        return TargetResult(target, error=msg)

    if not isinstance(data, list):
        return TargetResult(target, error="unexpected_shape")

    open_dates = sorted(
        {_extract_iso_date(s) for s in data if isinstance(s, dict)} - {""}
    )
    return TargetResult(
        target=target,
        open_dates=[d for d in open_dates if d],
        slot_count=len(data),
    )


def _extract_iso_date(slot: dict) -> str:
    """Pull the ISO date out of a slot object (best-effort)."""
    for k in ("startTime", "fromTime", "fromDate", "from", "start", "date"):
        v = slot.get(k)
        if isinstance(v, str) and len(v) >= 10:
            return v[:10]
    return ""
