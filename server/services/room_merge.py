"""Combine 25Live data with CampusReserve's own data.

These are plain functions with no I/O, so they're easy to unit test. The
router fetches 25Live and the database, then hands both to these functions.

Conventions:
- A room's public id is its 25Live space_id (Room.external_ref). Rooms that
  exist only in our database use their UUID instead.
- All times are naive datetimes in campus local time (America/New_York),
  because that's what 25Live returns. Use `to_local_naive` on DB datetimes.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from zoneinfo import ZoneInfo

CAMPUS_TZ = ZoneInfo("America/New_York")


def campus_now() -> dt.datetime:
    """Current campus local time, naive (independent of the server's timezone)."""
    return dt.datetime.now(CAMPUS_TZ).replace(tzinfo=None)


def to_local_naive(t: dt.datetime | None) -> dt.datetime | None:
    """Convert a DB datetime to naive campus time so it compares with 25Live times."""
    if t is None or t.tzinfo is None:
        return t  # naive values are assumed to already be campus local time
    return t.astimezone(CAMPUS_TZ).replace(tzinfo=None)


def overlaps(a_start, a_end, b_start, b_end) -> bool:
    return a_start < b_end and b_start < a_end


# ---------------------------------------------------------------- rooms

_ROOM_DEFAULTS = {
    "db_id": None,
    "venue_id": None,
    "features": [],
    "club_bookable": True,
    "is_active": True,
    "booking_opens_days_ahead": None,
    "media_url": None,
}


def merge_rooms(
    live_rooms: list[dict],
    db_rooms: dict[str, dict],
    pending_counts: dict[str, int],
    waitlist_counts: dict[str, int],
) -> list[dict]:
    """One record per room.

    live_rooms:  from live25.list_spaces()
    db_rooms:    {public_id: {...Room columns...}}; non-null DB values override 25Live's
    *_counts:    {public_id: count}
    """
    merged: dict[str, dict] = {}

    for r in live_rooms:
        merged[r["id"]] = {
            **_ROOM_DEFAULTS,
            "id": r["id"],
            "building": r.get("building"),
            "name": r["name"],
            "short_name": r.get("short_name"),
            "capacity": r.get("capacity"),
            "room_type": r.get("room_type"),
            "source": "25live",
        }

    for public_id, local in db_rooms.items():
        room = merged.get(public_id)
        if room is None:
            # In our DB but not in 25Live (no external_ref, or 25Live dropped it).
            merged[public_id] = {**_ROOM_DEFAULTS, "id": public_id, "building": None,
                                 "short_name": None, "source": "campusreserve", **local}
        else:
            room["source"] = "both"
            for key, value in local.items():
                if value is not None:
                    room[key] = value

    for room in merged.values():
        room["pending_requests"] = pending_counts.get(room["id"], 0)
        room["waitlist_count"] = waitlist_counts.get(room["id"], 0)

    return sorted(merged.values(), key=lambda r: r["name"])


# ---------------------------------------------------------------- schedule

def _item(**kw) -> dict:
    base = {
        "source": None, "kind": None, "status": None, "name": "",
        "start": None, "end": None, "event_start": None, "event_end": None,
        "expected_headcount": None, "request_id": None, "waitlist_position": None,
        "waitlist_count": 0, "conflicts_with_25live": False,
    }
    base.update(kw)
    return base


def build_schedule(
    live_bookings: list[dict],
    requests: list[dict],
    waitlisted: list[dict],
    closures: list[dict] | None = None,
) -> list[dict]:
    """One time-ordered schedule for a room.

    live_bookings: from live25.get_bookings() (official 25Live bookings)
    requests:      our requests to show as their own items (pending, and
                   approved-but-not-yet-in-25Live). Each dict has
                   "id", "name", "status", "start", "end" (blocked window incl.
                   setup/cleanup), "event_start", "event_end", "expected_headcount"
    waitlisted:    our waitlisted requests, same shape plus "waitlist_position"
    closures:      our RoomClosures: {"name" (the reason), "start", "end"}

    - Each 25Live event gets `waitlist_count`: how many waitlisted requests
      overlap it.
    - Each request gets `conflicts_with_25live`: True if its blocked window
      overlaps a confirmed or tentative 25Live event.
    - Waitlisted requests that no longer overlap any 25Live event (the booking
      was cancelled, so the slot opened up) appear as their own items.
    """
    events = [b for b in live_bookings if b["kind"] == "event"]
    items: list[dict] = []

    claimed = set()
    for b in live_bookings:
        item = _item(source="25live", kind=b["kind"], status=b["state"], name=b["name"],
                     start=b["start"], end=b["end"],
                     event_start=b["event_start"], event_end=b["event_end"],
                     expected_headcount=b["expected_headcount"])
        if b["kind"] == "event":
            hits = [w for w in waitlisted if overlaps(w["start"], w["end"], b["start"], b["end"])]
            item["waitlist_count"] = len(hits)
            claimed.update(w["id"] for w in hits)
        items.append(item)

    def conflicts(r: dict) -> bool:
        return any(overlaps(r["start"], r["end"], e["start"], e["end"])
                   and e["state"] in ("Confirmed", "Tentative") for e in events)

    for r in requests:
        items.append(_item(source="campusreserve", kind="request", status=r["status"],
                           name=r["name"], start=r["start"], end=r["end"],
                           event_start=r["event_start"], event_end=r["event_end"],
                           expected_headcount=r.get("expected_headcount"),
                           request_id=str(r["id"]), conflicts_with_25live=conflicts(r)))

    # Waitlisted requests whose 25Live booking is gone: the slot opened up.
    open_counts: dict[tuple, int] = defaultdict(int)
    for w in waitlisted:
        if w["id"] in claimed:
            continue
        open_counts[(w["start"], w["end"])] += 1
        items.append(_item(source="campusreserve", kind="waitlist", status="slot_open",
                           name=w["name"], start=w["start"], end=w["end"],
                           event_start=w["event_start"], event_end=w["event_end"],
                           expected_headcount=w.get("expected_headcount"),
                           request_id=str(w["id"]),
                           waitlist_position=w.get("waitlist_position")))
    for it in items:
        if it["kind"] == "waitlist":
            it["waitlist_count"] = open_counts[(it["start"], it["end"])]

    for c in closures or []:
        items.append(_item(source="campusreserve", kind="closed", status="closed",
                           name=c["name"], start=c["start"], end=c["end"],
                           event_start=c["start"], event_end=c["end"]))

    items.sort(key=lambda i: (i["start"], i["source"] != "25live"))
    return items
