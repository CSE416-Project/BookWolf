"""Tests for the booking service — submission, approval, denial, and the
overlap check that prevents double-booking."""

from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from models.base import Base
from models.room import Room
from models.request import Request, RequestStatus
from services import booking


# --- Fresh in-memory DB per test ---
engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


@pytest.fixture
def db():
    Base.metadata.create_all(bind=engine)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture
def room(db):
    r = Room(name="Test Room", venue_id="venue-1")
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


# helper: a start/end window offset in hours from a fixed base time
BASE = datetime(2026, 10, 1, 9, 0)

def window(start_h, end_h):
    return BASE + timedelta(hours=start_h), BASE + timedelta(hours=end_h)


# --- Submission ---
def test_submit_creates_pending_request(db, room):
    start, end = window(0, 2)
    req = booking.submit_request(
        db, room_id=room.id, organization_id="org-1",
        requester_id="user-1", start_time=start, end_time=end,
    )
    assert req.status == RequestStatus.PENDING
    assert req.room_id == room.id


def test_submit_rejects_end_before_start(db, room):
    start, end = window(2, 0)  # end before start
    with pytest.raises(HTTPException) as exc:
        booking.submit_request(
            db, room_id=room.id, organization_id="org-1",
            requester_id="user-1", start_time=start, end_time=end,
        )
    assert exc.value.status_code == 400


def test_submit_rejects_unknown_room(db):
    start, end = window(0, 2)
    with pytest.raises(HTTPException) as exc:
        booking.submit_request(
            db, room_id="nonexistent", organization_id="org-1",
            requester_id="user-1", start_time=start, end_time=end,
        )
    assert exc.value.status_code == 404


# --- Approval + the overlap check (the important ones) ---
def _make_request(db, room, start_h, end_h, org="org-1"):
    start, end = window(start_h, end_h)
    return booking.submit_request(
        db, room_id=room.id, organization_id=org,
        requester_id="user-1", start_time=start, end_time=end,
    )


def test_approve_sets_approved_with_reason(db, room):
    req = _make_request(db, room, 0, 2)
    approved = booking.approve_request(
        db, request_id=req.id, admin_id="admin-1", reason="Looks good",
    )
    assert approved.status == RequestStatus.APPROVED
    assert approved.decision_reason == "Looks good"
    assert approved.decided_by_id == "admin-1"


def test_two_overlapping_requests_cannot_both_be_approved(db, room):
    """The core guarantee: approving an overlapping slot is rejected."""
    first = _make_request(db, room, 0, 2)     # 9:00–11:00
    second = _make_request(db, room, 1, 3)    # 10:00–12:00  (overlaps)

    # first approval succeeds
    booking.approve_request(db, request_id=first.id, admin_id="admin-1", reason="ok")

    # second, overlapping, must be rejected
    with pytest.raises(HTTPException) as exc:
        booking.approve_request(db, request_id=second.id, admin_id="admin-1", reason="ok")
    assert exc.value.status_code == 409


def test_adjacent_non_overlapping_requests_both_approve(db, room):
    """Back-to-back bookings that only touch at the boundary are allowed."""
    first = _make_request(db, room, 0, 2)     # 9:00–11:00
    second = _make_request(db, room, 2, 4)    # 11:00–13:00  (touches, no overlap)

    booking.approve_request(db, request_id=first.id, admin_id="admin-1", reason="ok")
    approved2 = booking.approve_request(db, request_id=second.id, admin_id="admin-1", reason="ok")
    assert approved2.status == RequestStatus.APPROVED


def test_same_time_different_rooms_both_approve(db, room):
    """Overlapping times in *different* rooms are fine."""
    other_room = Room(name="Room 2", venue_id="venue-1")
    db.add(other_room)
    db.commit()
    db.refresh(other_room)

    r1 = _make_request(db, room, 0, 2)
    start, end = window(0, 2)
    r2 = booking.submit_request(
        db, room_id=other_room.id, organization_id="org-1",
        requester_id="user-1", start_time=start, end_time=end,
    )

    booking.approve_request(db, request_id=r1.id, admin_id="admin-1", reason="ok")
    approved2 = booking.approve_request(db, request_id=r2.id, admin_id="admin-1", reason="ok")
    assert approved2.status == RequestStatus.APPROVED


def test_cannot_approve_already_decided_request(db, room):
    req = _make_request(db, room, 0, 2)
    booking.approve_request(db, request_id=req.id, admin_id="admin-1", reason="ok")
    # approving again should fail — it's no longer pending
    with pytest.raises(HTTPException) as exc:
        booking.approve_request(db, request_id=req.id, admin_id="admin-1", reason="ok")
    assert exc.value.status_code == 409


# --- Denial ---
def test_deny_sets_denied_with_reason(db, room):
    req = _make_request(db, room, 0, 2)
    denied = booking.deny_request(
        db, request_id=req.id, admin_id="admin-1", reason="Room double-booked elsewhere",
    )
    assert denied.status == RequestStatus.DENIED
    assert denied.decision_reason == "Room double-booked elsewhere"
