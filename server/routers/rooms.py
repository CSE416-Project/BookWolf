"""Room endpoints: list, detail, and schedule.

Each endpoint combines two sources:
- 25Live (official rooms and bookings), via services.live25
- our database (rooms we've configured, requests, waitlist, closures)

The merging itself lives in services.room_merge.

Room ids in URLs are 25Live space_ids (e.g. "1810"), matched to our rooms via
Room.external_ref. Rooms that exist only in our database use their UUID.
"""

import asyncio
import datetime as dt
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import func, or_
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from authentication import get_current_user
from database import get_db
from models.room import Request, RequestStatus, Room, RoomClosure, SyncStatus
from models.user import User
from services import live25
from services.room_merge import build_schedule, campus_now, merge_rooms, to_local_naive

router = APIRouter(prefix="/rooms", tags=["rooms"])


# =============================================================================
# Response models
# =============================================================================

class RoomResponse(BaseModel):
    id: str                        # 25Live space_id, or our UUID for local-only rooms
    db_id: str | None = None       # our Room.id, if we have this room in our DB
    venue_id: str | None = None    # our Venue.id, if we have this room in our DB
    building: str | None = None    # from 25Live, e.g. "Administration"
    name: str
    short_name: str | None = None  # 25Live's ALL-CAPS code
    capacity: int | None = None
    room_type: str | None = None
    features: list[str] = []
    media_url: str | None = None
    club_bookable: bool = True
    is_active: bool = True
    booking_opens_days_ahead: int | None = None
    source: str                    # "25live", "campusreserve", or "both"
    pending_requests: int = 0
    waitlist_count: int = 0


class ScheduleItem(BaseModel):
    source: str                    # "25live" or "campusreserve"
    kind: str                      # "event", "closed", "request", or "waitlist"
    status: str | None = None      # 25Live: Confirmed/Tentative; ours: pending/approved/...
    name: str                      # event name, or the closure reason
    start: dt.datetime             # room blocked from (includes setup)
    end: dt.datetime               # room blocked until (includes cleanup)
    event_start: dt.datetime
    event_end: dt.datetime
    expected_headcount: int | None = None
    request_id: str | None = None
    waitlist_position: int | None = None
    waitlist_count: int = 0
    conflicts_with_25live: bool = False


# =============================================================================
# Database queries (sync SQLAlchemy, so the routes run them in a threadpool)
# =============================================================================

def _public_id(external_ref: str | None, room_id) -> str:
    return external_ref or str(room_id)


def _db_rooms(db: Session) -> dict[str, dict]:
    return {
        _public_id(r.external_ref, r.id): {
            "db_id": str(r.id),
            "venue_id": str(r.venue_id),
            "name": r.name,
            "capacity": r.capacity,
            "room_type": r.room_type,
            "features": list(r.features or []),
            "media_url": r.media_url,
            "club_bookable": r.club_bookable,
            "is_active": r.is_active,
            "booking_opens_days_ahead": r.booking_opens_days_ahead,
        }
        for r in db.query(Room).all()
    }


def _count_by_room(db: Session, *conditions) -> dict[str, int]:
    rows = (
        db.query(Room.external_ref, Room.id, func.count(Request.id))
        .join(Request, Request.room_id == Room.id)
        .filter(Request.end_time >= campus_now(), *conditions)
        .group_by(Room.id, Room.external_ref)
        .all()
    )
    return {_public_id(ref, rid): n for ref, rid, n in rows}


def _room_list_data(db: Session):
    return (
        _db_rooms(db),
        _count_by_room(db, Request.status == RequestStatus.PENDING),
        _count_by_room(db, Request.status == RequestStatus.WAITLISTED),
    )


def _find_room(db: Session, public_id: str) -> Room | None:
    room = db.query(Room).filter(Room.external_ref == public_id).first()
    if room is None:
        try:
            room = db.get(Room, uuid.UUID(public_id))
        except ValueError:
            pass
    return room


def _request_dict(r: Request) -> dict:
    return {
        "id": r.id,
        "name": r.event_name,
        "status": r.status.value,
        # Blocked window (with setup/cleanup) vs. the event itself.
        "start": to_local_naive(r.setup_start or r.start_time),
        "end": to_local_naive(r.cleanup_end or r.end_time),
        "event_start": to_local_naive(r.start_time),
        "event_end": to_local_naive(r.end_time),
        "expected_headcount": r.expected_attendance,
        "waitlist_position": r.waitlist_position,
    }


