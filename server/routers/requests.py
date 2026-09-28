"""Booking request endpoints: submit, list, approve, deny."""

from datetime import datetime

from fastapi import APIRouter, Depends, Security
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db
from models.request import Request
from models.user import User
from authentication import get_current_user
from services import booking

router = APIRouter(prefix="/requests", tags=["requests"])


class RequestCreate(BaseModel):
    room_id: str
    start_time: datetime
    end_time: datetime


class DecisionBody(BaseModel):
    reason: str


class RequestResponse(BaseModel):
    id: str
    room_id: str
    organization_id: str
    start_time: datetime
    end_time: datetime
    status: str

    class Config:
        from_attributes = True


@router.post("", response_model=RequestResponse, status_code=201)
def create_request(
    body: RequestCreate,
    db: Session = Depends(get_db),
    user: User = Security(get_current_user, scopes=["create:requests"]),
):
    req = booking.submit_request(
        db,
        room_id=body.room_id,
        organization_id=str(user.organization_id),
        requester_id=str(user.id),
        start_time=body.start_time,
        end_time=body.end_time,
    )
    return RequestResponse.model_validate(req)


@router.get("", response_model=list[RequestResponse])
def list_my_requests(
    db: Session = Depends(get_db),
    user: User = Security(get_current_user, scopes=["read:requests"]),
):
    """List the caller's organization's requests."""
    reqs = db.query(Request).filter(Request.organization_id == user.organization_id).all()
    return [RequestResponse.model_validate(r) for r in reqs]


@router.post("/{request_id}/approve", response_model=RequestResponse)
def approve(
    request_id: str,
    body: DecisionBody,
    db: Session = Depends(get_db),
    user: User = Security(get_current_user, scopes=["handle:requests"]),
):
    req = booking.approve_request(db, request_id=request_id, admin_id=str(user.id), reason=body.reason)
    return RequestResponse.model_validate(req)


@router.post("/{request_id}/deny", response_model=RequestResponse)
def deny(
    request_id: str,
    body: DecisionBody,
    db: Session = Depends(get_db),
    user: User = Security(get_current_user, scopes=["handle:requests"]),
):
    req = booking.deny_request(db, request_id=request_id, admin_id=str(user.id), reason=body.reason)
    return RequestResponse.model_validate(req)
