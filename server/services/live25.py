"""Read-only client for Stony Brook's 25Live (guest-visible data).

Everything that knows about 25Live's URLs and JSON shapes lives here, so the
routers never touch raw 25Live responses. Responses are cached in memory so
our API doesn't send a request to 25Live for every page load.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import time
from typing import Any
from urllib.parse import urlencode

import httpx

BASE_URL = "https://25live.collegenet.com/25live/data/stonybrook/run"
CACHE_TTL_SECONDS = 15 * 60
TIMEOUT_SECONDS = 20

# Confirmed working for SBU guest access.
SPACES_PATH = "/spaces.json"
SPACES_PARAMS: dict[str, Any] = {"scope": "minimal"}

EVENT_STATES = {0: "Draft", 1: "Tentative", 2: "Confirmed", 3: "Sealed",
                98: "Deleted", 99: "Cancelled"}


class Live25Error(Exception):
    """25Live was unreachable or returned something we couldn't use."""


# ---------------------------------------------------------------- caching

_cache: dict[str, tuple[float, Any]] = {}
_locks: dict[str, asyncio.Lock] = {}


def build_url(path: str, params: dict[str, Any]) -> str:
    """Build the URL the same way the 25Live web app does: spaces as '+',
    colons left as-is (e.g. start_dt=2026-10-08T00:00:00)."""
    return f"{BASE_URL}{path}?{urlencode(params, safe=':')}" if params else BASE_URL + path


async def _get_json(path: str, params: dict[str, Any]) -> Any:
    url = key = build_url(path, params)
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_TTL_SECONDS:
        return hit[1]

    # One lock per URL so concurrent requests share a single fetch.
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        hit = _cache.get(key)
        if hit and time.monotonic() - hit[0] < CACHE_TTL_SECONDS:
            return hit[1]
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
                r = await client.get(url, headers={"Accept": "application/json"})
                r.raise_for_status()
                data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            if hit:  # 25Live is down: serve stale data rather than fail
                return hit[1]
            raise Live25Error(f"{e} (URL: {url})") from e
        _cache[key] = (time.monotonic(), data)
        return data


# ---------------------------------------------------------------- rooms

def parse_spaces(data: Any) -> list[dict]:
    """Pull rooms out of a 25Live room-list response.

    Walks the whole JSON looking for room records, so it tolerates both the
    classic shape ({"space_id", "space_name", "max_capacity", ...}) and the
    25Live Pro list shape ({"itemId", "itemName", "itemTypeId": 4, ...}).
    Adjust here once you've seen SBU's real response.
    """
    rooms: dict[str, dict] = {}

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            if "space_id" in node:
                # formal_name is readable ("Academic Mall Fountain Area");
                # space_name is the ALL-CAPS short code.
                rid = node["space_id"]
                name = node.get("formal_name") or node.get("space_name")
                cap = node.get("max_capacity") or node.get("capacity")
                building_id = node.get("building_id")
                building = node.get("building_name")
                rtype = node.get("space_type") or node.get("feature_name")
            elif node.get("itemTypeId") == 4 and "itemId" in node:
                rid, name = node["itemId"], node.get("itemName")
                cap = node.get("max_capacity") or node.get("capacity")
                building_id, building = None, node.get("building_name")
                rtype = None
            else:
                rid = None
            if rid is not None and name:
                rooms[str(rid)] = {
                    "id": str(rid),
                    "name": str(name).strip(),
                    "short_name": str(node.get("space_name") or name).strip(),
                    "capacity": _to_int(cap),
                    "building_id": str(building_id) if building_id is not None else None,
                    "building": _clean_building(building),
                    "room_type": str(rtype) if rtype else None,
                }
            for v in node.values():
                visit(v)
        elif isinstance(node, list):
            for v in node:
                visit(v)

    visit(data)
    return sorted(rooms.values(), key=lambda r: r["name"])


async def list_spaces() -> list[dict]:
    return parse_spaces(await _get_json(SPACES_PATH, SPACES_PARAMS))


async def get_space(space_id: str) -> dict | None:
    return next((r for r in await list_spaces() if r["id"] == str(space_id)), None)


# ---------------------------------------------------------------- bookings

def _clean_building(name: Any) -> str | None:
    """'Building - Administration' -> 'Administration'."""
    if not name:
        return None
    name = str(name).strip()
    return name.removeprefix("Building - ").strip() or None


def _to_int(v: Any) -> int | None:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _hours_to_dt(day: dt.date, hours: Any) -> dt.datetime | None:
    """25Live times are decimal hours, as strings or floats: '18.5' -> 18:30."""
    if hours is None:
        return None
    return dt.datetime.combine(day, dt.time()) + dt.timedelta(minutes=round(float(hours) * 60))


def parse_availability(data: dict) -> list[dict]:
    """Turn an availabilitydata.json response into booking dicts."""
    out = []
    for subject in data.get("subjects", []):
        day = dt.date.fromisoformat(subject["item_date"][:10])
        for it in subject.get("items", []):
            start = _hours_to_dt(day, it.get("start"))
            end = _hours_to_dt(day, it.get("end"))
            is_event = it.get("type_id") == 1
            state = it.get("cur_event_state")
            out.append({
                "space_id": str(subject.get("itemId")),
                "kind": "event" if is_event else "closed",
                "state": EVENT_STATES.get(state, str(state)) if state is not None else None,
                "name": it.get("itemName") if is_event else "Closed",
                # Booked times include setup/teardown; use these for availability.
                "start": start,
                "end": end,
                # When the event itself runs (same as booked times if no buffer).
                "event_start": _hours_to_dt(day, it.get("ev_start_time")) or start,
                "event_end": _hours_to_dt(day, it.get("ev_end_time")) or end,
                "expected_headcount": it.get("exp_head_count"),
            })
    out.sort(key=lambda b: b["start"])
    return out


async def get_bookings(space_id: str, start: dt.date) -> list[dict]:
    """Bookings for one room, for about a month starting at `start`."""
    # Same parameters, in the same order, as the request the 25Live web app
    # makes. 25Live rejects the request without some of them (e.g. `caller`).
    params = {
        "obj_cache_accl": 0,
        "start_dt": f"{start.isoformat()}T00:00:00",
        "comptype": "availability_daily",
        "compsubject": "location",
        "page_size": 100,
        "space_id": space_id,
        "include": "closed blackouts pending related empty",
        "caller": "pro-AvailService.getData",
    }
    return parse_availability(await _get_json("/availability/availabilitydata.json", params))
