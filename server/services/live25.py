"""Read-only client for Stony Brook's 25Live (guest-visible data).

Everything that knows about 25Live's URLs and JSON shapes lives here, so the
routers never touch raw 25Live responses. Responses are cached in memory so
our API doesn't send a request to 25Live for every page load.

`run_refresh_loop()` keeps every room's next month of bookings cached in the
background, so availability searches across all rooms answer from memory.
Start it from main.py (see the docstring on run_refresh_loop).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger(__name__)

BASE_URL = "https://25live.collegenet.com/25live/data/stonybrook/run"
TIMEOUT_SECONDS = 20
MAX_CONCURRENT_REQUESTS = 6         # requests to 25Live in flight at once, app-wide
REFRESH_INTERVAL_SECONDS = 30 * 60  # background refresh of every room's bookings
# Longer than the refresh interval, so while the refresh loop runs, users
# never wait on 25Live: the refresh replaces entries before they expire.
CACHE_TTL_SECONDS = 45 * 60
# One availability request returns ~32 days; treat the first 29 as covered.
COVERAGE_DAYS = 29
CAMPUS_TZ = ZoneInfo("America/New_York")

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
_http_slots: asyncio.Semaphore | None = None


def _slots() -> asyncio.Semaphore:
    global _http_slots
    if _http_slots is None:
        _http_slots = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
    return _http_slots


def build_url(path: str, params: dict[str, Any]) -> str:
    """Build the URL the same way the 25Live web app does: spaces as '+',
    colons left as-is (e.g. start_dt=2026-10-08T00:00:00)."""
    return f"{BASE_URL}{path}?{urlencode(params, safe=':')}" if params else BASE_URL + path


async def _get_json(path: str, params: dict[str, Any], force: bool = False,
                    ttl: float = CACHE_TTL_SECONDS) -> Any:
    """Fetch JSON, from the cache when fresh. `force=True` always refetches."""
    url = key = build_url(path, params)
    hit = _cache.get(key)
    if not force and hit and time.monotonic() - hit[0] < ttl:
        return hit[1]

    # One lock per URL so concurrent requests share a single fetch.
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        hit = _cache.get(key)
        if not force and hit and time.monotonic() - hit[0] < ttl:
            return hit[1]
        try:
            async with _slots(), httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
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


def campus_today() -> dt.date:
    return dt.datetime.now(CAMPUS_TZ).date()


def _availability_params(space_id: str, start: dt.date) -> dict[str, Any]:
    # Same parameters, in the same order, as the request the 25Live web app
    # makes. 25Live rejects the request without some of them (e.g. `caller`).
    return {
        "obj_cache_accl": 0,
        "start_dt": f"{start.isoformat()}T00:00:00",
        "comptype": "availability_daily",
        "compsubject": "location",
        "page_size": 100,
        "space_id": space_id,
        "include": "closed blackouts pending related empty",
        "caller": "pro-AvailService.getData",
    }


_AVAILABILITY_PATH = "/availability/availabilitydata.json"


async def get_bookings(space_id: str, start: dt.date, end: dt.date | None = None) -> list[dict]:
    """Bookings for one room covering `start` through `end` (inclusive).

    If that range falls within the next COVERAGE_DAYS, this uses the shared
    "from today" fetch that the background refresh keeps cached, so it doesn't
    hit 25Live. Otherwise it fetches about a month starting at `start`.
    May return bookings outside the range; callers filter by time.
    """
    end = end or start
    today = campus_today()
    if today <= start and end <= today + dt.timedelta(days=COVERAGE_DAYS):
        start = today
    data = await _get_json(_AVAILABILITY_PATH, _availability_params(space_id, start))
    return parse_availability(data)


# ---------------------------------------------------------------- room details

DETAIL_TTL_SECONDS = 24 * 60 * 60  # details (features, hours, rules) rarely change
DETAIL_REFRESH_SECONDS = 24 * 60 * 60

# 25Live returns features and categories as numbers. These map the numbers
# to names. Fill them in data/live25_lookups.json:
#   {"features": {"17": "Projector", ...}, "categories": {"7": "Classroom", ...}}
LOOKUPS_FILE = Path(__file__).resolve().parent.parent / "data" / "live25_lookups.json"
_lookups: dict[str, dict[int, str]] | None = None

# Known negative attribute ids (25Live built-ins), from SBU's detail data.
_ATTR_BUILDING, _ATTR_LATITUDE, _ATTR_LONGITUDE = -6, -14, -15


def lookups() -> dict[str, dict[int, str]]:
    global _lookups
    if _lookups is None:
        _lookups = {"features": {}, "categories": {}}
        if LOOKUPS_FILE.exists():
            try:
                raw = json.loads(LOOKUPS_FILE.read_text())
                for kind in _lookups:
                    _lookups[kind] = {int(k): str(v) for k, v in raw.get(kind, {}).items()}
            except (OSError, ValueError, AttributeError) as e:
                logger.error("Couldn't read %s: %s", LOOKUPS_FILE, e)
    return _lookups


def _to_float(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_space_detail(data: dict) -> dict | None:
    """Turn a micro/space/{id}/detail.json response into a room-detail dict."""
    items = (((data or {}).get("content") or {}).get("data") or {}).get("items") or []
    s = next((i for i in items if i.get("kind") == "space"), None)
    if s is None:
        return None

    names = lookups()
    attrs = {a.get("attributeId"): a.get("value") for a in s.get("attributes") or []}
    default_layout = next((l for l in s.get("layouts") or [] if l.get("defaultLayout")),
                          (s.get("layouts") or [None])[0]) or {}

    return {
        "id": str(s.get("id")),
        "short_name": s.get("spaceName"),
        "name": s.get("spaceFormalName") or s.get("spaceName"),
        "capacity": _to_int(s.get("maxCapacity")),
        "building": attrs.get(_ATTR_BUILDING),
        "latitude": _to_float(attrs.get(_ATTR_LATITUDE)),
        "longitude": _to_float(attrs.get(_ATTR_LONGITUDE)),
        "features": [
            {"id": f.get("featureId"), "quantity": _to_int(f.get("quantity")),
             "name": names["features"].get(f.get("featureId"))}
            for f in s.get("features") or []
        ],
        "categories": [
            {"id": c.get("categoryId"), "name": names["categories"].get(c.get("categoryId"))}
            for c in s.get("categories") or []
        ],
        "comments": (s.get("comments") or "").strip() or None,
        "instructions": (s.get("instructions") or "").strip() or None,
        "hours": [
            {"day": h.get("dayName"), "open": h.get("open"), "close": h.get("close")}
            for h in s.get("hours") or []
        ],
        "layout_capacity": _to_int(default_layout.get("layoutCapacity")),
        "layout_photo_id": default_layout.get("layoutPhotoId"),
        "layout_diagram_id": default_layout.get("layoutDiagramId"),
    }


def _detail_params() -> dict[str, Any]:
    return {"include": "all", "expand": "T", "caller": "pro-SpaceService.getSpace"}


async def get_space_detail(space_id: str, force: bool = False) -> dict | None:
    """Features, categories, hours, rules text, and location for one room."""
    data = await _get_json(f"/micro/space/{space_id}/detail.json", _detail_params(),
                           force=force, ttl=DETAIL_TTL_SECONDS)
    return parse_space_detail(data)


async def get_space_details(space_ids: list[str]) -> dict[str, dict]:
    """Details for many rooms; rooms whose detail can't be fetched are left out."""
    async def one(i: str):
        try:
            return i, await get_space_detail(i)
        except Live25Error as e:
            logger.warning("25Live detail failed for room %s: %s", i, e)
            return i, None

    return {i: d for i, d in await asyncio.gather(*(one(i) for i in space_ids)) if d}


