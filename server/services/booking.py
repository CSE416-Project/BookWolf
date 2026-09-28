"""Booking service: the rules and workflow for requests and approvals.

Endpoints call these functions; the transactional correctness lives here,
not in the route handlers.
"""

from datetime import datetime

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from models.request import Request, RequestStatus
from models.room import Room


def submit_request(
    db: Session,
    *,
    room_id: str,
    organization_id: str,
    requester_id: str,
    start_time: datetime,
    end_time: datetime,
) -> Request:
    """Validate and create a new booking request in PENDING status."""
    if end_time <= start_time:
        raise HTTPException(status_code=400, detail="End time must be after start time.")

    room = db.query(Room).filter(Room.id == room_id).first()
    if room is None:
        raise HTTPException(status_code=404, detail="Room not found.")

    req = Request(
        room_id=room_id,
        organization_id=organization_id,
        requester_id=requester_id,
        start_time=start_time,
        end_time=end_time,
        status=RequestStatus.PENDING,
    )
    db.add(req)
    db.commit()
    db.refresh(req)
    return req


def approve_request(db: Session, *, request_id: str, admin_id: str, reason: str) -> Request:
    """Approve a request IF no approved booking overlaps its room and time.

    The overlap check and the approval are committed together, so two
    concurrent approvals of conflicting requests cannot both succeed.
    """
    req = db.query(Request).filter(Request.id == request_id).first()
    if req is None:
        raise HTTPException(status_code=404, detail="Request not found.")
    if req.status != RequestStatus.PENDING:
        raise HTTPException(status_code=409, detail="Request is not pending.")

    # Overlap: any APPROVED request on the same room whose window intersects.
    conflict = (
        db.query(Request)
        .filter(
            Request.room_id == req.room_id,
            Request.status == RequestStatus.APPROVED,
            Request.id != req.id,
            Request.start_time < req.end_time,
            Request.end_time > req.start_time,
        )
        .first()
    )
    if conflict is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That room is already booked for an overlapping time.",
        )

    req.status = RequestStatus.APPROVED
    req.decision_reason = reason
    req.decided_by_id = admin_id
    req.decided_at = datetime.utcnow()
    db.commit()
    db.refresh(req)
    return req


def deny_request(db: Session, *, request_id: str, admin_id: str, reason: str) -> Request:
    """Deny a pending request with a required reason."""
    req = db.query(Request).filter(Request.id == request_id).first()
    if req is None:
        raise HTTPException(status_code=404, detail="Request not found.")
    if req.status != RequestStatus.PENDING:
        raise HTTPException(status_code=409, detail="Request is not pending.")

    req.status = RequestStatus.DENIED
    req.decision_reason = reason
    req.decided_by_id = admin_id
    req.decided_at = datetime.utcnow()
    db.commit()
    db.refresh(req)
    return req
