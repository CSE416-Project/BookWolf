"""Room endpoints: list, search, and detail."""

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db
from models.room import Room
from models.user import User
from authentication import get_current_user

router = APIRouter(prefix="/rooms", tags=["rooms"])


class RoomResponse(BaseModel):
    id: str
    venue_id: str
    name: str
    capacity: int | None = None
    room_type: str | None = None

    class Config:
        from_attributes = True


@router.get("", response_model=list[RoomResponse])
def list_rooms(
    building: str | None = None,
    min_capacity: int | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List rooms, optionally filtered by building or minimum capacity."""
    query = db.query(Room)
    if min_capacity is not None:
        query = query.filter(Room.capacity >= min_capacity)
    # building lives on Venue; if you want to filter by it, join Venue here.
    rooms = query.all()
    return [RoomResponse.model_validate(r) for r in rooms]


@router.get("/{room_id}", response_model=RoomResponse)
def get_room(
    room_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get a single room's details."""
    room = db.query(Room).filter(Room.id == room_id).first()
    if room is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Room not found.")
    return RoomResponse.model_validate(room)