def _schedule_data(db: Session, public_id: str, start: dt.datetime, end: dt.datetime):
    room = _find_room(db, public_id)
    if room is None:
        return None, [], [], []

    # Requests whose blocked window (start/end widened by setup/cleanup)
    # overlaps [start, end).
    in_window = (
        func.coalesce(Request.setup_start, Request.start_time) < end,
        func.coalesce(Request.cleanup_end, Request.end_time) > start,
    )
    rows = (
        db.query(Request)
        .filter(Request.room_id == room.id, *in_window,
                or_(
                    Request.status.in_([RequestStatus.PENDING, RequestStatus.WAITLISTED]),
                    # Approved but not in 25Live yet: show it, or it would vanish
                    # from the schedule until the sync succeeds.
                    (Request.status == RequestStatus.APPROVED)
                    & (Request.sync_status != SyncStatus.SYNCED),
                ))
        .order_by(Request.waitlist_position.nulls_last(), Request.created_at)
        .all()
    )
    requests = [_request_dict(r) for r in rows if r.status != RequestStatus.WAITLISTED]
    waitlisted = [_request_dict(r) for r in rows if r.status == RequestStatus.WAITLISTED]

    closures = (
        db.query(RoomClosure)
        .filter(or_(RoomClosure.room_id == room.id, RoomClosure.venue_id == room.venue_id),
                RoomClosure.closed_from < end, RoomClosure.closed_until > start)
        .all()
    )
    closure_dicts = [{"name": c.reason, "start": to_local_naive(c.closed_from),
                      "end": to_local_naive(c.closed_until)} for c in closures]

    return room, requests, waitlisted, closure_dicts


# =============================================================================
# Routes
# =============================================================================

def _upstream_error(e: Exception) -> HTTPException:
    return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY,
                         detail=f"Couldn't reach 25Live: {e}")


async def _merged_rooms(db: Session) -> list[dict]:
    # Fetch 25Live and query our DB at the same time.
    try:
        live_rooms, (db_rooms, pending, waitlist) = await asyncio.gather(
            live25.list_spaces(),
            run_in_threadpool(_room_list_data, db),
        )
    except live25.Live25Error as e:
        raise _upstream_error(e)
    return merge_rooms(live_rooms, db_rooms, pending, waitlist)


@router.get("", response_model=list[RoomResponse])
async def list_rooms(
    building: str | None = None,
    min_capacity: int | None = None,
    club_bookable_only: bool = False,
    include_inactive: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List rooms, optionally filtered by building, minimum capacity, or bookability."""
    rooms = await _merged_rooms(db)
    if not include_inactive:
        rooms = [r for r in rooms if r["is_active"]]
    if club_bookable_only:
        rooms = [r for r in rooms if r["club_bookable"]]
    if building:
        b = building.lower()
        rooms = [r for r in rooms
                 if b in (r.get("building") or "").lower() or r.get("venue_id") == building]
    if min_capacity is not None:
        rooms = [r for r in rooms if (r.get("capacity") or 0) >= min_capacity]
    return [RoomResponse(**r) for r in rooms]


@router.get("/{room_id}", response_model=RoomResponse)
async def get_room(
    room_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get a single room's details, with pending and waitlist counts."""
    room = next((r for r in await _merged_rooms(db) if r["id"] == room_id), None)
    if room is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Room not found.")
    return RoomResponse(**room)


@router.get("/{room_id}/schedule", response_model=list[ScheduleItem])
async def get_room_schedule(
    room_id: str,
    start: dt.date | None = None,
    end: dt.date | None = None,
    include_closed: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """A room's 25Live bookings plus our requests, waitlist, and closures,
    from `start` (default today) through `end` (default a week later, max 30 days).

    `include_closed` adds 25Live's daily closed hours; our RoomClosures
    (maintenance, holidays) are always included.
    """
    start = start or campus_now().date()
    end = end or start + dt.timedelta(days=7)
    if end < start:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="end is before start.")
    end = min(end, start + dt.timedelta(days=30))
    window_start = dt.datetime.combine(start, dt.time())
    window_end = dt.datetime.combine(end + dt.timedelta(days=1), dt.time())

    # 25Live space_ids are numeric; anything else is a local-only room's UUID.
    is_25live_room = room_id.isdigit()

    async def fetch_live():
        return await live25.get_bookings(room_id, start) if is_25live_room else []

    try:
        live_bookings, (room, requests, waitlisted, closures) = await asyncio.gather(
            fetch_live(),
            run_in_threadpool(_schedule_data, db, room_id, window_start, window_end),
        )
    except live25.Live25Error as e:
        raise _upstream_error(e)

    if room is None and not is_25live_room:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Room not found.")

    live_bookings = [b for b in live_bookings
                     if b["start"] < window_end and b["end"] > window_start
                     and (include_closed or b["kind"] != "closed")]
    items = build_schedule(live_bookings, requests, waitlisted, closures)
    return [ScheduleItem(**i) for i in items]