# ---------------------------------------------------------------- background refresh

async def refresh_all_bookings() -> None:
    """Refetch every room's next month of bookings into the cache."""
    rooms = await list_spaces()
    today = campus_today()
    started = time.monotonic()

    async def refresh(space_id: str) -> bool:
        try:
            await _get_json(_AVAILABILITY_PATH, _availability_params(space_id, today), force=True)
            return True
        except Live25Error as e:
            logger.warning("25Live refresh failed for room %s: %s", space_id, e)
            return False

    results = await asyncio.gather(*(refresh(r["id"]) for r in rooms))
    logger.info("25Live refresh: %d/%d rooms in %.0fs",
                sum(results), len(rooms), time.monotonic() - started)


async def run_refresh_loop() -> None:
    """Refresh all rooms now, then every REFRESH_INTERVAL_SECONDS, forever.

    Start it when the app starts, in main.py:

        import asyncio
        from contextlib import asynccontextmanager
        from services import live25

        @asynccontextmanager
        async def lifespan(app):
            task = asyncio.create_task(live25.run_refresh_loop())
            yield
            task.cancel()

        app = FastAPI(title="CampusReserve", lifespan=lifespan)
    """
    last_detail_refresh = None
    while True:
        try:
            rooms = await list_spaces_fresh()
            await refresh_all_bookings()
            # Room details change rarely: refresh them once a day.
            if last_detail_refresh is None or \
                    time.monotonic() - last_detail_refresh >= DETAIL_REFRESH_SECONDS:
                started = time.monotonic()
                await asyncio.gather(*(_refresh_detail(r["id"]) for r in rooms))
                last_detail_refresh = time.monotonic()
                logger.info("25Live room details refreshed in %.0fs",
                            last_detail_refresh - started)
        except Exception:  # keep the loop alive no matter what
            logger.exception("25Live refresh failed")
        await asyncio.sleep(REFRESH_INTERVAL_SECONDS)


async def _refresh_detail(space_id: str) -> None:
    try:
        await get_space_detail(space_id, force=True)
    except Live25Error as e:
        logger.warning("25Live detail refresh failed for room %s: %s", space_id, e)


async def list_spaces_fresh() -> list[dict]:
    """Refetch the room list (picks up rooms added in 25Live)."""
    return parse_spaces(await _get_json(SPACES_PATH, SPACES_PARAMS, force=True))